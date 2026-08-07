from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.evidence_store import ContentAddressedEvidenceStore
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
    StoredObject,
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


def _content_batch() -> tuple[
    tuple[ConnectorItem, ...],
    tuple[NormalizedObservationInput, ...],
]:
    items, normalized = _batch()
    value = normalized[0]
    return items, (
        NormalizedObservationInput(
            channel="email",
            participants=value.participants,
            evidence=(
                value.evidence[0],
                EvidenceArtifact(
                    media_type="text/plain; charset=utf-8",
                    locator="message-body",
                    role="derived-text",
                    content=b"private body evidence",
                ),
                EvidenceArtifact(
                    media_type="application/octet-stream",
                    locator="attachment:1",
                    role="attachment",
                    content=b"private attachment evidence",
                ),
            ),
            consent_basis=value.consent_basis,
            data_classification=value.data_classification,
            retention_class=value.retention_class,
            original_language=value.original_language,
            correlation_id=value.correlation_id,
        ),
    )


def test_sink_materializes_content_to_site_cas_before_storage(
    tmp_path: Path,
) -> None:
    storage = RecordingStorage()
    evidence_store = ContentAddressedEvidenceStore(tmp_path)
    sink = PostgresNormalizedObservationSink(
        storage=storage,
        evidence_store=evidence_store,
    )
    items, normalized = _content_batch()

    sink.accept_batch(SCOPE, ConnectorKey("email", "primary"), _job(), items, normalized)

    persisted = storage.calls[0][4][0]  # type: ignore[index]
    assert all(artifact.content is None for artifact in persisted.evidence)
    assert persisted.evidence[0] == normalized[0].evidence[0]
    for before, after in zip(
        normalized[0].evidence[1:],
        persisted.evidence[1:],
        strict=True,
    ):
        assert after.reference is not None
        assert evidence_store.read(SCOPE, after.reference) == before.content
    rendered = repr((sink, persisted))
    assert "private body evidence" not in rendered
    assert "private attachment evidence" not in rendered


def test_sink_rejects_content_without_evidence_store() -> None:
    storage = RecordingStorage()
    items, normalized = _content_batch()

    with pytest.raises(ValueError, match="evidence store"):
        PostgresNormalizedObservationSink(storage=storage).accept_batch(
            SCOPE,
            ConnectorKey("email", "primary"),
            _job(),
            items,
            normalized,
        )

    assert storage.calls == []


class _FailingEvidenceStore:
    def put(self, *args: object, **kwargs: object) -> object:
        raise OSError("private filesystem failure")


def test_sink_cas_failure_never_calls_database() -> None:
    storage = RecordingStorage()
    items, normalized = _content_batch()

    with pytest.raises(OSError, match="private filesystem failure"):
        PostgresNormalizedObservationSink(
            storage=storage,
            evidence_store=_FailingEvidenceStore(),  # type: ignore[arg-type]
        ).accept_batch(
            SCOPE,
            ConnectorKey("email", "primary"),
            _job(),
            items,
            normalized,
        )

    assert storage.calls == []


class _WrongReferenceEvidenceStore:
    def put(
        self,
        scope: TenantScope,
        content: bytes,
        *,
        media_type: str,
    ) -> StoredObject:
        del scope
        digest = hashlib.sha256(content).hexdigest()
        return StoredObject(
            object_ref=f"obs:v1:{'f' * 32}:sha256:{digest}",
            sha256=digest,
            size=len(content),
            media_type=media_type,
        )


def test_sink_rejects_wrong_site_cas_reference_without_database_call() -> None:
    storage = RecordingStorage()
    items, normalized = _content_batch()

    with pytest.raises(ValueError, match="integrity"):
        PostgresNormalizedObservationSink(
            storage=storage,
            evidence_store=_WrongReferenceEvidenceStore(),  # type: ignore[arg-type]
        ).accept_batch(
            SCOPE,
            ConnectorKey("email", "primary"),
            _job(),
            items,
            normalized,
        )

    assert storage.calls == []


class _FailOnceStorage(RecordingStorage):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def persist_normalized_batch(self, *args: object) -> PersistedNormalizedBatch:
        self.calls.append(args)
        if self.fail:
            self.fail = False
            raise RuntimeError("database unavailable")
        return PersistedNormalizedBatch(observations=())


def test_database_failure_replay_reuses_identical_cas_references(
    tmp_path: Path,
) -> None:
    storage = _FailOnceStorage()
    sink = PostgresNormalizedObservationSink(
        storage=storage,
        evidence_store=ContentAddressedEvidenceStore(tmp_path),
    )
    items, normalized = _content_batch()

    with pytest.raises(RuntimeError, match="database unavailable"):
        sink.accept_batch(
            SCOPE,
            ConnectorKey("email", "primary"),
            _job(),
            items,
            normalized,
        )
    sink.accept_batch(
        SCOPE,
        ConnectorKey("email", "primary"),
        _job(),
        items,
        normalized,
    )

    first = storage.calls[0][4]
    second = storage.calls[1][4]
    assert first == second
