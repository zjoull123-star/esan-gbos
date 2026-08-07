from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from services.media_runtime.speech import (
    FASTER_WHISPER_VERSION,
    WHISPER_MODEL_NAME,
    WHISPER_MODEL_SHA256,
    EngineConfig,
    EngineSegment,
    EngineTranscript,
    FasterWhisperAdapter,
    SpeechRejected,
    SpeechRequest,
)


class StaticEngine:
    def __init__(
        self,
        transcript: EngineTranscript,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.transcript = transcript
        self.failure = failure
        self.calls: list[tuple[str, EngineConfig, str | None]] = []

    def transcribe(
        self,
        audio_ref: str,
        *,
        config: EngineConfig,
        language_hint: str | None,
    ) -> EngineTranscript:
        self.calls.append((audio_ref, config, language_hint))
        if self.failure is not None:
            raise self.failure
        return self.transcript


class RecordingTextStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def store(self, text: str, *, idempotency_key: str) -> str:
        self.calls.append((text, idempotency_key))
        return f"localtext://site-a/{idempotency_key}"


class RecordingEvidenceFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int, str]] = []

    def for_time_range(
        self,
        source_evidence_ref: str,
        *,
        start_ms: int,
        end_ms: int,
        idempotency_key: str,
    ) -> str:
        self.calls.append((source_evidence_ref, start_ms, end_ms, idempotency_key))
        return f"{source_evidence_ref}/t/{start_ms}-{end_ms}"


def _engine_segment(**changes: object) -> EngineSegment:
    values: dict[str, object] = {
        "start_seconds": 0.0,
        "end_seconds": 1.25,
        "text": "controlled transcript SENTINEL",
        "confidence": 0.92,
        "speaker_label": "Speaker 1",
        "speaker_confidence": 0.9,
    }
    values.update(changes)
    return EngineSegment(**values)  # type: ignore[arg-type]


def _adapter(
    transcript: EngineTranscript,
    *,
    failure: Exception | None = None,
) -> tuple[
    FasterWhisperAdapter,
    StaticEngine,
    RecordingTextStore,
    RecordingEvidenceFactory,
]:
    engine = StaticEngine(transcript, failure=failure)
    texts = RecordingTextStore()
    evidence = RecordingEvidenceFactory()
    return (
        FasterWhisperAdapter(
            engine=engine,
            text_store=texts,
            evidence_factory=evidence,
            clock=lambda: datetime(2026, 8, 7, 3, 1, tzinfo=UTC),
            transcript_id_factory=lambda: "transcript-01",
        ),
        engine,
        texts,
        evidence,
    )


def _request() -> SpeechRequest:
    return SpeechRequest(
        site_id="site-a",
        audio_ref="localmedia://normalized/request-01.wav",
        source_evidence_ref="evidence://site-a/upload-01",
        duration_ms=2_000,
        language_hint="en",
    )


def test_local_whisper_is_fixed_offline_read_only_and_emits_time_evidence_refs() -> None:
    adapter, engine, texts, evidence = _adapter(
        EngineTranscript(language="en", segments=(_engine_segment(),))
    )

    transcript = adapter.transcribe(_request(), idempotency_key="speech:01")

    assert len(engine.calls) == 1
    _audio_ref, config, language_hint = engine.calls[0]
    assert config.model_name == WHISPER_MODEL_NAME == "large-v3-turbo"
    assert config.model_sha256 == WHISPER_MODEL_SHA256
    assert config.engine_version == FASTER_WHISPER_VERSION
    assert config.model_mount == "/models/large-v3-turbo"
    assert config.read_only_model_mount is True
    assert config.offline is True
    assert config.allow_runtime_download is False
    assert language_hint == "en"
    assert transcript.model_name == "large-v3-turbo"
    assert transcript.model_sha256 == WHISPER_MODEL_SHA256
    assert transcript.language == "en"
    assert transcript.segments[0].start_ms == 0
    assert transcript.segments[0].end_ms == 1_250
    assert transcript.segments[0].speaker == "Speaker 1"
    assert transcript.segments[0].text_ref == "localtext://site-a/speech:01:text:000001"
    assert transcript.segments[0].evidence_ref.endswith("/t/0-1250")
    assert texts.calls == [("controlled transcript SENTINEL", "speech:01:text:000001")]
    assert evidence.calls == [
        ("evidence://site-a/upload-01", 0, 1_250, "speech:01:evidence:000001")
    ]
    assert "controlled transcript SENTINEL" not in repr(transcript)
    assert "controlled transcript SENTINEL" not in repr(_engine_segment())


@pytest.mark.parametrize(
    ("speaker_label", "speaker_confidence", "expected"),
    (
        ("Alice Example", 0.99, "unknown"),
        ("contact:123", 0.99, "unknown"),
        ("Speaker 1", 0.49, "unknown"),
        ("Speaker 2", 0.9, "Speaker 2"),
        (None, None, "unknown"),
    ),
)
def test_speaker_labels_are_generic_and_never_contact_linked(
    speaker_label: str | None,
    speaker_confidence: float | None,
    expected: str,
) -> None:
    adapter, _engine, _texts, _evidence = _adapter(
        EngineTranscript(
            language="en",
            segments=(
                _engine_segment(
                    speaker_label=speaker_label,
                    speaker_confidence=speaker_confidence,
                ),
            ),
        )
    )

    transcript = adapter.transcribe(_request(), idempotency_key="speech:01")

    assert transcript.segments[0].speaker == expected
    assert "Alice Example" not in repr(transcript)
    assert "contact:123" not in repr(transcript)


@pytest.mark.parametrize(
    ("segments", "code"),
    (
        (
            (_engine_segment(start_seconds=1.0, end_seconds=0.5),),
            "invalid_time_range",
        ),
        (
            (
                _engine_segment(start_seconds=0.0, end_seconds=1.0),
                _engine_segment(start_seconds=0.5, end_seconds=1.5),
            ),
            "overlapping_segments",
        ),
        (
            (_engine_segment(start_seconds=0.0, end_seconds=2.001),),
            "segment_exceeds_audio",
        ),
        (
            (_engine_segment(start_seconds=float("nan")),),
            "non_finite_segment",
        ),
        (
            (_engine_segment(confidence=float("nan")),),
            "invalid_confidence",
        ),
        (
            (_engine_segment(confidence=1.1),),
            "invalid_confidence",
        ),
        (
            (_engine_segment(text="x" * 4_001),),
            "segment_text_too_long",
        ),
    ),
)
def test_invalid_engine_segments_are_rejected_before_text_or_evidence_storage(
    segments: tuple[EngineSegment, ...],
    code: str,
) -> None:
    adapter, _engine, texts, evidence = _adapter(EngineTranscript(language="en", segments=segments))

    with pytest.raises(SpeechRejected, match=code) as caught:
        adapter.transcribe(_request(), idempotency_key="speech:01")

    assert texts.calls == []
    assert evidence.calls == []
    assert "x" * 20 not in repr(caught.value)


def test_engine_failure_is_retryable_generic_and_redacted() -> None:
    adapter, _engine, texts, evidence = _adapter(
        EngineTranscript(language="en", segments=()),
        failure=RuntimeError("engine-SENTINEL secret path"),
    )

    with pytest.raises(SpeechRejected, match="speech_engine_unavailable") as caught:
        adapter.transcribe(_request(), idempotency_key="speech:01")

    assert caught.value.retryable is True
    assert "engine-SENTINEL" not in repr(caught.value)
    assert texts.calls == []
    assert evidence.calls == []


@pytest.mark.parametrize(
    ("unsafe_text_ref", "unsafe_evidence_ref", "code"),
    (
        (
            "/tmp/raw-transcript-SENTINEL.txt",
            "evidence://site-a/upload-01/t/0-1250",
            "text_ref_invalid",
        ),
        (
            "localtext://site-a/transcript-01/segment-000001",
            "/tmp/evidence-SENTINEL.json",
            "evidence_ref_invalid",
        ),
    ),
)
def test_unsafe_output_references_are_rejected_without_echoing_values(
    unsafe_text_ref: str,
    unsafe_evidence_ref: str,
    code: str,
) -> None:
    class UnsafeTextStore(RecordingTextStore):
        def store(self, text: str, *, idempotency_key: str) -> str:
            self.calls.append((text, idempotency_key))
            return unsafe_text_ref

    class UnsafeEvidenceFactory(RecordingEvidenceFactory):
        def for_time_range(
            self,
            source_evidence_ref: str,
            *,
            start_ms: int,
            end_ms: int,
            idempotency_key: str,
        ) -> str:
            self.calls.append((source_evidence_ref, start_ms, end_ms, idempotency_key))
            return unsafe_evidence_ref

    engine = StaticEngine(EngineTranscript(language="en", segments=(_engine_segment(),)))
    adapter = FasterWhisperAdapter(
        engine=engine,
        text_store=UnsafeTextStore(),
        evidence_factory=UnsafeEvidenceFactory(),
        clock=lambda: datetime(2026, 8, 7, 3, 1, tzinfo=UTC),
        transcript_id_factory=lambda: "transcript-01",
    )

    with pytest.raises(SpeechRejected, match=code) as caught:
        adapter.transcribe(_request(), idempotency_key="speech:01")

    assert "SENTINEL" not in repr(caught.value)


def test_transcript_contract_contains_references_not_text_or_identity_mapping() -> None:
    adapter, _engine, _texts, _evidence = _adapter(
        EngineTranscript(language="en", segments=(_engine_segment(),))
    )

    payload = adapter.transcribe(_request(), idempotency_key="speech:01").to_contract()
    segments = cast(list[dict[str, object]], payload["segments"])

    assert payload["model"] == {
        "provider": "local_faster_whisper",
        "name": WHISPER_MODEL_NAME,
        "version": FASTER_WHISPER_VERSION,
        "sha256": WHISPER_MODEL_SHA256,
    }
    assert "text" not in segments[0]
    assert "contact_id" not in segments[0]
    assert "speaker_mapping" not in payload


def test_empty_speech_idempotency_key_is_rejected_before_engine() -> None:
    adapter, engine, _texts, _evidence = _adapter(EngineTranscript(language="en", segments=()))

    with pytest.raises(SpeechRejected, match="idempotency_key_required"):
        adapter.transcribe(_request(), idempotency_key="")

    assert engine.calls == []
