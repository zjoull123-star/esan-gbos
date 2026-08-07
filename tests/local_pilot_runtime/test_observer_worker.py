from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.local_pilot_runtime.observer_worker import (
    ConnectorPipeline,
    ObserverConnectorWorker,
    main,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.local_pilot_storage import (
    InboundDeliveryMetadata,
    ProcessingJobMetadata,
)
from services.observer.observer.models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    TenantScope,
)

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


def _job(connector: str, *, instance: str = "primary") -> ProcessingJobMetadata:
    return ProcessingJobMetadata(
        site_id=SCOPE.site_id,
        job_id=f"job-{connector}",
        connector=connector,
        connector_instance_id=instance,
        delivery_id=f"delivery-{connector}",
        stage="normalize",
        status="processing",
        attempt_count=1,
        max_attempts=3,
        idempotency_key=f"delivery:{connector}",
        generation=0,
        lease_owner="observer-worker",
        lease_expires_at=NOW + timedelta(seconds=60),
        lease_generation=1,
        next_retry_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStorage:
    def __init__(
        self,
        *,
        job: ProcessingJobMetadata,
        delivery: InboundDeliveryMetadata,
    ) -> None:
        self.next_job: ProcessingJobMetadata | None = job
        self.delivery = delivery
        self.completed: list[tuple[str, ...]] = []
        self.quarantines: list[str] = []
        self.retries: list[str] = []

    def claim_processing_job(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata | None:
        assert scope == SCOPE
        claimed, self.next_job = self.next_job, None
        return claimed

    def get_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
    ) -> InboundDeliveryMetadata:
        assert scope == SCOPE
        assert delivery_id == self.delivery.delivery_id
        return self.delivery

    def heartbeat_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata:
        assert self.delivery.delivery_id
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
        )

    def complete_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        provider_event_ids: tuple[str, ...],
    ) -> ProcessingJobMetadata:
        self.completed.append(provider_event_ids)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="succeeded",
            lease_owner=None,
            lease_expires_at=None,
        )

    def quarantine_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        reason_code: str,
    ) -> ProcessingJobMetadata:
        self.quarantines.append(reason_code)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="quarantined",
            lease_owner=None,
            lease_expires_at=None,
        )

    def retry_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        next_retry_at: datetime,
        error_code: str,
    ) -> ProcessingJobMetadata:
        self.retries.append(error_code)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="retry_wait",
            lease_owner=None,
            lease_expires_at=None,
        )


class Decoder:
    def __init__(self, connector: str) -> None:
        self.connector = connector
        self.received: list[bytes] = []

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        self.received.append(exact_bytes)
        return (
            ConnectorItem(
                provider_event_id=f"{self.connector}-event-001",
                occurred_at=NOW,
                source_cursor="cursor-001",
                payload={"kind": self.connector},
            ),
        )


class Normalizer:
    def __init__(self, connector: str) -> None:
        self.connector = connector
        self.items: list[ConnectorItem] = []

    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        self.items.append(item)
        return NormalizedObservationInput(
            channel="email" if self.connector == "email" else "chat",
            participants=(
                Participant(
                    "unknown",
                    f"unresolved:delivery:{self.connector}-event-001",
                ),
            ),
            evidence=(
                EvidenceArtifact(
                    media_type="application/octet-stream",
                    locator="delivery",
                    role="source",
                    reference=source_ref,
                ),
            ),
            consent_basis="pilot_deferred_review",
            data_classification="Restricted",
            retention_class="R1-operational",
            original_language="und",
            correlation_id=f"corr-{self.connector}",
        )


class Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def accept_batch(self, *args: object) -> None:
        self.calls.append(args)


def _storage(
    tmp_path: Path,
    *,
    connector: str,
) -> tuple[FakeStorage, ContentAddressedEvidenceStore]:
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(
        SCOPE,
        f"private-{connector}-payload".encode(),
        media_type="application/octet-stream",
    )
    job = _job(connector)
    delivery = InboundDeliveryMetadata(
        site_id=SCOPE.site_id,
        connector=connector,
        connector_instance_id="primary",
        delivery_id=job.delivery_id,
        exact_body_sha256=stored.sha256,
        object_ref=stored.object_ref,
        byte_size=stored.size,
        media_type=stored.media_type,
        received_at=NOW,
        processing_status="processing",
        attempt_count=1,
        correlation_id=f"corr-{connector}",
        last_attempt_at=NOW,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return FakeStorage(job=job, delivery=delivery), evidence


@pytest.mark.parametrize("connector", ["whatsapp", "email", "wecom"])
def test_worker_dispatches_claimed_connector_and_restart_does_not_repeat(
    tmp_path: Path,
    connector: str,
) -> None:
    storage, evidence = _storage(tmp_path, connector=connector)
    decoder = Decoder(connector)
    normalizer = Normalizer(connector)
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            connector: ConnectorPipeline(
                decoder=decoder,
                normalizer=normalizer,
            )
        },
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    first = worker.run_once(SCOPE)
    restarted = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            connector: ConnectorPipeline(
                decoder=decoder,
                normalizer=normalizer,
            )
        },
        sink=sink,
        worker_id="observer-worker-restarted",
        clock=lambda: NOW,
    ).run_once(SCOPE)

    assert first is not None
    assert first.status == "succeeded"
    assert first.normalized_count == 1
    assert restarted is None
    assert len(decoder.received) == 1
    assert len(normalizer.items) == 1
    assert len(sink.calls) == 1
    assert storage.completed == [(f"{connector}-event-001",)]


def test_unknown_connector_is_quarantined_without_reading_or_sinking(
    tmp_path: Path,
) -> None:
    storage, evidence = _storage(tmp_path, connector="phone")
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={},
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None
    assert result.status == "quarantined"
    assert result.normalized_count == 0
    assert storage.quarantines == ["unsupported_connector"]
    assert storage.completed == []
    assert sink.calls == []
    assert "storage=<redacted>" in repr(worker)
    assert main() == 78
