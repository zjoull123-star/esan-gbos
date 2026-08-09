from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.media_runtime.observer_bridge import (
    MediaEvidenceManifest,
    ObserverBridge,
    TranscriptReferenceSubmission,
)
from services.media_runtime.speech import TranscriptSegment, TranscriptSegments
from services.media_runtime.upload import SourceKind, UploadReceipt


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[TranscriptReferenceSubmission, str]] = []
        self.manifest_calls: list[tuple[MediaEvidenceManifest, str]] = []

    def submit_transcript_refs(
        self,
        submission: TranscriptReferenceSubmission,
        *,
        idempotency_key: str,
    ) -> None:
        self.calls.append((submission, idempotency_key))

    def submit_evidence_manifest(
        self,
        manifest: MediaEvidenceManifest,
        *,
        idempotency_key: str,
    ) -> None:
        self.manifest_calls.append((manifest, idempotency_key))


def _bridge(client: RecordingClient) -> ObserverBridge:
    return ObserverBridge(
        client=client,
        site_id="site-a",
        team_ref="team-sales",
    )


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
    bridge = _bridge(client)

    transcript_ref = bridge.publish(_transcript(), idempotency_key="publish:request-01")

    assert transcript_ref.startswith("transcript://")
    submission, key = client.calls[0]
    assert key == "publish:request-01"
    assert submission.site_id == "site-a"
    assert submission.team_ref == "team-sales"
    assert submission.source_evidence_ref == "evidence://site-a/source-01"
    assert submission.segments[0].start_ms == 10
    assert submission.segments[0].end_ms == 900
    assert submission.segments[0].text_ref == "localtext://site-a/text-01"
    assert not hasattr(submission, "text")
    assert not hasattr(submission, "command")
    assert not hasattr(submission, "action")


def test_bridge_rejects_invalid_ranges_before_client_call() -> None:
    client = RecordingClient()
    bridge = _bridge(client)
    invalid = _transcript()
    object.__setattr__(invalid.segments[0], "end_ms", 10)

    with pytest.raises(ValueError, match="invalid_transcript_range"):
        bridge.publish(invalid, idempotency_key="publish:request-01")

    assert client.calls == []


@pytest.mark.parametrize(
    ("field_name", "invalid_ref"),
    (
        ("source_evidence_ref", "s3://site-a/source-01"),
        ("text_ref", "custom://site-a/text-01"),
        ("evidence_ref", "https://site-a/source-01/range-10-900"),
    ),
)
def test_bridge_rejects_non_local_reference_schemes(
    field_name: str,
    invalid_ref: str,
) -> None:
    client = RecordingClient()
    bridge = _bridge(client)
    transcript = _transcript()
    target = transcript if field_name == "source_evidence_ref" else transcript.segments[0]
    object.__setattr__(target, field_name, invalid_ref)

    with pytest.raises(ValueError, match="ref_invalid"):
        bridge.publish(transcript, idempotency_key="publish:request-01")

    assert client.calls == []


def test_bridge_publishes_closed_refs_only_non_audio_evidence_manifest() -> None:
    client = RecordingClient()
    bridge = _bridge(client)
    receipt = UploadReceipt(
        receipt_id="receipt-SENSITIVE-01",
        site_id="site-a",
        purpose="document_review",
        request_id="request-SENSITIVE-01",
        source_kind=SourceKind.FILE,
        media_type="application/pdf",
        byte_size=123,
        sha256="a" * 64,
        object_ref="localobject://site-a/object-SENSITIVE-01",
        evidence_ref="evidence://site-a/evidence-SENSITIVE-01",
        received_at=datetime(2026, 8, 8, tzinfo=UTC),
        immutable_checksum="b" * 64,
    )

    manifest_ref = bridge.publish_media_evidence(
        receipt,
        idempotency_key="media-evidence:job-01",
    )

    assert manifest_ref.startswith("evidence://site-a/media-manifest-")
    manifest, key = client.manifest_calls[0]
    assert key == "media-evidence:job-01"
    assert manifest.site_id == "site-a"
    assert manifest.team_ref == "team-sales"
    assert manifest.source_object_ref == receipt.object_ref
    assert manifest.source_evidence_ref == receipt.evidence_ref
    assert manifest.content_sha256 == receipt.sha256
    assert manifest.manifest_ref == manifest_ref
    assert not hasattr(manifest, "body")
    assert not hasattr(manifest, "content")
    assert not hasattr(manifest, "bytes")
    assert not hasattr(manifest, "command")
    assert not hasattr(manifest, "action")
    assert "SENSITIVE" not in repr(manifest)


@pytest.mark.parametrize(
    ("field_name", "invalid_ref"),
    (
        ("object_ref", "s3://site-a/object-01"),
        ("object_ref", "localobject://other-site/object-01"),
        ("evidence_ref", "evidence://other-site/evidence-01"),
    ),
)
def test_bridge_rejects_non_local_or_cross_site_media_refs(
    field_name: str,
    invalid_ref: str,
) -> None:
    client = RecordingClient()
    bridge = _bridge(client)
    receipt = UploadReceipt(
        receipt_id="receipt-01",
        site_id="site-a",
        purpose="document_review",
        request_id="request-01",
        source_kind=SourceKind.FILE,
        media_type="application/pdf",
        byte_size=123,
        sha256="a" * 64,
        object_ref="object://site-a/object-01",
        evidence_ref="evidence://site-a/evidence-01",
        received_at=datetime(2026, 8, 8, tzinfo=UTC),
        immutable_checksum="b" * 64,
    )
    object.__setattr__(receipt, field_name, invalid_ref)

    with pytest.raises(ValueError, match="ref_invalid"):
        bridge.publish_media_evidence(
            receipt,
            idempotency_key="media-evidence:job-01",
        )

    assert client.manifest_calls == []
