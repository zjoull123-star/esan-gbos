from __future__ import annotations

import re
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

from services.context.context_service.models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    TenantScope,
    ValidationError,
)
from services.context.context_service.repositories import InMemoryContextRepository
from services.context.context_service.storage import PostgresContextRepository

ROOT = Path(__file__).parents[2]
CONTEXT_MIGRATIONS = ROOT / "services" / "context" / "migrations"
EXPECTED_TABLES = {
    "evidence_records",
    "fact_proposals",
    "fact_evidence",
    "entity_resolution_proposals",
    "candidates",
    "restrictions",
    "inbox_messages",
}
GATE4_NAMES = {
    "verified_facts",
    "conflicts",
    "decisions",
    "actions",
    "draft_mutations",
    "approved_commands",
    "review_cases",
    "agent_tasks",
}


def _migration_sql() -> str:
    paths = sorted(CONTEXT_MIGRATIONS.glob("*.sql"))
    assert paths, "Context Service must provide plain SQL migrations"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()


def _table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"create\s+table\s+if\s+not\s+exists\s+context\.{table}\s*\((.*?)\);",
        sql,
        re.DOTALL,
    )
    assert match, f"missing CREATE TABLE for context.{table}"
    return match.group(1)


def _payload(record_id: str, *, kind: RecordKind) -> dict[str, object]:
    base: dict[str, object] = {
        "site_id": "site-a",
        "processing_purpose": "observation_processing",
    }
    if kind is RecordKind.EVIDENCE:
        base["evidence_record_id"] = record_id
    elif kind is RecordKind.FACT_PROPOSAL:
        base["fact_proposal_record_id"] = record_id
        base["fact"] = {"status": "proposed"}
    else:
        base["entity_resolution_proposal_id"] = record_id
        base["processing_purpose"] = "entity_resolution"
        base["status"] = "proposed"
    return base


def _envelope(record_id: str, *, kind: RecordKind, key: str = "key-1") -> GovernedEnvelope:
    payload = _payload(record_id, kind=kind)
    return GovernedEnvelope.from_payload(
        site_id="site-a",
        processing_purpose=str(payload["processing_purpose"]),
        idempotency_key=key,
        payload=payload,
    )


def test_context_migration_creates_gate3_tables_with_composite_site_keys() -> None:
    sql = _migration_sql()
    created_tables = set(
        re.findall(r"create\s+table\s+if\s+not\s+exists\s+context\.([a-z_]+)", sql)
    )

    assert created_tables == EXPECTED_TABLES
    for table in EXPECTED_TABLES:
        body = _table_body(sql, table)
        assert re.search(r"primary\s+key\s*\(\s*site_id\s*,", body), table
    for forbidden in GATE4_NAMES:
        assert f"context.{forbidden}" not in sql


def test_context_foreign_keys_and_rls_are_site_scoped() -> None:
    sql = _migration_sql()
    foreign_keys = re.findall(
        r"foreign\s+key\s*\(([^)]+)\)\s+references\s+(?:observer|context)\.[a-z_]+\s*"
        r"\(([^)]+)\)",
        sql,
    )

    assert foreign_keys
    assert all("site_id" in local and "site_id" in remote for local, remote in foreign_keys)
    for table in EXPECTED_TABLES:
        assert f"alter table context.{table} enable row level security" in sql
        assert f"alter table context.{table} force row level security" in sql
        assert f"create policy {table}_site_isolation" in sql


def test_context_has_a_dedicated_non_bypass_runtime_role_and_repeatable_policies() -> None:
    sql = _migration_sql()

    assert "gbos_context_app" in sql
    assert "gbos_observer_app" not in sql
    assert "nobypassrls" in sql
    assert "nosuperuser" in sql
    assert "grant select, insert, update, delete on all tables" not in sql
    for table in EXPECTED_TABLES:
        assert f"drop policy if exists {table}_site_isolation on context.{table}" in sql


def test_context_database_constraints_keep_proposals_proposed() -> None:
    sql = _migration_sql()

    assert re.search(
        r"fact_proposals.*?status\s+text\s+not\s+null\s+default\s+'proposed'.*?"
        r"check\s*\(\s*status\s*=\s*'proposed'\s*\)",
        sql,
        re.DOTALL,
    )
    assert re.search(
        r"entity_resolution_proposals.*?status\s+text\s+not\s+null\s+default\s+'proposed'.*?"
        r"check\s*\(\s*status\s*=\s*'proposed'\s*\)",
        sql,
        re.DOTALL,
    )


def test_postgres_repository_sets_transaction_local_site_and_uses_gate3_tables() -> None:
    storage_source = (ROOT / "services" / "context" / "context_service" / "storage.py").read_text(
        encoding="utf-8"
    )

    assert "set_config('app.site_id', %s, true)" in storage_source
    assert "context.record_metadata" not in storage_source
    assert all(
        f"context.{table}" in storage_source
        for table in (
            "evidence_records",
            "fact_proposals",
            "entity_resolution_proposals",
        )
    )


def test_tenant_scope_rejects_empty_or_unknown_purpose() -> None:
    with pytest.raises(ValidationError):
        TenantScope(site_id="", processing_purpose="observation_processing")
    with pytest.raises(ValidationError):
        TenantScope(site_id="site-a", processing_purpose="arbitrary_model_use")


def test_governed_envelope_rejects_payload_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="payload_digest"):
        GovernedEnvelope(
            site_id="site-a",
            processing_purpose="observation_processing",
            idempotency_key="key-1",
            payload_digest="0" * 64,
            payload=_payload("fact-1", kind=RecordKind.FACT_PROPOSAL),
        )


@pytest.mark.parametrize(
    ("kind", "status_path"),
    [
        (RecordKind.FACT_PROPOSAL, ("fact", "status")),
        (RecordKind.ENTITY_RESOLUTION_PROPOSAL, ("status",)),
    ],
)
def test_repository_rejects_non_proposed_records(
    kind: RecordKind, status_path: tuple[str, ...]
) -> None:
    payload = _payload("record-1", kind=kind)
    target = payload
    for part in status_path[:-1]:
        target = target[part]  # type: ignore[assignment,index]
    target[status_path[-1]] = "confirmed"  # type: ignore[index]
    envelope = GovernedEnvelope.from_payload(
        site_id="site-a",
        processing_purpose=str(payload["processing_purpose"]),
        idempotency_key="key-1",
        payload=payload,
    )

    with pytest.raises(ValidationError, match="proposed"):
        InMemoryContextRepository().save(
            TenantScope("site-a", str(payload["processing_purpose"])),
            kind,
            envelope,
        )


def test_repository_requires_matching_site_and_purpose() -> None:
    repository = InMemoryContextRepository()
    envelope = _envelope("fact-1", kind=RecordKind.FACT_PROPOSAL)

    with pytest.raises(ValidationError, match="site"):
        repository.save(
            TenantScope("site-b", "observation_processing"),
            RecordKind.FACT_PROPOSAL,
            envelope,
        )
    with pytest.raises(ValidationError, match="purpose"):
        repository.save(
            TenantScope("site-a", "audit_compliance"),
            RecordKind.FACT_PROPOSAL,
            envelope,
        )


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "verified_fact",
        "decision",
        "action",
        "draft_mutation",
        "approved_command",
        "review_case",
        "conflict",
    ],
)
def test_repository_rejects_gate4_fields(forbidden_field: str) -> None:
    payload = _payload("fact-1", kind=RecordKind.FACT_PROPOSAL)
    payload[forbidden_field] = {"status": "forbidden"}
    envelope = GovernedEnvelope.from_payload(
        site_id="site-a",
        processing_purpose="observation_processing",
        idempotency_key="key-1",
        payload=payload,
    )

    with pytest.raises(ValidationError, match="Gate 4"):
        InMemoryContextRepository().save(
            TenantScope("site-a", "observation_processing"),
            RecordKind.FACT_PROPOSAL,
            envelope,
        )


def test_repository_idempotency_returns_same_metadata_and_rejects_conflict() -> None:
    repository = InMemoryContextRepository()
    scope = TenantScope("site-a", "observation_processing")
    first = _envelope("fact-1", kind=RecordKind.FACT_PROPOSAL)

    first_result = repository.save(scope, RecordKind.FACT_PROPOSAL, first)
    replay_result = repository.save(scope, RecordKind.FACT_PROPOSAL, first)

    assert replay_result == first_result
    conflict = _envelope("fact-2", kind=RecordKind.FACT_PROPOSAL)
    with pytest.raises(IdempotencyConflict):
        repository.save(scope, RecordKind.FACT_PROPOSAL, conflict)


def test_repository_exposes_metadata_only_and_denies_cross_site_reads() -> None:
    repository = InMemoryContextRepository()
    scope = TenantScope("site-a", "observation_processing")
    envelope = _envelope("evidence-1", kind=RecordKind.EVIDENCE)

    metadata = repository.save(scope, RecordKind.EVIDENCE, envelope)

    assert metadata.record_id == "evidence-1"
    assert not hasattr(metadata, "payload")
    assert repository.get(scope, RecordKind.EVIDENCE, "evidence-1") == metadata
    assert (
        repository.get(
            TenantScope("site-b", "observation_processing"),
            RecordKind.EVIDENCE,
            "evidence-1",
        )
        is None
    )


class _Transaction(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _RecordingCursor:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.rows = rows
        self.executions: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executions.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0)


class _RecordingConnection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.cursor_instance = _RecordingCursor(rows)
        self.transaction_count = 0

    def transaction(self) -> _Transaction:
        self.transaction_count += 1
        return _Transaction()

    def cursor(self) -> _RecordingCursor:
        return self.cursor_instance


def _created_row(kind: RecordKind, record_id: str) -> tuple[Any, ...]:
    return (
        kind.value,
        record_id,
        "observation_processing",
        f"key-{record_id}",
        "a" * 64,
        datetime(2026, 8, 6, tzinfo=UTC),
    )


def test_postgres_repository_saves_fact_evidence_in_parent_transaction() -> None:
    connection = _RecordingConnection(
        [None, _created_row(RecordKind.FACT_PROPOSAL, "fact-record-1")]
    )
    payload = _payload("fact-record-1", kind=RecordKind.FACT_PROPOSAL)
    payload["fact"] = {
        "status": "proposed",
        "subject_ref": "contact-1",
        "predicate": "communication_summary",
        "confidence": 1.0,
        "evidence_refs": ["evidence-record-1", "evidence-record-2"],
    }
    envelope = GovernedEnvelope.from_payload(
        site_id="site-a",
        processing_purpose="observation_processing",
        idempotency_key="key-fact-record-1",
        payload=payload,
    )

    PostgresContextRepository(connection).save(
        TenantScope("site-a", "observation_processing"),
        RecordKind.FACT_PROPOSAL,
        envelope,
    )

    child_inserts = [
        (sql, params)
        for sql, params in connection.cursor_instance.executions
        if "INSERT INTO context.fact_evidence" in sql
    ]
    assert connection.transaction_count == 1
    assert [params for _, params in child_inserts] == [
        ("site-a", "fact-record-1", "evidence-record-1"),
        ("site-a", "fact-record-1", "evidence-record-2"),
    ]


def test_postgres_repository_saves_entity_candidates_in_parent_transaction() -> None:
    connection = _RecordingConnection(
        [
            None,
            _created_row(
                RecordKind.ENTITY_RESOLUTION_PROPOSAL,
                "entity-proposal-1",
            ),
        ]
    )
    payload = _payload(
        "entity-proposal-1",
        kind=RecordKind.ENTITY_RESOLUTION_PROPOSAL,
    )
    payload["processing_purpose"] = "observation_processing"
    payload["entity_type"] = "Contact"
    payload["source_entity_ref"] = "observed-contact-1"
    payload["candidates"] = [
        {
            "entity_ref": "contact-1",
            "confidence": 0.75,
            "matching_attributes": ["external_reference"],
        },
        {
            "entity_ref": "contact-2",
            "confidence": 0.5,
            "matching_attributes": ["normalized_name"],
        },
    ]
    envelope = GovernedEnvelope.from_payload(
        site_id="site-a",
        processing_purpose="observation_processing",
        idempotency_key="key-entity-proposal-1",
        payload=payload,
    )

    PostgresContextRepository(connection).save(
        TenantScope("site-a", "observation_processing"),
        RecordKind.ENTITY_RESOLUTION_PROPOSAL,
        envelope,
    )

    child_inserts = [
        (sql, params)
        for sql, params in connection.cursor_instance.executions
        if "INSERT INTO context.candidates" in sql
    ]
    assert connection.transaction_count == 1
    assert [params for _, params in child_inserts] == [
        (
            "site-a",
            "entity-proposal-1",
            "candidate-0001",
            "contact-1",
            0.75,
            '["external_reference"]',
        ),
        (
            "site-a",
            "entity-proposal-1",
            "candidate-0002",
            "contact-2",
            0.5,
            '["normalized_name"]',
        ),
    ]
