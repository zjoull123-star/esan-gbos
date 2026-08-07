from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from .speech import TranscriptSegments

_REFERENCE = re.compile(r"^[a-z]+://[A-Za-z0-9][A-Za-z0-9._~:/-]{0,500}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,255}$")


@dataclass(frozen=True, slots=True)
class TranscriptSegmentReference:
    segment_id: str
    start_ms: int
    end_ms: int
    text_ref: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class TranscriptReferenceSubmission:
    site_id: str
    transcript_ref: str
    source_evidence_ref: str
    language: str
    segments: tuple[TranscriptSegmentReference, ...]


class ObserverSubmissionClient(Protocol):
    def submit_transcript_refs(
        self,
        submission: TranscriptReferenceSubmission,
        *,
        idempotency_key: str,
    ) -> None: ...


class ObserverBridge:
    """Narrow refs-only bridge; it has no formal command or action capability."""

    def __init__(self, *, client: ObserverSubmissionClient) -> None:
        self._client = client

    def publish(self, transcript: TranscriptSegments, *, idempotency_key: str) -> str:
        if not idempotency_key or len(idempotency_key) > 512:
            raise ValueError("idempotency_key_invalid")
        if not _IDENTIFIER.fullmatch(transcript.transcript_id):
            raise ValueError("transcript_id_invalid")
        if not transcript.site_id or len(transcript.site_id) > 140:
            raise ValueError("site_id_invalid")
        if _REFERENCE.fullmatch(transcript.source_evidence_ref) is None:
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
            if (
                _REFERENCE.fullmatch(segment.text_ref) is None
                or _REFERENCE.fullmatch(segment.evidence_ref) is None
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
            transcript_ref=transcript_ref,
            source_evidence_ref=transcript.source_evidence_ref,
            language=transcript.language,
            segments=tuple(segments),
        )
        self._client.submit_transcript_refs(submission, idempotency_key=idempotency_key)
        return transcript_ref

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


__all__ = [
    "ObserverBridge",
    "ObserverSubmissionClient",
    "TranscriptReferenceSubmission",
    "TranscriptSegmentReference",
]
