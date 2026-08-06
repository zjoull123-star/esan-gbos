from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from observer.models import (
    ByteLocator,
    CanonicalObservation,
    EvidenceRecord,
    FactProposal,
    ImportResult,
    Participant,
    TenantScope,
)
from observer.storage import (
    CheckpointDisposition,
    IdempotencyConflict,
    PostgresObserverRepository,
    classify_checkpoint,
    fallback_dedup_key,
)

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services" / "observer" / "migrations" / "002_gate3_observer_runtime.sql"
NOW = datetime(2026, 8, 6, 8, 1, 42, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._many: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if (
            normalized.upper().startswith("SELECT")
            and "pg_advisory_xact_lock" not in normalized
            and "set_config(" not in normalized
        ):
            response = self.connection.responses.pop(0)
            if isinstance(response, list):
                self._many = response
                self._one = response[0] if response else None
            else:
                self._one = response
                self._many = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._many


class FakeConnection:
    def __init__(self, responses: list[tuple[Any, ...] | list[tuple[Any, ...]] | None]) -> None:
        self.responses = responses
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _result(
    *,
    event_id: str = "01K20B8BV5C6P4YFAT8YQ3D4S5",
    occurred_at: datetime = NOW,
    raw_sha256: str = "a" * 64,
) -> ImportResult:
    evidence_id = f"evidence-{event_id}"
    participant = Participant("external", "party:synthetic-001")
    observation = CanonicalObservation(
        event_id=event_id,
        site_id=SCOPE.site_id,
        processing_purpose=SCOPE.processing_purpose,
        connector="manual_import",
        channel="manual_import",
        occurred_at=occurred_at,
        ingested_at=occurred_at + timedelta(seconds=1),
        original_language="zh",
        participants=(participant,),
        evidence_refs=(evidence_id,),
        raw_sha256=raw_sha256,
        consent_basis="consent",
        data_classification="Restricted",
        retention_class="R1-operational",
        correlation_id=f"corr-{event_id}",
        source_lineage=("manual_import:fixture-001",),
        processor_version="manual-import-v1",
    )
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        observation_event_id=event_id,
        site_id=SCOPE.site_id,
        processing_purpose=SCOPE.processing_purpose,
        data_classification="Restricted",
        source_lineage=("manual_import:fixture-001", "member:message.txt"),
        processor_version="manual-import-v1",
        raw_sha256="b" * 64,
        object_ref=f"local-object://{evidence_id}",
        media_type="text/plain",
        locator=ByteLocator(0, 12),
        created_at=occurred_at + timedelta(seconds=1),
        retention_class="R1-operational",
    )
    fact = FactProposal(
        fact_id=f"fact-{event_id}",
        site_id=SCOPE.site_id,
        processing_purpose=SCOPE.processing_purpose,
        data_classification="Restricted",
        source_lineage=(event_id, evidence_id),
        processor_version="1.0.0",
        rule_version="sample-request-v1",
        output_version="fact-proposal-v1",
        subject_ref="party:synthetic-001",
        predicate="requested_sample",
        value="true",
        summary_zh="客户申请样品。",
        original_language="zh",
        confidence=1.0,
        evidence_refs=(evidence_id,),
        status="proposed",
        recorded_at=occurred_at + timedelta(seconds=1),
    )
    return ImportResult(observation, (evidence,), (fact,), ())


def test_runtime_migration_adds_fallback_dedup_and_checkpoint_lineage_without_gate4() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter table observer.observation_events" in sql
    assert "add column if not exists raw_sha256" in sql
    assert "add column if not exists occurred_minute" in sql
    assert "where provider_event_id is null" in sql
    assert "site_id, connector, raw_sha256, occurred_minute" in sql
    assert "add column if not exists cursor_occurred_at" in sql
    assert "add column if not exists last_event_id" in sql
    assert "add column if not exists result_event_id" in sql
    assert "add column if not exists checkpoint_disposition" in sql
    assert "add column if not exists job_id" in sql
    assert "add column if not exists evidence_ordinal" in sql
    assert "gbos_observer_app" in sql
    for forbidden in (
        "verified_fact",
        "decision",
        "approved_command",
        "kingdee",
        "model_call",
        "external_send",
    ):
        assert forbidden not in sql


def test_fallback_dedup_is_site_connector_digest_and_utc_minute_scoped() -> None:
    first = fallback_dedup_key(SCOPE, "manual_import", "a" * 64, NOW)
    same_minute = fallback_dedup_key(
        SCOPE,
        "manual_import",
        "a" * 64,
        NOW.replace(second=59),
    )

    assert first == same_minute
    assert first != fallback_dedup_key(
        TenantScope("beta.example", SCOPE.processing_purpose),
        "manual_import",
        "a" * 64,
        NOW,
    )
    assert first != fallback_dedup_key(
        SCOPE,
        "manual_import",
        "a" * 64,
        NOW + timedelta(minutes=1),
    )


def test_checkpoint_classification_is_monotonic_with_bounded_lateness() -> None:
    cursor = NOW

    assert classify_checkpoint(None, NOW - timedelta(days=1), 60) is CheckpointDisposition.ADVANCE
    assert classify_checkpoint(cursor, cursor, 60) is CheckpointDisposition.ADVANCE
    assert (
        classify_checkpoint(cursor, cursor - timedelta(seconds=59), 60)
        is CheckpointDisposition.LATE_WITHIN_WINDOW
    )
    assert (
        classify_checkpoint(cursor, cursor - timedelta(seconds=61), 60)
        is CheckpointDisposition.DEAD_LETTER
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        classify_checkpoint(cursor.replace(tzinfo=None), NOW, 60)


def test_repository_persists_complete_observer_lineage_in_one_scoped_transaction() -> None:
    connection = FakeConnection([None, None, None, None])
    repository = PostgresObserverRepository(connection)

    metadata = repository.persist(
        SCOPE,
        idempotency_key="import-001",
        payload_digest="a" * 64,
        result=_result(),
        provider_event_id="provider-001",
        checkpoint_id="manual-import-main",
        replay_window_seconds=60,
    )

    statements = "\n".join(sql for sql, _params in connection.executed).lower()
    assert connection.transactions == 1
    assert connection.executed[0] == (
        "SELECT set_config('app.site_id', %s, true)",
        ("alpha.example",),
    )
    assert any(
        params is not None and str(params[0]).startswith("checkpoint\x1f")
        for _sql, params in connection.executed
    )
    for table in (
        "manual_import_jobs",
        "raw_objects",
        "observation_events",
        "participants",
        "evidence_refs",
        "event_evidence",
        "checkpoints",
        "processor_runs",
        "derivation_edges",
    ):
        assert f"observer.{table}" in statements
    assert metadata.status == "stored"
    assert metadata.event_id == _result().observation.event_id
    assert metadata.evidence_ids == _result().observation.evidence_refs
    assert not hasattr(metadata, "document")
    assert not hasattr(metadata, "payload")


def test_repository_replay_conflicts_when_digest_changes() -> None:
    connection = FakeConnection(
        [
            (
                "b" * 64,
                "stored",
                "event-001",
                None,
                "advance",
                SCOPE.processing_purpose,
            )
        ]
    )
    repository = PostgresObserverRepository(connection)

    with pytest.raises(IdempotencyConflict, match="idempotency_conflict"):
        repository.persist(
            SCOPE,
            idempotency_key="import-001",
            payload_digest="a" * 64,
            result=_result(),
            provider_event_id=None,
            checkpoint_id="manual-import-main",
            replay_window_seconds=60,
        )

    assert connection.transactions == 1
    assert not any(sql.startswith("INSERT") for sql, _params in connection.executed)


def test_repository_never_replays_an_idempotency_key_across_purposes() -> None:
    connection = FakeConnection(
        [
            (
                "a" * 64,
                "stored",
                "event-001",
                None,
                "advance",
                "audit_compliance",
            )
        ]
    )
    repository = PostgresObserverRepository(connection)

    with pytest.raises(IdempotencyConflict, match="idempotency_conflict"):
        repository.persist(
            SCOPE,
            idempotency_key="import-001",
            payload_digest="a" * 64,
            result=_result(),
            provider_event_id=None,
            checkpoint_id="manual-import-main",
            replay_window_seconds=60,
        )


def test_repository_outside_replay_window_writes_dead_letter_only() -> None:
    cursor = NOW + timedelta(minutes=2)
    connection = FakeConnection([None, None, (cursor, 60)])
    repository = PostgresObserverRepository(connection)

    metadata = repository.persist(
        SCOPE,
        idempotency_key="import-old",
        payload_digest="a" * 64,
        result=_result(occurred_at=NOW),
        provider_event_id=None,
        checkpoint_id="manual-import-main",
        replay_window_seconds=60,
    )

    statements = "\n".join(sql for sql, _params in connection.executed).lower()
    assert metadata.status == "dead_letter"
    assert metadata.event_id is None
    assert metadata.dead_letter_reason == "outside_replay_window"
    assert "insert into observer.dead_letter" in statements
    assert "insert into observer.observation_events" not in statements


def test_repository_get_is_scoped_and_returns_none_for_cross_site() -> None:
    connection = FakeConnection([None])
    repository = PostgresObserverRepository(connection)

    assert (
        repository.get(
            TenantScope("other.example", SCOPE.processing_purpose),
            "event-001",
        )
        is None
    )
    assert connection.executed[0] == (
        "SELECT set_config('app.site_id', %s, true)",
        ("other.example",),
    )
