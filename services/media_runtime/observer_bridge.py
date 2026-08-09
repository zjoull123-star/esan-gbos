from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from .speech import TranscriptSegments
from .upload import UploadReceipt

_EVIDENCE_REFERENCE = re.compile(
    r"^evidence://(?P<site>[A-Za-z0-9][A-Za-z0-9.-]{0,139})/"
    r"[A-Za-z0-9][A-Za-z0-9._~:/-]{0,359}$"
)
_LOCAL_TEXT_REFERENCE = re.compile(
    r"^localtext://(?P<site>[A-Za-z0-9][A-Za-z0-9.-]{0,139})/"
    r"[A-Za-z0-9][A-Za-z0-9._~:/-]{0,359}$"
)
_OBJECT_REFERENCE = re.compile(
    r"^(?:object|localobject)://(?P<site>[A-Za-z0-9][A-Za-z0-9.-]{0,139})/"
    r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}$"
)
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True, repr=False)
class TranscriptSegmentReference:
    segment_id: str
    start_ms: int
    end_ms: int
    text_ref: str
    evidence_ref: str

    def __repr__(self) -> str:
        return (
            "TranscriptSegmentReference("
            f"segment_id={self.segment_id!r}, start_ms={self.start_ms}, "
            f"end_ms={self.end_ms}, text_ref=<redacted>, evidence_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TranscriptReferenceSubmission:
    site_id: str
    team_ref: str
    transcript_ref: str
    source_evidence_ref: str
    language: str
    segments: tuple[TranscriptSegmentReference, ...]

    def __repr__(self) -> str:
        return (
            "TranscriptReferenceSubmission("
            f"site_id={self.site_id!r}, team_ref={self.team_ref!r}, "
            "transcript_ref=<redacted>, source_evidence_ref=<redacted>, "
            f"language={self.language!r}, segment_count={len(self.segments)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MediaEvidenceManifest:
    site_id: str
    team_ref: str
    manifest_ref: str
    source_object_ref: str = field(repr=False)
    source_evidence_ref: str = field(repr=False)
    media_type: str = field(repr=False)
    byte_size: int
    content_sha256: str

    def __repr__(self) -> str:
        return (
            "MediaEvidenceManifest("
            f"site_id={self.site_id!r}, team_ref={self.team_ref!r}, "
            "manifest_ref=<redacted>, source_object_ref=<redacted>, "
            "source_evidence_ref=<redacted>, media_type=<redacted>, "
            f"byte_size={self.byte_size}, content_sha256=<redacted>)"
        )


class ObserverSubmissionClient(Protocol):
    def submit_transcript_refs(
        self,
        submission: TranscriptReferenceSubmission,
        *,
        idempotency_key: str,
    ) -> None: ...

    def submit_evidence_manifest(
        self,
        manifest: MediaEvidenceManifest,
        *,
        idempotency_key: str,
    ) -> None: ...


class ObserverBridge:
    """Narrow refs-only bridge; it has no formal command or action capability."""

    def __init__(
        self,
        *,
        client: ObserverSubmissionClient,
        site_id: str,
        team_ref: str,
    ) -> None:
        if _SITE.fullmatch(site_id) is None:
            raise ValueError("site_id_invalid")
        if _IDENTIFIER.fullmatch(team_ref) is None:
            raise ValueError("team_ref_invalid")
        self._client = client
        self._site_id = site_id
        self._team_ref = team_ref

    def __repr__(self) -> str:
        return (
            "ObserverBridge("
            f"site_id={self._site_id!r}, team_ref={self._team_ref!r}, "
            "client=<redacted>)"
        )

    def publish(self, transcript: TranscriptSegments, *, idempotency_key: str) -> str:
        _validate_idempotency_key(idempotency_key)
        if not _IDENTIFIER.fullmatch(transcript.transcript_id):
            raise ValueError("transcript_id_invalid")
        if transcript.site_id != self._site_id:
            raise ValueError("site_scope_invalid")
        if not _site_reference(
            transcript.source_evidence_ref,
            pattern=_EVIDENCE_REFERENCE,
            site_id=transcript.site_id,
        ):
            raise ValueError("source_evidence_ref_invalid")
        transcript_ref = self._transcript_ref(transcript)
        segments: list[TranscriptSegmentReference] = []
        previous_end = 0
        for segment in transcript.segments:
            if (
                _IDENTIFIER.fullmatch(segment.segment_id) is None
                or segment.start_ms < previous_end
                or segment.end_ms <= segment.start_ms
            ):
                raise ValueError("invalid_transcript_range")
            if not _site_reference(
                segment.text_ref,
                pattern=_LOCAL_TEXT_REFERENCE,
                site_id=transcript.site_id,
            ) or not _site_reference(
                segment.evidence_ref,
                pattern=_EVIDENCE_REFERENCE,
                site_id=transcript.site_id,
            ):
                raise ValueError("transcript_segment_ref_invalid")
            segments.append(
                TranscriptSegmentReference(
                    segment_id=segment.segment_id,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    text_ref=segment.text_ref,
                    evidence_ref=segment.evidence_ref,
                )
            )
            previous_end = segment.end_ms
        submission = TranscriptReferenceSubmission(
            site_id=transcript.site_id,
            team_ref=self._team_ref,
            transcript_ref=transcript_ref,
            source_evidence_ref=transcript.source_evidence_ref,
            language=transcript.language,
            segments=tuple(segments),
        )
        self._client.submit_transcript_refs(submission, idempotency_key=idempotency_key)
        return transcript_ref

    def publish_media_evidence(
        self,
        receipt: UploadReceipt,
        *,
        idempotency_key: str,
    ) -> str:
        _validate_idempotency_key(idempotency_key)
        if receipt.site_id != self._site_id:
            raise ValueError("site_scope_invalid")
        if receipt.media_type.startswith("audio/"):
            raise ValueError("non_audio_evidence_required")
        if (
            not receipt.media_type
            or "/" not in receipt.media_type
            or receipt.byte_size < 0
            or _SHA256.fullmatch(receipt.sha256) is None
            or _SHA256.fullmatch(receipt.immutable_checksum) is None
        ):
            raise ValueError("media_evidence_metadata_invalid")
        if not _site_reference(
            receipt.object_ref,
            pattern=_OBJECT_REFERENCE,
            site_id=self._site_id,
        ):
            raise ValueError("source_object_ref_invalid")
        if not _site_reference(
            receipt.evidence_ref,
            pattern=_EVIDENCE_REFERENCE,
            site_id=self._site_id,
        ):
            raise ValueError("source_evidence_ref_invalid")
        manifest_ref = self._manifest_ref(receipt)
        manifest = MediaEvidenceManifest(
            site_id=self._site_id,
            team_ref=self._team_ref,
            manifest_ref=manifest_ref,
            source_object_ref=receipt.object_ref,
            source_evidence_ref=receipt.evidence_ref,
            media_type=receipt.media_type,
            byte_size=receipt.byte_size,
            content_sha256=receipt.sha256,
        )
        self._client.submit_evidence_manifest(
            manifest,
            idempotency_key=idempotency_key,
        )
        return manifest_ref

    @staticmethod
    def _transcript_ref(transcript: TranscriptSegments) -> str:
        document = "\x1f".join(
            (
                "observer-transcript-ref-v1",
                transcript.site_id,
                transcript.transcript_id,
                transcript.source_evidence_ref,
                transcript.model_sha256,
            )
        )
        return f"transcript://{hashlib.sha256(document.encode()).hexdigest()}"

    def _manifest_ref(self, receipt: UploadReceipt) -> str:
        document = {
            "schema": "observer-media-evidence-manifest-v1",
            "site_id": self._site_id,
            "team_ref": self._team_ref,
            "source_object_ref": receipt.object_ref,
            "source_evidence_ref": receipt.evidence_ref,
            "media_type": receipt.media_type,
            "byte_size": receipt.byte_size,
            "content_sha256": receipt.sha256,
            "immutable_checksum": receipt.immutable_checksum,
        }
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return f"evidence://{self._site_id}/media-manifest-{hashlib.sha256(encoded).hexdigest()}"


def _site_reference(
    value: str,
    *,
    pattern: re.Pattern[str],
    site_id: str,
) -> bool:
    match = pattern.fullmatch(value)
    return match is not None and match.group("site") == site_id


def _validate_idempotency_key(value: str) -> None:
    if not value or value != value.strip() or len(value) > 512:
        raise ValueError("idempotency_key_invalid")


__all__ = [
    "ObserverBridge",
    "ObserverSubmissionClient",
    "MediaEvidenceManifest",
    "TranscriptReferenceSubmission",
    "TranscriptSegmentReference",
]
