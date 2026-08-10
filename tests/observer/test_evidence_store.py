from __future__ import annotations

import hashlib
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from observer import evidence_store as evidence_store_module
from observer.evidence_store import ContentAddressedEvidenceStore, EvidenceIntegrityError
from observer.models import TenantScope

SCOPE = TenantScope(site_id="alpha.example", processing_purpose="observation_processing")


def _object_path(root: Path, content: bytes) -> Path:
    partition = hashlib.sha256(f"site:{SCOPE.site_id}".encode()).hexdigest()[:32]
    digest = hashlib.sha256(content).hexdigest()
    return root / partition / "sha256" / digest[:2] / digest


def test_put_removes_partial_temp_when_file_sync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    content = b"crash-atomic evidence"
    object_path = _object_path(root, content)

    def fail_file_sync(_fd: int) -> None:
        raise OSError("injected file fsync failure")

    monkeypatch.setattr(evidence_store_module.os, "fsync", fail_file_sync)

    store = ContentAddressedEvidenceStore(root)
    with pytest.raises(OSError, match="injected file fsync failure"):
        store.put(SCOPE, content, media_type="application/octet-stream")

    assert not object_path.exists()
    assert list(object_path.parent.iterdir()) == []


def test_put_removes_partial_temp_when_write_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    content = b"write-all evidence"
    object_path = _object_path(root, content)
    real_write = os.write
    calls = 0

    def interrupt_after_short_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[: max(1, len(data) // 2)])
        raise OSError("injected interrupted write")

    monkeypatch.setattr(evidence_store_module.os, "write", interrupt_after_short_write)

    store = ContentAddressedEvidenceStore(root)
    with pytest.raises(OSError, match="injected interrupted write"):
        store.put(SCOPE, content, media_type="application/octet-stream")

    assert calls == 2
    assert not object_path.exists()
    assert list(object_path.parent.iterdir()) == []


def test_put_publishes_synced_private_temp_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    content = b"durable evidence"
    object_path = _object_path(root, content)
    real_fsync = os.fsync
    real_link = os.link
    synced_modes: list[int] = []
    link_sources: list[Path] = []

    def record_sync(fd: int) -> None:
        synced_modes.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    def record_link(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        source = Path(os.fsdecode(src))
        destination = Path(os.fsdecode(dst))
        link_sources.append(source)
        assert source.parent == destination.parent
        assert stat.S_IMODE(source.stat().st_mode) == 0o400
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(evidence_store_module.os, "fsync", record_sync)
    monkeypatch.setattr(evidence_store_module.os, "link", record_link)

    stored = ContentAddressedEvidenceStore(root).put(
        SCOPE,
        content,
        media_type="application/octet-stream",
    )

    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    assert object_path.read_bytes() == content
    assert link_sources and not link_sources[0].exists()
    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)
    assert list(object_path.parent.iterdir()) == [object_path]


def test_put_verifies_conflicting_published_content_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    content = b"expected evidence"
    object_path = _object_path(root, content)

    def publish_corrupt_competitor(
        _src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        **_kwargs: object,
    ) -> None:
        Path(os.fsdecode(dst)).write_bytes(b"corrupt competitor")
        raise FileExistsError

    monkeypatch.setattr(evidence_store_module.os, "link", publish_corrupt_competitor)

    with pytest.raises(EvidenceIntegrityError, match="sha256"):
        ContentAddressedEvidenceStore(root).put(
            SCOPE,
            content,
            media_type="application/octet-stream",
        )

    assert object_path.read_bytes() == b"corrupt competitor"
    assert list(object_path.parent.iterdir()) == [object_path]


def test_concurrent_puts_publish_once_and_remove_all_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "objects"
    content = b"concurrent evidence"
    object_path = _object_path(root, content)
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    real_link = os.link
    link_sources: list[Path] = []
    link_sources_lock = threading.Lock()

    def synchronized_link(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        **kwargs: object,
    ) -> None:
        with link_sources_lock:
            link_sources.append(Path(os.fsdecode(src)))
        barrier.wait(timeout=5)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(evidence_store_module.os, "link", synchronized_link)
    store = ContentAddressedEvidenceStore(root)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(
            executor.map(
                lambda _: store.put(
                    SCOPE,
                    content,
                    media_type="application/octet-stream",
                ),
                range(worker_count),
            )
        )

    assert len({result.object_ref for result in results}) == 1
    assert len(link_sources) == worker_count
    assert all(not source.exists() for source in link_sources)
    assert object_path.read_bytes() == content
    assert list(object_path.parent.iterdir()) == [object_path]
