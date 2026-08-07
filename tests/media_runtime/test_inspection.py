from __future__ import annotations

import pytest

from services.media_runtime.common import PipelineStatus
from services.media_runtime.inspection import (
    ArchiveEntry,
    ArchiveManifest,
    InspectionPolicy,
    MediaAsset,
    MediaPreprocessor,
    ScanResult,
    ScanVerdict,
)


class StaticDetector:
    def __init__(self, detected: str) -> None:
        self.detected = detected
        self.calls: list[str] = []

    def detect(self, object_ref: str) -> str:
        self.calls.append(object_ref)
        return self.detected


class RecordingScanner:
    def __init__(
        self,
        verdict: ScanVerdict = ScanVerdict.CLEAN,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.verdict = verdict
        self.failure = failure
        self.calls: list[tuple[str, int]] = []

    def scan(self, object_ref: str, *, timeout_seconds: int) -> ScanResult:
        self.calls.append((object_ref, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return ScanResult(verdict=self.verdict, detail="scanner-detail-SENTINEL")


class StaticArchiveInspector:
    def __init__(self, manifest: ArchiveManifest | None = None) -> None:
        self.manifest = manifest or ArchiveManifest(entries=())
        self.calls: list[str] = []

    def inspect(self, object_ref: str) -> ArchiveManifest:
        self.calls.append(object_ref)
        return self.manifest


def _policy() -> InspectionPolicy:
    return InspectionPolicy(
        allowed_media_types=frozenset(
            {"audio/wav", "audio/mpeg", "application/pdf", "application/zip"}
        ),
        archive_media_types=frozenset({"application/zip"}),
        max_file_bytes=100,
        max_expanded_bytes=200,
        max_compression_ratio=10,
        max_archive_entries=3,
        max_nesting_depth=1,
        scanner_timeout_seconds=5,
    )


def _asset(
    *,
    declared_mime: str = "audio/wav",
    byte_size: int = 10,
) -> MediaAsset:
    return MediaAsset(
        object_ref="object://site-a/upload-01",
        declared_mime=declared_mime,
        byte_size=byte_size,
    )


def _processor(
    *,
    detected: str = "audio/wav",
    scanner: RecordingScanner | None = None,
    manifest: ArchiveManifest | None = None,
) -> tuple[MediaPreprocessor, RecordingScanner, StaticArchiveInspector]:
    actual_scanner = scanner or RecordingScanner()
    archive = StaticArchiveInspector(manifest)
    return (
        MediaPreprocessor(
            magic_detector=StaticDetector(detected),
            malware_scanner=actual_scanner,
            archive_inspector=archive,
            policy=_policy(),
        ),
        actual_scanner,
        archive,
    )


def test_clean_allowlisted_file_is_ready_after_magic_and_local_scan() -> None:
    processor, scanner, archive = _processor()

    result = processor.inspect(_asset(), idempotency_key="inspect:01")

    assert result.status is PipelineStatus.READY
    assert result.detected_mime == "audio/wav"
    assert result.reason_codes == ()
    assert scanner.calls == [("object://site-a/upload-01", 5)]
    assert archive.calls == []


def test_mime_spoof_is_quarantined_without_scanner_or_archive_processing() -> None:
    processor, scanner, archive = _processor(detected="application/pdf")

    result = processor.inspect(_asset(), idempotency_key="inspect:01")

    assert result.status is PipelineStatus.QUARANTINED
    assert result.reason_codes == ("mime_mismatch",)
    assert scanner.calls == []
    assert archive.calls == []


def test_invalid_magic_detector_output_is_generic_and_not_exposed() -> None:
    processor, scanner, archive = _processor(detected="audio/wav\nmagic-SENTINEL-secret")

    result = processor.inspect(_asset(), idempotency_key="inspect:01")

    assert result.status is PipelineStatus.QUARANTINED
    assert result.detected_mime is None
    assert result.reason_codes == ("magic_detector_invalid",)
    assert "magic-SENTINEL" not in repr(result)
    assert scanner.calls == []
    assert archive.calls == []


@pytest.mark.parametrize(
    ("asset", "reason"),
    (
        (_asset(declared_mime="application/x-msdownload"), "media_type_not_allowed"),
        (_asset(byte_size=101), "file_size_exceeded"),
    ),
)
def test_disallowed_or_oversized_file_is_quarantined_before_scanning(
    asset: MediaAsset,
    reason: str,
) -> None:
    processor, scanner, archive = _processor(detected=asset.declared_mime)

    result = processor.inspect(asset, idempotency_key="inspect:01")

    assert result.status is PipelineStatus.QUARANTINED
    assert reason in result.reason_codes
    assert scanner.calls == []
    assert archive.calls == []


@pytest.mark.parametrize(
    ("entries", "reason"),
    (
        (
            (
                ArchiveEntry(
                    path="../escape.wav",
                    compressed_size=1,
                    uncompressed_size=1,
                ),
            ),
            "archive_path_traversal",
        ),
        (
            (
                ArchiveEntry(
                    path="/absolute.wav",
                    compressed_size=1,
                    uncompressed_size=1,
                ),
            ),
            "archive_path_traversal",
        ),
        (
            (
                ArchiveEntry(
                    path="link.wav",
                    compressed_size=1,
                    uncompressed_size=1,
                    is_symlink=True,
                ),
            ),
            "archive_symlink",
        ),
        (
            (
                ArchiveEntry(
                    path="bomb.wav",
                    compressed_size=1,
                    uncompressed_size=50,
                ),
            ),
            "archive_compression_ratio_exceeded",
        ),
        (
            (
                ArchiveEntry(
                    path="nested.zip",
                    compressed_size=10,
                    uncompressed_size=20,
                    nested_depth=2,
                ),
            ),
            "archive_nesting_exceeded",
        ),
        (
            tuple(
                ArchiveEntry(
                    path=f"{index}.wav",
                    compressed_size=1,
                    uncompressed_size=1,
                )
                for index in range(4)
            ),
            "archive_entry_count_exceeded",
        ),
        (
            (
                ArchiveEntry(
                    path="a.wav",
                    compressed_size=30,
                    uncompressed_size=120,
                ),
                ArchiveEntry(
                    path="b.wav",
                    compressed_size=30,
                    uncompressed_size=120,
                ),
            ),
            "archive_expanded_size_exceeded",
        ),
        (
            (
                ArchiveEntry(
                    path="payload.exe",
                    compressed_size=10,
                    uncompressed_size=20,
                ),
            ),
            "archive_executable",
        ),
        (
            (
                ArchiveEntry(
                    path="sheet.xlsm",
                    compressed_size=10,
                    uncompressed_size=20,
                ),
            ),
            "archive_macro",
        ),
        (
            (
                ArchiveEntry(
                    path=r"..\\escape.wav",
                    compressed_size=1,
                    uncompressed_size=1,
                ),
            ),
            "archive_path_traversal",
        ),
    ),
)
def test_unsafe_archive_metadata_is_quarantined_without_extraction_or_scan(
    entries: tuple[ArchiveEntry, ...],
    reason: str,
) -> None:
    processor, scanner, archive = _processor(
        detected="application/zip",
        manifest=ArchiveManifest(entries=entries),
    )

    result = processor.inspect(
        _asset(declared_mime="application/zip"),
        idempotency_key="inspect:01",
    )

    assert result.status is PipelineStatus.QUARANTINED
    assert reason in result.reason_codes
    assert archive.calls == ["object://site-a/upload-01"]
    assert scanner.calls == []


def test_negative_archive_sizes_are_quarantined_as_invalid_metadata() -> None:
    processor, scanner, _archive = _processor(
        detected="application/zip",
        manifest=ArchiveManifest(
            entries=(
                ArchiveEntry(
                    path="invalid.wav",
                    compressed_size=-1,
                    uncompressed_size=-1,
                ),
            )
        ),
    )

    result = processor.inspect(
        _asset(declared_mime="application/zip"),
        idempotency_key="inspect:01",
    )

    assert result.status is PipelineStatus.QUARANTINED
    assert result.reason_codes == ("archive_metadata_invalid",)
    assert scanner.calls == []


@pytest.mark.parametrize(
    ("verdict", "status", "reason"),
    (
        (ScanVerdict.POSITIVE, PipelineStatus.QUARANTINED, "malware_detected"),
        (ScanVerdict.UNAVAILABLE, PipelineStatus.RETRY, "scanner_unavailable"),
        (ScanVerdict.TIMEOUT, PipelineStatus.RETRY, "scanner_timeout"),
        (ScanVerdict.ERROR, PipelineStatus.RETRY, "scanner_error"),
    ),
)
def test_scanner_non_clean_results_fail_closed(
    verdict: ScanVerdict,
    status: PipelineStatus,
    reason: str,
) -> None:
    scanner = RecordingScanner(verdict)
    processor, _scanner, _archive = _processor(scanner=scanner)

    result = processor.inspect(_asset(), idempotency_key="inspect:01")

    assert result.status is status
    assert result.reason_codes == (reason,)
    assert "scanner-detail-SENTINEL" not in repr(result)


def test_scanner_exception_is_retryable_and_redacted() -> None:
    scanner = RecordingScanner(failure=RuntimeError("scanner-SENTINEL"))
    processor, _scanner, _archive = _processor(scanner=scanner)

    result = processor.inspect(_asset(), idempotency_key="inspect:01")

    assert result.status is PipelineStatus.RETRY
    assert result.reason_codes == ("scanner_error",)
    assert "scanner-SENTINEL" not in repr(result)


def test_empty_stage_idempotency_key_is_rejected_before_dependencies() -> None:
    processor, scanner, archive = _processor()

    with pytest.raises(ValueError, match="idempotency_key_required"):
        processor.inspect(_asset(), idempotency_key="")

    assert scanner.calls == []
    assert archive.calls == []
