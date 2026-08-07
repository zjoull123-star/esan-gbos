from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import pytest
from observer.evidence_store import ContentAddressedEvidenceStore, SiteIsolationError
from observer.local_pilot_ingestion import (
    DeliveryIntegrityError,
    DeliveryQuarantine,
    DeliveryWorker,
    DurableDeliveryInbox,
)
from observer.local_pilot_storage import InboundDeliveryMetadata, ProcessingJobMetadata
from observer.models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    RawDelivery,
    TenantScope,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
OTHER_SCOPE = TenantScope("beta.example", "observation_processing")
KEY = ConnectorKey("wecom", "sales-primary")


def _delivery(*, object_ref: str, size: int, digest: str) -> InboundDeliveryMetadata:
    return InboundDeliveryMetadata(
        site_id=SCOPE.site_id,
        connector=KEY.connector,
        connector_instance_id=KEY.instance_id,
        delivery_id="delivery-001",
        exact_body_sha256=digest,
        object_ref=object_ref,
        byte_size=size,
        media_type="application/octet-stream",
        received_at=NOW,
        processing_status="queued",
        attempt_count=0,
        correlation_id="corr-001",
        last_attempt_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _job(*, status: str = "processing") -> ProcessingJobMetadata:
    return ProcessingJobMetadata(
        site_id=SCOPE.site_id,
        job_id="job-001",
        connector=KEY.connector,
        connector_instance_id=KEY.instance_id,
        delivery_id="delivery-001",
        stage="normalize",
        status=status,
        attempt_count=1,
        max_attempts=3,
        idempotency_key="delivery:wecom:sales-primary:delivery-001:g0",
        generation=0,
        lease_owner="worker-a" if status == "processing" else None,
        lease_expires_at=NOW + timedelta(seconds=30) if status == "processing" else None,
        lease_generation=1,
        next_retry_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


class InboxStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def accept_and_enqueue_delivery(self, scope: TenantScope, key: ConnectorKey, **kwargs: object):
        self.calls.append({"scope": scope, "key": key, **kwargs})
        if self.fail:
            raise RuntimeError("database unavailable")
        return (
            _delivery(
                object_ref=str(kwargs["object_ref"]),
                size=int(kwargs["byte_size"]),
                digest=str(kwargs["exact_body_sha256"]),
            ),
            _job(status="queued"),
        )


def test_durable_inbox_stores_exact_binary_then_atomically_registers_and_enqueues(
    tmp_path: Path,
) -> None:
    exact = b"\xff\x00\xfe\x81binary"
    evidence = ContentAddressedEvidenceStore(tmp_path)
    storage = InboxStorage()
    inbox = DurableDeliveryInbox(storage=storage, evidence_store=evidence)

    accepted = inbox.accept(
        SCOPE,
        KEY,
        RawDelivery("delivery-001", exact, "application/octet-stream", NOW),
        correlation_id="corr-001",
        max_attempts=3,
    )
    replay = inbox.accept(
        SCOPE,
        KEY,
        RawDelivery("delivery-001", exact, "application/octet-stream", NOW),
        correlation_id="corr-001",
        max_attempts=3,
    )

    assert accepted.delivery.exact_body_sha256 == hashlib.sha256(exact).hexdigest()
    assert evidence.read(SCOPE, accepted.delivery.object_ref) == exact
    assert replay.job.idempotency_key == accepted.job.idempotency_key
    assert len(storage.calls) == 2


def test_durable_inbox_does_not_report_acceptance_when_database_fails(tmp_path: Path) -> None:
    exact = b"\xffnot-utf8"
    evidence = ContentAddressedEvidenceStore(tmp_path)
    storage = InboxStorage(fail=True)
    inbox = DurableDeliveryInbox(storage=storage, evidence_store=evidence)

    with pytest.raises(RuntimeError, match="database unavailable"):
        inbox.accept(
            SCOPE,
            KEY,
            RawDelivery("delivery-001", exact, "application/octet-stream", NOW),
            correlation_id="corr-001",
        )

    call = storage.calls[0]
    assert evidence.read(SCOPE, str(call["object_ref"])) == exact


class WorkerStorage(Protocol):
    claimed: ProcessingJobMetadata
    delivery: InboundDeliveryMetadata
    completed_event_ids: tuple[str, ...]
    retries: list[str]
    quarantines: list[str]


@dataclass
class FakeWorkerStorage:
    claimed: ProcessingJobMetadata
    delivery: InboundDeliveryMetadata
    completed_event_ids: tuple[str, ...] = ()
    retries: list[str] | None = None
    quarantines: list[str] | None = None

    def __post_init__(self) -> None:
        self.retries = []
        self.quarantines = []

    def claim_processing_job(self, *_args: object, **_kwargs: object) -> ProcessingJobMetadata:
        return self.claimed

    def get_inbound_delivery(self, *_args: object, **_kwargs: object) -> InboundDeliveryMetadata:
        return self.delivery

    def heartbeat_processing_job(self, *_args: object, **_kwargs: object) -> ProcessingJobMetadata:
        return self.claimed

    def complete_processing_job(
        self, *_args: object, provider_event_ids: tuple[str, ...], **_kwargs: object
    ) -> ProcessingJobMetadata:
        self.completed_event_ids = provider_event_ids
        return _job(status="succeeded")

    def retry_processing_job(
        self, *_args: object, error_code: str, **_kwargs: object
    ) -> ProcessingJobMetadata:
        assert self.retries is not None
        self.retries.append(error_code)
        return _job(status="retry_wait")

    def quarantine_processing_job(
        self, *_args: object, reason_code: str, **_kwargs: object
    ) -> ProcessingJobMetadata:
        assert self.quarantines is not None
        self.quarantines.append(reason_code)
        return _job(status="quarantined")


class BinaryDecoder:
    def __init__(self, items: tuple[ConnectorItem, ...]) -> None:
        self.items = items
        self.received: list[bytes] = []

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        self.received.append(exact_bytes)
        return self.items


class Normalizer:
    def __init__(self) -> None:
        self.items: list[ConnectorItem] = []

    def normalize(self, item: ConnectorItem) -> NormalizedObservationInput:
        self.items.append(item)
        return NormalizedObservationInput(
            channel="chat",
            participants=(Participant("external", "party:opaque"),),
            evidence=(
                EvidenceArtifact(
                    media_type="application/octet-stream",
                    locator="delivery:opaque",
                    role="source",
                    reference="obs:opaque",
                ),
            ),
            consent_basis="pilot_deferred_review",
            data_classification="Restricted",
            retention_class="pilot",
            original_language="und",
            correlation_id="corr-001",
        )


class Sink:
    def __init__(self) -> None:
        self.normalized: list[NormalizedObservationInput] = []

    def accept(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        item: ConnectorItem,
        normalized: NormalizedObservationInput,
    ) -> None:
        assert scope == SCOPE
        assert key == KEY
        assert job.job_id == "job-001"
        assert item.provider_event_id
        self.normalized.append(normalized)


def _items() -> tuple[ConnectorItem, ...]:
    return (
        ConnectorItem("event-001", NOW, "cursor:1", {"opaque": 1}),
        ConnectorItem("event-002", NOW, "cursor:2", {"opaque": 2}),
    )


def test_worker_reads_site_scoped_binary_decodes_many_items_and_completes(tmp_path: Path) -> None:
    exact = b"\xff\xfe\x00payload"
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(SCOPE, exact, media_type="application/octet-stream")
    storage = FakeWorkerStorage(
        claimed=_job(),
        delivery=_delivery(object_ref=stored.object_ref, size=stored.size, digest=stored.sha256),
    )
    decoder = BinaryDecoder(_items())
    normalizer = Normalizer()
    sink = Sink()
    worker = DeliveryWorker(
        storage=storage,
        evidence_store=evidence,
        decoder=decoder,
        normalizer=normalizer,
        sink=sink,
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None
    assert result.status == "succeeded"
    assert result.normalized_count == 2
    assert decoder.received == [exact]
    assert len(normalizer.items) == 2
    assert len(sink.normalized) == 2
    assert storage.completed_event_ids == ("event-001", "event-002")


def test_worker_checks_scope_hash_and_size_before_decode(tmp_path: Path) -> None:
    exact = b"binary"
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(SCOPE, exact, media_type="application/octet-stream")
    decoder = BinaryDecoder(_items())
    storage = FakeWorkerStorage(
        claimed=_job(),
        delivery=_delivery(object_ref=stored.object_ref, size=999, digest=stored.sha256),
    )
    worker = DeliveryWorker(
        storage=storage,
        evidence_store=evidence,
        decoder=decoder,
        normalizer=Normalizer(),
        sink=Sink(),
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None
    assert result.status == "quarantined"
    assert storage.quarantines == ["evidence_integrity_mismatch"]
    assert decoder.received == []
    with pytest.raises(SiteIsolationError):
        evidence.read(OTHER_SCOPE, stored.object_ref)


class QuarantineDecoder:
    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        del exact_bytes
        raise DeliveryQuarantine("unsupported_envelope")


def test_worker_quarantines_explicit_decoder_rejection_and_retries_transient_failure(
    tmp_path: Path,
) -> None:
    exact = b"binary"
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(SCOPE, exact, media_type="application/octet-stream")
    delivery = _delivery(object_ref=stored.object_ref, size=stored.size, digest=stored.sha256)
    quarantine_storage = FakeWorkerStorage(claimed=_job(), delivery=delivery)
    quarantining = DeliveryWorker(
        storage=quarantine_storage,
        evidence_store=evidence,
        decoder=QuarantineDecoder(),
        normalizer=Normalizer(),
        sink=Sink(),
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    quarantined = quarantining.run_once(SCOPE)

    assert quarantined is not None
    assert quarantined.status == "quarantined"
    assert quarantine_storage.quarantines == ["unsupported_envelope"]

    transient_storage = FakeWorkerStorage(claimed=_job(), delivery=delivery)
    retrying = DeliveryWorker(
        storage=transient_storage,
        evidence_store=evidence,
        decoder=BinaryDecoder(_items()),
        normalizer=Normalizer(),
        sink=_FailingSink(),
        worker_id="worker-a",
        clock=lambda: NOW,
    )
    retried = retrying.run_once(SCOPE)
    assert retried is not None
    assert retried.status == "retry_wait"
    assert transient_storage.retries == ["delivery_processing_failed"]


class _FailingSink:
    def accept(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("transient sink failure")


def test_worker_rejects_wrong_site_or_instance_metadata(tmp_path: Path) -> None:
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(SCOPE, b"binary", media_type="application/octet-stream")
    wrong_delivery = _delivery(
        object_ref=stored.object_ref,
        size=stored.size,
        digest=stored.sha256,
    )
    object.__setattr__(wrong_delivery, "connector_instance_id", "other-instance")
    storage = FakeWorkerStorage(claimed=_job(), delivery=wrong_delivery)
    worker = DeliveryWorker(
        storage=storage,
        evidence_store=evidence,
        decoder=BinaryDecoder(_items()),
        normalizer=Normalizer(),
        sink=Sink(),
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    with pytest.raises(DeliveryIntegrityError, match="scope"):
        worker.run_once(SCOPE)
