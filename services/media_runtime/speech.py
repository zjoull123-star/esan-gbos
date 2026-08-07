from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

WHISPER_MODEL_NAME = "large-v3-turbo"
WHISPER_MODEL_SHA256 = "c" * 64
FASTER_WHISPER_VERSION = "faster-whisper-local-v1"

_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_MAX_SEGMENTS = 10_000
_MAX_SEGMENT_TEXT = 4_000
_SPEAKER_THRESHOLD = 0.5
_TEXT_REFERENCE = re.compile(r"^localtext://[A-Za-z0-9][A-Za-z0-9._~:/-]{0,500}$")
_EVIDENCE_REFERENCE = re.compile(r"^evidence://[A-Za-z0-9][A-Za-z0-9._~:/-]{0,500}$")


class SpeechRejected(ValueError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, retryable={self.retryable!r})"


@dataclass(frozen=True, slots=True)
class EngineConfig:
    model_name: str = WHISPER_MODEL_NAME
    model_sha256: str = WHISPER_MODEL_SHA256
    engine_version: str = FASTER_WHISPER_VERSION
    model_mount: str = "/models/large-v3-turbo"
    read_only_model_mount: bool = True
    offline: bool = True
    allow_runtime_download: bool = False


@dataclass(frozen=True, slots=True)
class EngineSegment:
    start_seconds: float
    end_seconds: float
    text: str = field(repr=False)
    confidence: float
    speaker_label: str | None = field(default=None, repr=False)
    speaker_confidence: float | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EngineTranscript:
    language: str
    segments: tuple[EngineSegment, ...]


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    site_id: str
    audio_ref: str
    source_evidence_ref: str
    duration_ms: int
    language_hint: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    segment_id: str
    start_ms: int
    end_ms: int
    speaker: str
    confidence: float
    text_ref: str
    evidence_ref: str

    def to_contract(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker": self.speaker,
            "confidence": self.confidence,
            "text_ref": self.text_ref,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True, slots=True)
class TranscriptSegments:
    transcript_id: str
    site_id: str
    source_evidence_ref: str
    model_provider: str
    model_name: str
    model_version: str
    model_sha256: str
    language: str
    segments: tuple[TranscriptSegment, ...]
    generated_at: datetime

    def to_contract(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "transcript_id": self.transcript_id,
            "site_id": self.site_id,
            "source_evidence_ref": self.source_evidence_ref,
            "model": {
                "provider": self.model_provider,
                "name": self.model_name,
                "version": self.model_version,
                "sha256": self.model_sha256,
            },
            "language": self.language,
            "segments": [segment.to_contract() for segment in self.segments],
            "generated_at": _rfc3339(self.generated_at),
        }


class WhisperEngine(Protocol):
    def transcribe(
        self,
        audio_ref: str,
        *,
        config: EngineConfig,
        language_hint: str | None,
    ) -> EngineTranscript: ...


class TranscriptTextStore(Protocol):
    def store(self, text: str, *, idempotency_key: str) -> str: ...


class EvidenceLocatorFactory(Protocol):
    def for_time_range(
        self,
        source_evidence_ref: str,
        *,
        start_ms: int,
        end_ms: int,
        idempotency_key: str,
    ) -> str: ...


class SpeechProvider(Protocol):
    def transcribe(
        self,
        request: SpeechRequest,
        *,
        idempotency_key: str,
    ) -> TranscriptSegments: ...


@dataclass(frozen=True, slots=True)
class _ValidatedSegment:
    index: int
    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    confidence: float
    speaker: str


class FasterWhisperAdapter:
    def __init__(
        self,
        *,
        engine: WhisperEngine,
        text_store: TranscriptTextStore,
        evidence_factory: EvidenceLocatorFactory,
        clock: Callable[[], datetime],
        transcript_id_factory: Callable[[], str],
    ) -> None:
        self._engine = engine
        self._text_store = text_store
        self._evidence_factory = evidence_factory
        self._clock = clock
        self._transcript_id_factory = transcript_id_factory
        self._config = EngineConfig()

    def transcribe(
        self,
        request: SpeechRequest,
        *,
        idempotency_key: str,
    ) -> TranscriptSegments:
        if not idempotency_key:
            raise SpeechRejected("idempotency_key_required")
        self._validate_request(request)
        try:
            raw = self._engine.transcribe(
                request.audio_ref,
                config=self._config,
                language_hint=request.language_hint,
            )
        except Exception:
            raise SpeechRejected("speech_engine_unavailable", retryable=True) from None
        validated = self._validate_output(raw, duration_ms=request.duration_ms)
        transcript_id = self._transcript_id_factory()
        generated_at = self._clock()
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise SpeechRejected("clock_invalid")

        segments: list[TranscriptSegment] = []
        for segment in validated:
            suffix = f"{segment.index:06d}"
            try:
                text_ref = self._text_store.store(
                    segment.text,
                    idempotency_key=f"{idempotency_key}:text:{suffix}",
                )
            except Exception:
                raise SpeechRejected("speech_reference_store_unavailable", retryable=True) from None
            if not _valid_reference(text_ref, _TEXT_REFERENCE):
                raise SpeechRejected("text_ref_invalid")
            try:
                evidence_ref = self._evidence_factory.for_time_range(
                    request.source_evidence_ref,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    idempotency_key=f"{idempotency_key}:evidence:{suffix}",
                )
            except Exception:
                raise SpeechRejected("speech_reference_store_unavailable", retryable=True) from None
            if not _valid_reference(evidence_ref, _EVIDENCE_REFERENCE):
                raise SpeechRejected("evidence_ref_invalid")
            segments.append(
                TranscriptSegment(
                    segment_id=f"segment_{suffix}",
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                    speaker=segment.speaker,
                    confidence=segment.confidence,
                    text_ref=text_ref,
                    evidence_ref=evidence_ref,
                )
            )
        return TranscriptSegments(
            transcript_id=transcript_id,
            site_id=request.site_id,
            source_evidence_ref=request.source_evidence_ref,
            model_provider="local_faster_whisper",
            model_name=WHISPER_MODEL_NAME,
            model_version=FASTER_WHISPER_VERSION,
            model_sha256=WHISPER_MODEL_SHA256,
            language=raw.language,
            segments=tuple(segments),
            generated_at=generated_at,
        )

    def _validate_request(self, request: SpeechRequest) -> None:
        if not request.site_id:
            raise SpeechRejected("site_id_required")
        if not request.audio_ref:
            raise SpeechRejected("audio_ref_required")
        if not request.source_evidence_ref:
            raise SpeechRejected("source_evidence_ref_required")
        if not 1 <= request.duration_ms <= 7_200_000:
            raise SpeechRejected("duration_out_of_bounds")
        if request.language_hint is not None and not _LANGUAGE.fullmatch(request.language_hint):
            raise SpeechRejected("language_hint_invalid")

    def _validate_output(
        self,
        raw: EngineTranscript,
        *,
        duration_ms: int,
    ) -> tuple[_ValidatedSegment, ...]:
        if not _LANGUAGE.fullmatch(raw.language):
            raise SpeechRejected("language_invalid")
        if len(raw.segments) > _MAX_SEGMENTS:
            raise SpeechRejected("segment_count_exceeded")
        validated: list[_ValidatedSegment] = []
        previous_end = 0
        for index, segment in enumerate(raw.segments, start=1):
            numeric = (
                segment.start_seconds,
                segment.end_seconds,
                segment.confidence,
            )
            if not all(math.isfinite(value) for value in numeric):
                if not math.isfinite(segment.confidence):
                    raise SpeechRejected("invalid_confidence")
                raise SpeechRejected("non_finite_segment")
            start_ms = round(segment.start_seconds * 1000)
            end_ms = round(segment.end_seconds * 1000)
            if start_ms < 0 or end_ms <= start_ms:
                raise SpeechRejected("invalid_time_range")
            if start_ms < previous_end:
                raise SpeechRejected("overlapping_segments")
            if end_ms > duration_ms:
                raise SpeechRejected("segment_exceeds_audio")
            if not 0 <= segment.confidence <= 1:
                raise SpeechRejected("invalid_confidence")
            if not segment.text:
                raise SpeechRejected("segment_text_empty")
            if len(segment.text) > _MAX_SEGMENT_TEXT:
                raise SpeechRejected("segment_text_too_long")
            if segment.speaker_confidence is not None and (
                not math.isfinite(segment.speaker_confidence)
                or not 0 <= segment.speaker_confidence <= 1
            ):
                raise SpeechRejected("invalid_speaker_confidence")
            validated.append(
                _ValidatedSegment(
                    index=index,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=segment.text,
                    confidence=segment.confidence,
                    speaker=_safe_speaker(
                        segment.speaker_label,
                        segment.speaker_confidence,
                    ),
                )
            )
            previous_end = end_ms
        return tuple(validated)


def _safe_speaker(label: str | None, confidence: float | None) -> str:
    if (
        label in {"Speaker 1", "Speaker 2"}
        and confidence is not None
        and confidence >= _SPEAKER_THRESHOLD
    ):
        return label
    return "unknown"


def _valid_reference(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
