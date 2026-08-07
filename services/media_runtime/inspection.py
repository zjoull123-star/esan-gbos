from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol

from .common import PipelineStatus


class ScanVerdict(StrEnum):
    CLEAN = "clean"
    POSITIVE = "positive"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: ScanVerdict
    detail: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    path: str = field(repr=False)
    compressed_size: int
    uncompressed_size: int
    is_symlink: bool = False
    nested_depth: int = 0


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    entries: tuple[ArchiveEntry, ...]


@dataclass(frozen=True, slots=True)
class MediaAsset:
    object_ref: str
    declared_mime: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class InspectionPolicy:
    allowed_media_types: frozenset[str]
    archive_media_types: frozenset[str]
    max_file_bytes: int
    max_expanded_bytes: int
    max_compression_ratio: float
    max_archive_entries: int
    max_nesting_depth: int
    scanner_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class InspectionResult:
    status: PipelineStatus
    detected_mime: str | None
    reason_codes: tuple[str, ...]


class MagicDetector(Protocol):
    def detect(self, object_ref: str) -> str: ...


class MalwareScanner(Protocol):
    def scan(self, object_ref: str, *, timeout_seconds: int) -> ScanResult: ...


class ArchiveInspector(Protocol):
    def inspect(self, object_ref: str) -> ArchiveManifest: ...


_EXECUTABLE_SUFFIXES = frozenset(
    {
        ".app",
        ".bat",
        ".bin",
        ".cmd",
        ".com",
        ".dll",
        ".dmg",
        ".exe",
        ".jar",
        ".js",
        ".msi",
        ".ps1",
        ".scr",
        ".sh",
    }
)
_MACRO_SUFFIXES = frozenset({".docm", ".pptm", ".xlsm", ".xltm"})
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class MediaPreprocessor:
    def __init__(
        self,
        *,
        magic_detector: MagicDetector,
        malware_scanner: MalwareScanner,
        archive_inspector: ArchiveInspector,
        policy: InspectionPolicy,
    ) -> None:
        self._magic_detector = magic_detector
        self._malware_scanner = malware_scanner
        self._archive_inspector = archive_inspector
        self._policy = policy

    def inspect(self, asset: MediaAsset, *, idempotency_key: str) -> InspectionResult:
        if not idempotency_key:
            raise ValueError("idempotency_key_required")
        if asset.byte_size < 0 or asset.byte_size > self._policy.max_file_bytes:
            return _quarantine(None, "file_size_exceeded")
        if asset.declared_mime not in self._policy.allowed_media_types:
            return _quarantine(None, "media_type_not_allowed")
        try:
            detected_mime = self._magic_detector.detect(asset.object_ref)
        except Exception:
            return _retry(None, "magic_detector_error")
        if (
            not isinstance(detected_mime, str)
            or len(detected_mime) > 255
            or _MEDIA_TYPE.fullmatch(detected_mime) is None
        ):
            return _quarantine(None, "magic_detector_invalid")
        if detected_mime != asset.declared_mime:
            return _quarantine(detected_mime, "mime_mismatch")

        if detected_mime in self._policy.archive_media_types:
            try:
                manifest = self._archive_inspector.inspect(asset.object_ref)
            except Exception:
                return _retry(detected_mime, "archive_inspector_error")
            archive_reasons = self._archive_reasons(manifest)
            if archive_reasons:
                return InspectionResult(
                    status=PipelineStatus.QUARANTINED,
                    detected_mime=detected_mime,
                    reason_codes=archive_reasons,
                )

        try:
            scan = self._malware_scanner.scan(
                asset.object_ref,
                timeout_seconds=self._policy.scanner_timeout_seconds,
            )
        except Exception:
            return _retry(detected_mime, "scanner_error")
        return _scan_result(detected_mime, scan.verdict)

    def _archive_reasons(self, manifest: ArchiveManifest) -> tuple[str, ...]:
        reasons: list[str] = []
        entries = manifest.entries
        if len(entries) > self._policy.max_archive_entries:
            reasons.append("archive_entry_count_exceeded")
        expanded_size = sum(max(entry.uncompressed_size, 0) for entry in entries)
        if expanded_size > self._policy.max_expanded_bytes:
            reasons.append("archive_expanded_size_exceeded")
        for entry in entries:
            if entry.compressed_size < 0 or entry.uncompressed_size < 0:
                reasons.append("archive_metadata_invalid")
                continue
            if _unsafe_archive_path(entry.path):
                reasons.append("archive_path_traversal")
            if entry.is_symlink:
                reasons.append("archive_symlink")
            if entry.nested_depth > self._policy.max_nesting_depth:
                reasons.append("archive_nesting_exceeded")
            if _compression_ratio(entry) > self._policy.max_compression_ratio:
                reasons.append("archive_compression_ratio_exceeded")
            suffix = PurePosixPath(entry.path.replace("\\", "/")).suffix.casefold()
            if suffix in _EXECUTABLE_SUFFIXES:
                reasons.append("archive_executable")
            if suffix in _MACRO_SUFFIXES:
                reasons.append("archive_macro")
        return tuple(dict.fromkeys(reasons))


def _unsafe_archive_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not normalized or path.is_absolute() or ".." in path.parts or "\x00" in normalized


def _compression_ratio(entry: ArchiveEntry) -> float:
    if entry.uncompressed_size <= 0:
        return 0.0
    if entry.compressed_size <= 0:
        return float("inf")
    return entry.uncompressed_size / entry.compressed_size


def _quarantine(detected_mime: str | None, reason: str) -> InspectionResult:
    return InspectionResult(
        status=PipelineStatus.QUARANTINED,
        detected_mime=detected_mime,
        reason_codes=(reason,),
    )


def _retry(detected_mime: str | None, reason: str) -> InspectionResult:
    return InspectionResult(
        status=PipelineStatus.RETRY,
        detected_mime=detected_mime,
        reason_codes=(reason,),
    )


def _scan_result(detected_mime: str, verdict: ScanVerdict) -> InspectionResult:
    if verdict is ScanVerdict.CLEAN:
        return InspectionResult(
            status=PipelineStatus.READY,
            detected_mime=detected_mime,
            reason_codes=(),
        )
    if verdict is ScanVerdict.POSITIVE:
        return _quarantine(detected_mime, "malware_detected")
    reason = {
        ScanVerdict.UNAVAILABLE: "scanner_unavailable",
        ScanVerdict.TIMEOUT: "scanner_timeout",
        ScanVerdict.ERROR: "scanner_error",
    }[verdict]
    return _retry(detected_mime, reason)
