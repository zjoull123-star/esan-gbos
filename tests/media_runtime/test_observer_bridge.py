from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.media_runtime.observer_bridge import (
    ObserverBridge,
    TranscriptReferenceSubmission,
)
from services.media_runtime.speech import TranscriptSegment, TranscriptSegments


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[TranscriptReferenceSubmission, str]] = []

    def submit_transcript_refs(
        self,
        submission: TranscriptReferenceSubmission,
        *,
        idempotency_key: str,
    ) -> None:
        self.calls.append((submission, idempotency_key))


def _transcript() -> TranscriptSegments:
    return TranscriptSegments(
        transcript_id="transcript_01",
        site_id="site-a",
        source_evidence_ref="evidence://site-a/source-01",
        model_provider="local_faster_whisper",
        model_name="large-v3-turbo",
        model_version="large-v3-turbo-ct2-local-v1",
        model_sha256="b" * 64,
        language="en",
        segments=(
            TranscriptSegment(
                segment_id="segment_000001",
                start_ms=10,
                end_ms=900,
                speaker="Unknown",
                confidence=0.9,
                text_ref="localtext://site-a/text-01",
                evidence_ref="evidence://site-a/source-01/range-10-900",
            ),
        ),
        generated_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def test_bridge_submits_only_refs_and_millisecond_ranges() -> None:
    client = RecordingClient()
    bridge = ObserverBridge(client=client)

    transcript_ref = bridge.publish(_transcript(), idempotency_key="publish:request-01")

    assert transcript_ref.startswith("transcript://")
    submission, key = client.calls[0]
    assert key == "publish:request-01"
    assert submission.site_id == "site-a"
    assert submission.source_evidence_ref == "evidence://site-a/source-01"
    assert submission.segments[0].start_ms == 10
    assert submission.segments[0].end_ms == 900
    assert submission.segments[0].text_ref == "localtext://site-a/text-01"
    assert not hasattr(submission, "text")
    assert not hasattr(submission, "command")
    assert not hasattr(submission, "action")


def test_bridge_rejects_invalid_ranges_before_client_call() -> None:
    client = RecordingClient()
    bridge = ObserverBridge(client=client)
    invalid = _transcript()
    object.__setattr__(invalid.segments[0], "end_ms", 10)

    with pytest.raises(ValueError, match="invalid_transcript_range"):
        bridge.publish(invalid, idempotency_key="publish:request-01")

    assert client.calls == []
