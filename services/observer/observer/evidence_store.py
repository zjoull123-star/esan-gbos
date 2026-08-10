from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from .models import StoredObject, TenantScope

_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class EvidenceStoreError(ValueError):
    pass


class EvidenceIntegrityError(EvidenceStoreError):
    pass


class SiteIsolationError(EvidenceStoreError):
    pass


class ContentAddressedEvidenceStore:
    """Site-partitioned immutable bytes behind opaque content references."""

    def __init__(self, root: Path) -> None:
        root = Path(root)
        if root.exists() and root.is_symlink():
            raise EvidenceStoreError("object store root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        self._root = root.resolve(strict=True)

    @staticmethod
    def _partition(scope: TenantScope) -> str:
        return hashlib.sha256(f"site:{scope.site_id}".encode()).hexdigest()[:32]

    def _object_ref(self, scope: TenantScope, digest: str) -> str:
        return f"obs:v1:{self._partition(scope)}:sha256:{digest}"

    def _parse_ref(self, scope: TenantScope, object_ref: str) -> str:
        parts = object_ref.split(":")
        if len(parts) != 5 or parts[:2] != ["obs", "v1"] or parts[3] != "sha256":
            raise EvidenceStoreError("invalid opaque object reference")
        if parts[2] != self._partition(scope):
            raise SiteIsolationError("object reference belongs to another site")
        digest = parts[4]
        if not _DIGEST.fullmatch(digest):
            raise EvidenceStoreError("invalid object digest")
        return digest

    def _path(self, scope: TenantScope, digest: str, *, create: bool) -> Path:
        partition = self._root / self._partition(scope)
        digest_root = partition / "sha256"
        parent = digest_root / digest[:2]
        if create:
            for directory in (partition, digest_root, parent):
                if directory.exists() and directory.is_symlink():
                    raise EvidenceStoreError("symlink in object store partition")
                directory.mkdir(exist_ok=True)
        path = parent / digest
        if path.exists() and path.is_symlink():
            raise EvidenceStoreError("object path cannot be a symlink")
        try:
            path.resolve(strict=False).relative_to(self._root)
        except ValueError as exc:
            raise EvidenceStoreError("object path escaped store root") from exc
        return path

    def put(self, scope: TenantScope, content: bytes, *, media_type: str) -> StoredObject:
        if not isinstance(content, bytes):
            raise TypeError("object content must be bytes")
        if not media_type:
            raise ValueError("media_type is required")
        digest = hashlib.sha256(content).hexdigest()
        path = self._path(scope, digest, create=True)
        if path.exists():
            self._verify(path, digest)
        else:
            temp_path = self._write_temp(path, content)
            try:
                try:
                    os.link(temp_path, path, follow_symlinks=False)
                except FileExistsError:
                    self._verify(path, digest)
            finally:
                temp_path.unlink(missing_ok=True)
                self._fsync_directory(path.parent)
        return StoredObject(
            object_ref=self._object_ref(scope, digest),
            sha256=digest,
            size=len(content),
            media_type=media_type,
        )

    @classmethod
    def _write_temp(cls, path: Path, content: bytes) -> Path:
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            try:
                remaining = memoryview(content)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("failed to make progress writing evidence object")
                    remaining = remaining[written:]
                os.fchmod(fd, 0o400)
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            finally:
                cls._fsync_directory(path.parent)
            raise
        return temp_path

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _verify(path: Path, expected_digest: str) -> bytes:
        content = path.read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if not hmac_digest_equal(actual_digest, expected_digest):
            raise EvidenceIntegrityError("stored object sha256 verification failed")
        return content

    def read(self, scope: TenantScope, object_ref: str) -> bytes:
        digest = self._parse_ref(scope, object_ref)
        path = self._path(scope, digest, create=False)
        if not path.is_file():
            raise FileNotFoundError("evidence object not found")
        return self._verify(path, digest)

    def delete(self, scope: TenantScope, object_ref: str) -> None:
        digest = self._parse_ref(scope, object_ref)
        path = self._path(scope, digest, create=False)
        if path.exists():
            self._verify(path, digest)
            path.unlink()

    def exists(self, scope: TenantScope, object_ref: str) -> bool:
        digest = self._parse_ref(scope, object_ref)
        path = self._path(scope, digest, create=False)
        if not path.exists():
            return False
        self._verify(path, digest)
        return True


def hmac_digest_equal(left: str, right: str) -> bool:
    """Constant-time comparison without coupling the store to auth objects."""

    import hmac

    return hmac.compare_digest(left, right)
