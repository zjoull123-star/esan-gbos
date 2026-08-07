from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from services.media_runtime.common import PipelineStatus
from services.media_runtime.ffmpeg import FFmpegRejected, FFmpegRequest, NormalizedAudio
from services.media_runtime.inspection import InspectionResult, MediaAsset
from services.media_runtime.pipeline import MediaPipeline, MediaPipelineRequest
from services.media_runtime.speech import SpeechRejected, TranscriptSegments
from services.media_runtime.upload import SourceKind, UploadReceipt


class RecordingPreprocessor:
    def __init__(self, result: InspectionResult) -> None:
        self.result = result
        self.calls: list[tuple[MediaAsset, str]] = []

    def inspect(self, asset: MediaAsset, *, idempotency_key: str) -> InspectionResult:
        self.calls.append((asset, idempotency_key))
        return self.result


class RecordingNormalizer:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[FFmpegRequest, str]] = []

    def normalize(
        self,
        request: FFmpegRequest,
        *,
        idempotency_key: str,
    ) -> NormalizedAudio:
        self.calls.append((request, idempotency_key))
        if self.failure is not None:
            raise self.failure
        return NormalizedAudio(
            audio_ref="localmedia://normalized/request-01.wav",
            media_type="audio/wav",
            byte_size=64_044,
            content_sha256=hashlib.sha256(b"verified-pipeline-audio").hexdigest(),
            duration_ms=request.duration_ms,
            codec="pcm_s16le",
            channels=1,
            sample_rate=16_000,
            executable_name="ffmpeg",
            executable_version="ffmpeg-local-v1",
            executable_sha256=hashlib.sha256(b"bound-pipeline-ffmpeg").hexdigest(),
        )


class RecordingSpeech:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[object, str]] = []

    def transcribe(self, request: object, *, idempotency_key: str) -> TranscriptSegments:
        self.calls.append((request, idempotency_key))
        if self.failure is not None:
            raise self.failure
        return TranscriptSegments(
            transcript_id="transcript-01",
            site_id="site-a",
            source_evidence_ref="evidence://site-a/upload-01",
            model_provider="local_faster_whisper",
            model_name="large-v3-turbo",
            model_version="large-v3-turbo-ct2-local-v1",
            model_sha256=hashlib.sha256(b"bound-pipeline-model").hexdigest(),
            language="en",
            segments=(),
            generated_at=datetime(2026, 8, 7, 3, 1, tzinfo=UTC),
        )


def _receipt(*, media_type: str = "audio/wav") -> UploadReceipt:
    return UploadReceipt(
        receipt_id="upload-01",
        site_id="site-a",
        purpose="observation_processing",
        request_id="request-01",
        source_kind=SourceKind.MEETING,
        media_type=media_type,
        byte_size=10,
        sha256="a" * 64,
        object_ref="object://site-a/upload-01",
        evidence_ref="evidence://site-a/upload-01",
        received_at=datetime(2026, 8, 7, 3, tzinfo=UTC),
        immutable_checksum="b" * 64,
    )


def _request(
    *,
    attempt: int = 1,
    max_attempts: int = 3,
    media_type: str = "audio/wav",
) -> MediaPipelineRequest:
    return MediaPipelineRequest(
        receipt=_receipt(media_type=media_type),
        source_path="/media/input/object-01",
        output_path="/media/output/request-01.wav",
        duration_ms=2_000,
        channels=2,
        sample_rate=48_000,
        attempt=attempt,
        max_attempts=max_attempts,
        language_hint="en",
    )


def test_pipeline_request_repr_excludes_local_source_and_output_paths() -> None:
    request = _request()

    assert "/media/input/object-01" not in repr(request)
    assert "/media/output/request-01.wav" not in repr(request)


@pytest.mark.parametrize(
    ("inspection_status", "expected_status"),
    (
        (PipelineStatus.QUARANTINED, PipelineStatus.QUARANTINED),
        (PipelineStatus.RETRY, PipelineStatus.RETRY),
    ),
)
def test_failed_inspection_never_calls_normalization_or_speech(
    inspection_status: PipelineStatus,
    expected_status: PipelineStatus,
) -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=inspection_status,
            detected_mime="audio/wav",
            reason_codes=("inspection_failure",),
        )
    )
    normalizer = RecordingNormalizer()
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=normalizer,
        speech_provider=speech,
    ).run(_request())

    assert outcome.status is expected_status
    assert normalizer.calls == []
    assert speech.calls == []
    assert len(outcome.stage_idempotency_keys) == 1


def test_retry_exhaustion_becomes_dead_letter_without_downstream_calls() -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.RETRY,
            detected_mime="audio/wav",
            reason_codes=("scanner_unavailable",),
        )
    )
    normalizer = RecordingNormalizer()
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=normalizer,
        speech_provider=speech,
    ).run(_request(attempt=3, max_attempts=3))

    assert outcome.status is PipelineStatus.DEAD_LETTER
    assert normalizer.calls == []
    assert speech.calls == []


def test_preprocessor_exception_is_retryable_and_never_calls_downstream() -> None:
    class FailingPreprocessor:
        def inspect(
            self,
            asset: MediaAsset,
            *,
            idempotency_key: str,
        ) -> InspectionResult:
            del asset, idempotency_key
            raise RuntimeError("inspection-SENTINEL")

    normalizer = RecordingNormalizer()
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=FailingPreprocessor(),
        normalizer=normalizer,
        speech_provider=speech,
    ).run(_request())

    assert outcome.status is PipelineStatus.RETRY
    assert outcome.reason_codes == ("preprocessor_error",)
    assert normalizer.calls == []
    assert speech.calls == []


def test_clean_non_audio_file_is_ready_without_ffmpeg_or_speech() -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.READY,
            detected_mime="application/pdf",
            reason_codes=(),
        )
    )
    normalizer = RecordingNormalizer()
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=normalizer,
        speech_provider=speech,
    ).run(_request(media_type="application/pdf"))

    assert outcome.status is PipelineStatus.READY
    assert outcome.transcript is None
    assert normalizer.calls == []
    assert speech.calls == []
    assert [entry.stage for entry in outcome.stage_idempotency_keys] == ["inspect"]


@pytest.mark.parametrize(
    ("failure", "attempt", "expected"),
    (
        (SpeechRejected("invalid_time_range"), 1, PipelineStatus.QUARANTINED),
        (
            SpeechRejected("speech_engine_unavailable", retryable=True),
            1,
            PipelineStatus.RETRY,
        ),
        (
            SpeechRejected("speech_engine_unavailable", retryable=True),
            3,
            PipelineStatus.DEAD_LETTER,
        ),
    ),
)
def test_speech_failure_is_terminal_and_status_is_explicit(
    failure: Exception,
    attempt: int,
    expected: PipelineStatus,
) -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.READY,
            detected_mime="audio/wav",
            reason_codes=(),
        )
    )

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=RecordingNormalizer(),
        speech_provider=RecordingSpeech(failure=failure),
    ).run(_request(attempt=attempt, max_attempts=3))

    assert outcome.status is expected
    assert len(outcome.stage_idempotency_keys) == 3


def test_ready_pipeline_uses_stable_distinct_idempotency_key_for_every_stage() -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.READY,
            detected_mime="audio/wav",
            reason_codes=(),
        )
    )
    normalizer = RecordingNormalizer()
    speech = RecordingSpeech()
    pipeline = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=normalizer,
        speech_provider=speech,
    )

    first = pipeline.run(_request())
    second = pipeline.run(_request())

    assert first.status is PipelineStatus.READY
    assert first.transcript is not None
    assert first.stage_idempotency_keys == second.stage_idempotency_keys
    assert len(first.stage_idempotency_keys) == 3
    assert {entry.stage for entry in first.stage_idempotency_keys} == {
        "inspect",
        "normalize",
        "transcribe",
    }
    assert len({entry.key for entry in first.stage_idempotency_keys}) == 3
    assert len(preprocessor.calls) == 2
    assert len(normalizer.calls) == 2
    assert len(speech.calls) == 2


@pytest.mark.parametrize(
    ("attempt", "expected"),
    (
        (1, PipelineStatus.RETRY),
        (3, PipelineStatus.DEAD_LETTER),
    ),
)
def test_normalization_failure_never_calls_speech(
    attempt: int,
    expected: PipelineStatus,
) -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.READY,
            detected_mime="audio/wav",
            reason_codes=(),
        )
    )
    normalizer = RecordingNormalizer(failure=FFmpegRejected("ffmpeg_failed", retryable=True))
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=normalizer,
        speech_provider=speech,
    ).run(_request(attempt=attempt, max_attempts=3))

    assert outcome.status is expected
    assert speech.calls == []
    assert len(outcome.stage_idempotency_keys) == 2


def test_invalid_ffmpeg_output_proof_quarantines_and_never_calls_speech() -> None:
    preprocessor = RecordingPreprocessor(
        InspectionResult(
            status=PipelineStatus.READY,
            detected_mime="audio/wav",
            reason_codes=(),
        )
    )
    speech = RecordingSpeech()

    outcome = MediaPipeline(
        preprocessor=preprocessor,
        normalizer=RecordingNormalizer(failure=FFmpegRejected("ffmpeg_output_invalid")),
        speech_provider=speech,
    ).run(_request())

    assert outcome.status is PipelineStatus.QUARANTINED
    assert outcome.reason_codes == ("ffmpeg_output_invalid",)
    assert speech.calls == []
