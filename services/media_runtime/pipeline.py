from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .common import PipelineStatus, StageIdempotencyKey, stage_idempotency_key
from .ffmpeg import FFmpegRejected, FFmpegRequest, NormalizedAudio
from .inspection import InspectionResult, MediaAsset
from .speech import SpeechRejected, SpeechRequest, TranscriptSegments
from .upload import UploadReceipt


@dataclass(frozen=True, slots=True)
class MediaPipelineRequest:
    receipt: UploadReceipt
    source_path: str = field(repr=False)
    output_path: str = field(repr=False)
    duration_ms: int
    channels: int
    sample_rate: int
    attempt: int
    max_attempts: int
    language_hint: str | None = None


@dataclass(frozen=True, slots=True)
class MediaPipelineOutcome:
    status: PipelineStatus
    reason_codes: tuple[str, ...]
    stage_idempotency_keys: tuple[StageIdempotencyKey, ...]
    transcript: TranscriptSegments | None = None


class Preprocessor(Protocol):
    def inspect(self, asset: MediaAsset, *, idempotency_key: str) -> InspectionResult: ...


class AudioNormalizer(Protocol):
    def normalize(
        self,
        request: FFmpegRequest,
        *,
        idempotency_key: str,
    ) -> NormalizedAudio: ...


class PipelineSpeechProvider(Protocol):
    def transcribe(
        self,
        request: SpeechRequest,
        *,
        idempotency_key: str,
    ) -> TranscriptSegments: ...


class MediaPipeline:
    def __init__(
        self,
        *,
        preprocessor: Preprocessor,
        normalizer: AudioNormalizer,
        speech_provider: PipelineSpeechProvider,
    ) -> None:
        self._preprocessor = preprocessor
        self._normalizer = normalizer
        self._speech_provider = speech_provider

    def run(self, request: MediaPipelineRequest) -> MediaPipelineOutcome:
        if not 1 <= request.attempt <= request.max_attempts:
            raise ValueError("attempt_out_of_bounds")
        keys: list[StageIdempotencyKey] = []
        inspection_key = self._key(request, "inspect")
        keys.append(inspection_key)
        try:
            inspection = self._preprocessor.inspect(
                MediaAsset(
                    object_ref=request.receipt.object_ref,
                    declared_mime=request.receipt.media_type,
                    byte_size=request.receipt.byte_size,
                ),
                idempotency_key=inspection_key.key,
            )
        except Exception:
            return MediaPipelineOutcome(
                status=self._retry_or_dead_letter(request),
                reason_codes=("preprocessor_error",),
                stage_idempotency_keys=tuple(keys),
            )
        if inspection.status is not PipelineStatus.READY:
            return MediaPipelineOutcome(
                status=self._terminal_status(
                    inspection.status,
                    attempt=request.attempt,
                    max_attempts=request.max_attempts,
                ),
                reason_codes=inspection.reason_codes,
                stage_idempotency_keys=tuple(keys),
            )
        if not request.receipt.media_type.startswith("audio/"):
            return MediaPipelineOutcome(
                status=PipelineStatus.READY,
                reason_codes=(),
                stage_idempotency_keys=tuple(keys),
            )

        normalize_key = self._key(request, "normalize")
        keys.append(normalize_key)
        try:
            normalized = self._normalizer.normalize(
                FFmpegRequest(
                    source_path=request.source_path,
                    output_path=request.output_path,
                    duration_ms=request.duration_ms,
                    channels=request.channels,
                    sample_rate=request.sample_rate,
                ),
                idempotency_key=normalize_key.key,
            )
        except FFmpegRejected as exc:
            return MediaPipelineOutcome(
                status=self._exception_status(
                    retryable=exc.retryable,
                    attempt=request.attempt,
                    max_attempts=request.max_attempts,
                ),
                reason_codes=(exc.code,),
                stage_idempotency_keys=tuple(keys),
            )
        except Exception:
            return MediaPipelineOutcome(
                status=self._retry_or_dead_letter(request),
                reason_codes=("normalizer_error",),
                stage_idempotency_keys=tuple(keys),
            )

        speech_key = self._key(request, "transcribe")
        keys.append(speech_key)
        try:
            transcript = self._speech_provider.transcribe(
                SpeechRequest(
                    site_id=request.receipt.site_id,
                    audio_ref=normalized.audio_ref,
                    source_evidence_ref=request.receipt.evidence_ref,
                    duration_ms=normalized.duration_ms,
                    language_hint=request.language_hint,
                ),
                idempotency_key=speech_key.key,
            )
        except SpeechRejected as exc:
            return MediaPipelineOutcome(
                status=self._exception_status(
                    retryable=exc.retryable,
                    attempt=request.attempt,
                    max_attempts=request.max_attempts,
                ),
                reason_codes=(exc.code,),
                stage_idempotency_keys=tuple(keys),
            )
        except Exception:
            return MediaPipelineOutcome(
                status=self._retry_or_dead_letter(request),
                reason_codes=("speech_provider_error",),
                stage_idempotency_keys=tuple(keys),
            )
        return MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=tuple(keys),
            transcript=transcript,
        )

    @staticmethod
    def _key(request: MediaPipelineRequest, stage: str) -> StageIdempotencyKey:
        return stage_idempotency_key(
            site_id=request.receipt.site_id,
            request_id=request.receipt.request_id,
            immutable_checksum=request.receipt.immutable_checksum,
            stage=stage,
        )

    @staticmethod
    def _terminal_status(
        status: PipelineStatus,
        *,
        attempt: int,
        max_attempts: int,
    ) -> PipelineStatus:
        if status is PipelineStatus.RETRY and attempt >= max_attempts:
            return PipelineStatus.DEAD_LETTER
        return status

    @staticmethod
    def _exception_status(
        *,
        retryable: bool,
        attempt: int,
        max_attempts: int,
    ) -> PipelineStatus:
        if not retryable:
            return PipelineStatus.QUARANTINED
        if attempt >= max_attempts:
            return PipelineStatus.DEAD_LETTER
        return PipelineStatus.RETRY

    @staticmethod
    def _retry_or_dead_letter(request: MediaPipelineRequest) -> PipelineStatus:
        if request.attempt >= request.max_attempts:
            return PipelineStatus.DEAD_LETTER
        return PipelineStatus.RETRY
