from __future__ import annotations

from datetime import UTC, datetime, timedelta

from observer.local_pilot_sink import PostgresNormalizedObservationSink
from observer.local_pilot_storage import (
    PersistedNormalizedBatch,
    PersistedNormalizedObservation,
    ProcessingJobMetadata,
)
from observer.models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    TenantScope,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("whatsapp", "primary")


def _job() -> ProcessingJobMetadata:
    return ProcessingJobMetadata(
        site_id=SCOPE.site_id,
        job_id="job-001",
        connector=KEY.connector,
        connector_instance_id=KEY.instance_id,
        delivery_id="delivery-001",
        stage="normalize",
        status="processing",
        attempt_count=1,
        max_attempts=3,
        idempotency_key="job-key",
        generation=0,
        lease_owner="worker-a",
        lease_expires_at=NOW + timedelta(seconds=30),
        lease_generation=1,
        next_retry_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _batch() -> tuple[
    tuple[ConnectorItem, ...],
    tuple[NormalizedObservationInput, ...],
]:
    item = ConnectorItem(
        "provider-001",
        NOW,
        "cursor-001",
        {"body": "private body"},
    )
    normalized = NormalizedObservationInput(
        channel="chat",
        participants=(
            Participant(
                "unknown",
                "unresolved:delivery:" + "a" * 64,
            ),
        ),
        evidence=(
            EvidenceArtifact(
                media_type="application/json",
                locator="delivery",
                role="source",
                reference="obs:v1:site-partition:sha256:" + "b" * 64,
            ),
        ),
        consent_basis="pilot_deferred_review",
        data_classification="Restricted",
        retention_class="R1-operational",
        original_language="und",
        correlation_id="corr-001",
    )
    return (item,), (normalized,)


class RecordingStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def persist_normalized_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch:
        self.calls.append((scope, key, job, items, normalized))
        return PersistedNormalizedBatch(
            observations=(
                PersistedNormalizedObservation(
                    provider_event_id=items[0].provider_event_id,
                    event_id="event-001",
                    outbox_id="outbox-001",
                    payload_sha256="c" * 64,
                    replayed=False,
                ),
            ),
        )


def test_sink_has_one_batch_boundary_and_returns_storage_receipt_without_pii_repr() -> None:
    storage = RecordingStorage()
    sink = PostgresNormalizedObservationSink(storage=storage)
    items, normalized = _batch()

    result = sink.accept_batch(SCOPE, KEY, _job(), items, normalized)

    assert result.observations[0].event_id == "event-001"
    assert len(storage.calls) == 1
    assert storage.calls[0][3:] == (items, normalized)
    assert "private body" not in repr(sink)
