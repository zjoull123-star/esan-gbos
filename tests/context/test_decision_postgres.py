from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest

from services.context.context_service.decision import StaleRevision
from services.context.context_service.decision_postgres import PostgresDecisionStorage


class FakeCursor:
    def __init__(self, handler: Any, statements: list[tuple[str, tuple[Any, ...] | None]]) -> None:
        self.handler = handler
        self.statements = statements
        self.one: tuple[Any, ...] | None = None
        self.many: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        self.one, self.many = self.handler(normalized, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.many


class FakeConnection:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.transactions = 0

    def transaction(self) -> Any:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.handler, self.statements)


def proposal_document() -> dict[str, Any]:
    return {
        "fact": {
            "subject_ref": "contact-SYNTH-001",
            "predicate": "requested_quantity",
            "value": {"type": "number", "number": 1000, "unit": "pcs"},
            "evidence_refs": ["evidence-record-SYNTH-001"],
        },
        "valid_time": {"start": "2026-08-06T01:59:00Z", "end": None},
        "recorded_time": "2026-08-06T02:00:04Z",
        "source_lineage": {
            "source_system": "manual_import",
            "source_record_refs": ["event-SYNTH-001"],
            "retrieved_at": "2026-08-06T02:00:03Z",
            "transformation_version": "fact-proposal-v1",
            "evidence_status": "synthetic",
        },
    }


def decision_document() -> dict[str, Any]:
    return {
        "decision_id": "decision-SYNTH-001",
        "site_id": "gbos.localhost",
        "processing_purpose": "business_operations",
        "decision_revision": 1,
        "decision_type": "human",
        "proposal_ref": "fact-proposal-SYNTH-001",
        "proposal_version": "fact-proposal-v1",
        "proposal_revision": 1,
        "input_fact_refs": [],
        "output_fact_refs": [{"fact_id": "verified-fact-SYNTH-001", "fact_version": 1}],
        "evidence_refs": ["evidence-record-SYNTH-001"],
        "operator": "reviewer-SYNTH-001",
        "effective_at": "2026-08-06T03:05:00Z",
        "valid_time": {"start": "2026-08-06T01:59:00Z", "end": None},
        "recorded_time": "2026-08-06T03:05:00Z",
    }


def fact_document() -> dict[str, Any]:
    return {
        "fact_id": "verified-fact-SYNTH-001",
        "fact_version": 1,
        "site_id": "gbos.localhost",
        "processing_purpose": "business_operations",
        "proposal_ref": "fact-proposal-SYNTH-001",
        "proposal_version": "fact-proposal-v1",
        "proposal_revision": 1,
        "subject_ref": "contact-SYNTH-001",
        "predicate": "requested_quantity",
        "confirmation_decision_ref": "decision-SYNTH-001",
        "evidence_refs": ["evidence-record-SYNTH-001"],
        "valid_time": {"start": "2026-08-06T01:59:00Z", "end": None},
        "recorded_time": "2026-08-06T03:05:00Z",
    }


def test_get_proposal_returns_exact_snapshot_and_sets_tenant_scope() -> None:
    document = proposal_document()

    def handler(sql: str, _params: object) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]]]:
        if "FROM context.fact_proposals" in sql:
            return (
                (
                    "business_operations",
                    "fact-proposal-v1",
                    1,
                    "contact-SYNTH-001",
                    "requested_quantity",
                    json.dumps(document),
                    "a" * 64,
                    datetime(2026, 8, 6, 2, 0, 4, tzinfo=UTC),
                ),
                [],
            )
        if "FROM context.fact_evidence" in sql:
            return None, [("evidence-record-SYNTH-001",)]
        return None, []

    connection = FakeConnection(handler)
    snapshot = PostgresDecisionStorage(connection).get_proposal(
        "gbos.localhost",
        "fact-proposal-SYNTH-001",
    )

    assert snapshot is not None
    assert snapshot.proposal_version == "fact-proposal-v1"
    assert snapshot.proposal_revision == 1
    assert snapshot.evidence_refs == ("evidence-record-SYNTH-001",)
    assert connection.statements[0] == (
        "SELECT set_config('app.site_id', %s, true)",
        ("gbos.localhost",),
    )


def test_save_confirmation_is_one_transaction_with_exact_optimistic_checks() -> None:
    def handler(sql: str, _params: object) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]]]:
        if "FROM context.fact_proposals" in sql:
            return ("fact-proposal-v1", 1), []
        if "SELECT current.fact_id" in sql:
            return None, []
        return None, []

    connection = FakeConnection(handler)
    storage = PostgresDecisionStorage(connection)
    storage.save_confirmation(
        decision=decision_document(),
        fact=fact_document(),
        expected_proposal_version="fact-proposal-v1",
        expected_proposal_revision=1,
        expected_current_fact_ref=None,
        expected_current_fact_version=None,
    )

    statements = [sql for sql, _params in connection.statements]
    assert connection.transactions == 1
    assert statements[0] == "SELECT set_config('app.site_id', %s, true)"
    decision_insert = next(
        index for index, sql in enumerate(statements) if "INSERT INTO context.decisions" in sql
    )
    fact_insert = next(
        index for index, sql in enumerate(statements) if "INSERT INTO context.verified_facts" in sql
    )
    assert decision_insert < fact_insert
    assert any("INSERT INTO context.decision_evidence_refs" in sql for sql in statements)
    assert any("INSERT INTO context.fact_evidence_refs" in sql for sql in statements)
    assert any("pg_advisory_xact_lock" in sql for sql in statements)
    assert not any("UPDATE context.fact_proposals" in sql for sql in statements)
    assert not any("DELETE FROM context.fact_proposals" in sql for sql in statements)


def test_save_confirmation_rejects_a_stale_proposal_before_insert() -> None:
    def handler(sql: str, _params: object) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]]]:
        if "FROM context.fact_proposals" in sql:
            return ("fact-proposal-v2", 2), []
        return None, []

    connection = FakeConnection(handler)
    with pytest.raises(StaleRevision):
        PostgresDecisionStorage(connection).save_confirmation(
            decision=decision_document(),
            fact=fact_document(),
            expected_proposal_version="fact-proposal-v1",
            expected_proposal_revision=1,
            expected_current_fact_ref=None,
            expected_current_fact_version=None,
        )

    assert not any("INSERT INTO" in sql for sql, _params in connection.statements)


def test_save_confirmation_rejects_a_different_current_fact_with_same_version() -> None:
    def handler(
        sql: str,
        _params: object,
    ) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]]]:
        if "FROM context.fact_proposals" in sql:
            return ("fact-proposal-v1", 1), []
        if "SELECT current.fact_id" in sql:
            return ("verified-fact-OTHER", 1), []
        return None, []

    connection = FakeConnection(handler)
    with pytest.raises(StaleRevision, match="current fact"):
        PostgresDecisionStorage(connection).save_confirmation(
            decision=decision_document(),
            fact=fact_document(),
            expected_proposal_version="fact-proposal-v1",
            expected_proposal_revision=1,
            expected_current_fact_ref="verified-fact-SYNTH-000",
            expected_current_fact_version=1,
        )

    assert not any("INSERT INTO" in sql for sql, _params in connection.statements)


def test_every_public_method_uses_transaction_local_site_scope() -> None:
    def handler(_sql: str, _params: object) -> tuple[None, list[tuple[Any, ...]]]:
        return None, []

    connection = FakeConnection(handler)
    storage = PostgresDecisionStorage(connection)

    assert storage.get_evidence("site-a", "evidence-1") is None
    assert storage.get_fact("site-a", "fact-1", 1) is None
    assert storage.get_current_fact("site-a", "subject-1", "predicate") is None
    assert storage.get_decision("site-a", "decision-1") is None

    scope_statements = [
        params
        for sql, params in connection.statements
        if sql == "SELECT set_config('app.site_id', %s, true)"
    ]
    assert scope_statements == [("site-a",)] * 4
