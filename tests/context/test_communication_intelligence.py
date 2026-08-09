from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.context.context_service.communication_intelligence import (
    CommunicationDraftClaim,
    CommunicationIntelligenceConflict,
    PostgresCommunicationIntelligenceRepository,
)
from services.observer.observer.model_projection import ContextIntelligencePublication
from services.observer.observer.models import TenantScope

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


def _publication(*, team_ref: str | None = "team-sales") -> ContextIntelligencePublication:
    return ContextIntelligencePublication(
        site_id=SCOPE.site_id,
        observation_id="event-001",
        team_ref=team_ref,
        evidence_refs=("evidence-001", "evidence-002"),
        summary_zh="客户希望确认样品交期。",
        original_language="zh-CN",
        confidence=0.93,
        review_status="AI Draft",
        fact_proposals=(
            {
                "subject_ref": "party-001",
                "predicate": "sample_delivery_intent",
                "value_display": "希望确认样品交期",
                "type": "text",
                "unit": None,
                "confidence": 0.91,
                "evidence_refs": ["evidence-001"],
                "status": "proposed",
            },
        ),
        association_suggestions=(
            {
                "type": "party",
                "target_ref": "party-001",
                "confidence": 0.88,
                "evidence_refs": ["evidence-002"],
            },
        ),
        model={"name": "deepseek-v4-flash", "version": "deepseek-v4-flash"},
        invocation_refs=("invocation-001",),
    )


class _Cursor:
    def __init__(self, existing_digest: str | None = None) -> None:
        self.existing_digest = existing_digest
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self._next: tuple[Any, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.calls.append((sql, params))
        if "SELECT payload_digest" in sql:
            self._next = None if self.existing_digest is None else (self.existing_digest,)

    def fetchone(self) -> tuple[Any, ...] | None:
        value = self._next
        self._next = None
        return value

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _Connection:
    def __init__(self, existing_digest: str | None = None) -> None:
        self.cursor_value = _Cursor(existing_digest)
        self.transactions = 0

    def transaction(self) -> Any:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> _Cursor:
        return self.cursor_value


def _repository(
    connection: _Connection,
    *,
    resolved_team: str | None = "team-sales",
) -> PostgresCommunicationIntelligenceRepository:
    return PostgresCommunicationIntelligenceRepository(
        connection,
        team_ref_resolver=lambda scope, observation_id: (
            resolved_team if scope == SCOPE and observation_id == "event-001" else None
        ),
    )


def test_publish_relationalizes_governed_intelligence_and_draft_in_one_transaction() -> None:
    connection = _Connection()
    repository = _repository(connection)
    publication = _publication()

    repository.publish(
        SCOPE,
        publication,
        idempotency_key="context-normalized:event-001",
    )

    assert connection.transactions == 1
    statements = "\n".join(sql for sql, _ in connection.cursor_value.calls)
    for table in (
        "context.communication_intelligence",
        "context.communication_intelligence_evidence",
        "context.communication_intelligence_invocations",
        "context.communication_fact_proposals",
        "context.communication_fact_evidence",
        "context.communication_association_suggestions",
        "context.communication_association_evidence",
        "context.communication_draft_outbox",
        "context.communication_draft_evidence",
    ):
        assert f"INSERT INTO {table}" in statements
    lowered = statements.casefold()
    for forbidden in ("raw_prompt", "raw_response", "mapping", "prompt_text"):
        assert forbidden not in lowered
    outbox = next(
        params
        for sql, params in connection.cursor_value.calls
        if "INSERT INTO context.communication_draft_outbox" in sql
    )
    assert outbox is not None
    assert "Communication event event-001" in outbox
    assert "客户希望确认样品交期。" in outbox
    assert "team-sales" in outbox
    outbox_sql = next(
        sql
        for sql, _ in connection.cursor_value.calls
        if "INSERT INTO context.communication_draft_outbox" in sql
    )
    assert "'AI'" in outbox_sql
    assert "'AI Draft'" in outbox_sql
    assert "FALSE" in outbox_sql


def test_nullable_trusted_team_persists_intelligence_without_draft_outbox() -> None:
    connection = _Connection()
    repository = _repository(connection, resolved_team=None)

    repository.publish(
        SCOPE,
        _publication(team_ref=None),
        idempotency_key="context-normalized:event-001",
    )

    statements = "\n".join(sql for sql, _ in connection.cursor_value.calls)
    assert "INSERT INTO context.communication_intelligence" in statements
    assert "INSERT INTO context.communication_draft_outbox" not in statements


def test_trusted_team_resolver_may_fill_null_but_rejects_publication_mismatch() -> None:
    connection = _Connection()
    repository = _repository(connection, resolved_team="team-trusted")
    repository.publish(
        SCOPE,
        _publication(team_ref=None),
        idempotency_key="context-normalized:event-001",
    )
    outbox = next(
        params
        for sql, params in connection.cursor_value.calls
        if "INSERT INTO context.communication_draft_outbox" in sql
    )
    assert outbox is not None and "team-trusted" in outbox

    with pytest.raises(ValueError, match="team"):
        repository.publish(
            SCOPE,
            _publication(team_ref="team-model-controlled"),
            idempotency_key="context-normalized:event-002",
        )


def test_publish_rejects_scope_or_nested_evidence_mismatch_before_transaction() -> None:
    connection = _Connection()
    repository = _repository(connection)
    with pytest.raises(ValueError, match="site"):
        repository.publish(
            TenantScope("other.example", SCOPE.processing_purpose),
            _publication(),
            idempotency_key="context-normalized:event-001",
        )
    with pytest.raises(ValueError, match="purpose"):
        repository.publish(
            TenantScope(SCOPE.site_id, "business_operations"),
            _publication(),
            idempotency_key="context-normalized:event-001",
        )
    tampered = _publication()
    tampered.fact_proposals[0]["evidence_refs"] = ["evidence-other"]
    with pytest.raises(ValueError, match="evidence"):
        repository.publish(
            SCOPE,
            tampered,
            idempotency_key="context-normalized:event-001",
        )
    assert connection.transactions == 0


def test_idempotent_replay_accepts_same_digest_and_rejects_changed_payload() -> None:
    first = _Connection()
    repository = _repository(first)
    repository.publish(
        SCOPE,
        _publication(),
        idempotency_key="context-normalized:event-001",
    )
    main_params = next(
        params
        for sql, params in first.cursor_value.calls
        if "INSERT INTO context.communication_intelligence (" in sql
    )
    assert main_params is not None
    digest = next(value for value in main_params if isinstance(value, str) and len(value) == 64)

    same = _Connection(existing_digest=digest)
    _repository(same).publish(
        SCOPE,
        _publication(),
        idempotency_key="context-normalized:event-001",
    )
    assert not any(
        "INSERT INTO context.communication_intelligence (" in sql
        for sql, _ in same.cursor_value.calls
    )

    changed = _Connection(existing_digest="f" * 64)
    with pytest.raises(CommunicationIntelligenceConflict, match="idempotency"):
        _repository(changed).publish(
            SCOPE,
            _publication(),
            idempotency_key="context-normalized:event-001",
        )


def test_claim_repr_redacts_summary_evidence_and_model_identity() -> None:
    claim = CommunicationDraftClaim(
        site_id=SCOPE.site_id,
        draft_id="draft-001",
        intelligence_id="intelligence-001",
        observation_id="event-001",
        processing_purpose=SCOPE.processing_purpose,
        subject="Communication event event-001",
        summary_zh="客户希望确认样品交期。",
        team_ref="team-sales",
        evidence_refs=("evidence-001",),
        model_name="deepseek-v4-flash",
        model_version="deepseek-v4-flash",
        payload_digest="a" * 64,
        attempt=1,
        max_attempts=5,
        lease_owner="worker-001",
        lease_expires_at=NOW + timedelta(seconds=30),
    )

    rendered = repr(claim)
    for secret in (
        claim.summary_zh,
        claim.evidence_refs[0],
        claim.model_name,
        claim.team_ref,
    ):
        assert secret not in rendered
    assert "<redacted>" in rendered


def test_migration_is_relational_fenced_rls_and_contains_no_raw_model_storage() -> None:
    sql = (
        ROOT
        / "services"
        / "context"
        / "migrations"
        / "005_local_pilot_communication_intelligence.sql"
    ).read_text(encoding="utf-8")
    lowered = sql.casefold()
    for table in (
        "communication_intelligence",
        "communication_intelligence_evidence",
        "communication_intelligence_invocations",
        "communication_fact_proposals",
        "communication_fact_evidence",
        "communication_association_suggestions",
        "communication_association_evidence",
        "communication_draft_outbox",
        "communication_draft_evidence",
    ):
        assert f"context.{table}" in lowered
    for status in ("pending", "running", "retry", "succeeded", "dead_letter"):
        assert f"'{status}'" in lowered
    assert "max_attempts <= 5" in lowered
    assert "force row level security" in lowered
    assert "new.lease_expires_at > old.lease_expires_at" in lowered
    assert "new.attempt = old.attempt" in lowered
    assert "processing_purpose = 'observation_processing'" in lowered
    assert "grant delete" not in lowered
    for forbidden in ("raw_prompt", "raw_response", "mapping_digest", "prompt_text"):
        assert forbidden not in lowered
