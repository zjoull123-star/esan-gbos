from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.agent_runtime import (
    AgentTaskSubmission,
    PostgresAgentTaskRepository,
    TaskStatus,
)
from services.context.context_service.decision import (
    ConfirmationRequest,
    DecisionKind,
    DecisionService,
    TraceIntegrityError,
)
from services.context.context_service.decision_postgres import PostgresDecisionStorage
from services.context.context_service.storage import connect_postgres_components

pytestmark = pytest.mark.postgres_integration


def enabled() -> bool:
    return os.getenv("GBOS_RUN_GATE4_POSTGRES_INTEGRATION") == "1"


def connection(user_env: str):
    if not enabled():
        pytest.skip("set GBOS_RUN_GATE4_POSTGRES_INTEGRATION=1 for Gate 4 PostgreSQL tests")
    return connect_postgres_components(
        host=os.environ["GBOS_GATE4_POSTGRES_HOST"],
        port=int(os.environ["GBOS_GATE4_POSTGRES_PORT"]),
        database=os.environ["GBOS_GATE4_POSTGRES_DATABASE"],
        user=os.environ[user_env],
        password=os.environ["GBOS_GATE4_POSTGRES_PASSWORD"],
    )


def submission(site_id: str, suffix: str, *, max_attempts: int = 2) -> AgentTaskSubmission:
    now = datetime.now(UTC)
    return AgentTaskSubmission(
        task_id=f"task-{suffix}",
        site_id=site_id,
        processing_purpose="business_operations",
        idempotency_key=f"idem-{suffix}",
        agent_type="sales",
        subject_type="CRM Deal",
        subject_ref=f"deal-{suffix}",
        due_at=now,
        priority=50,
        max_attempts=max_attempts,
        causation_id=f"cause-{suffix}",
        correlation_id=f"corr-{suffix}",
        payload={"mode": "synthetic"},
    )


def test_gate4_agent_role_is_rls_scoped_and_queue_lifecycle_is_durable() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-{suffix}.localhost"
    other_site = f"gate4-other-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = submission(site_id, suffix)
        now = request.due_at
        created = repository.enqueue(request, now=now)
        replay = repository.enqueue(request, now=now)
        assert replay == created
        assert repository.get(other_site, created.task_id) is None

        leased = repository.claim(
            site_id,
            worker_id="gate4-worker-1",
            now=now + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )
        assert leased is not None
        assert leased.status is TaskStatus.LEASED
        assert (
            repository.claim(
                site_id,
                worker_id="gate4-worker-2",
                now=now + timedelta(seconds=1),
                lease_duration=timedelta(seconds=30),
            )
            is None
        )
        completed = repository.succeed(
            site_id,
            created.task_id,
            worker_id="gate4-worker-1",
            now=now + timedelta(seconds=2),
            output_artifact_refs=(f"action-proposal-{suffix}",),
        )
        assert completed.status is TaskStatus.SUCCEEDED
        assert [event.sequence for event in repository.timeline(site_id, created.task_id)] == [
            0,
            1,
            2,
        ]
    finally:
        conn.close()


def test_gate4_roles_are_non_superuser_non_bypass_and_schema_separated() -> None:
    for user_env, own_schema, forbidden_schema in (
        ("GBOS_GATE4_AGENT_USER", "agent_runtime", "observer"),
        ("GBOS_GATE4_CONTEXT_USER", "context", "agent_runtime"),
    ):
        conn = connection(user_env)
        try:
            with conn.transaction(), conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
                assert cursor.fetchone() == (False, False)
                cursor.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'USAGE')",
                    (own_schema,),
                )
                assert cursor.fetchone() == (True,)
                cursor.execute(
                    "SELECT has_schema_privilege(current_user, %s, 'USAGE')",
                    (forbidden_schema,),
                )
                assert cursor.fetchone() == (False,)
        finally:
            conn.close()


def test_gate4_human_confirmation_persists_exact_trace_without_mutating_proposal() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-decision-{suffix}.localhost"
    other_site = f"gate4-decision-other-{suffix}.localhost"
    raw_object_id = f"raw-{suffix}"
    event_id = f"event-{suffix}"
    observer_evidence_id = f"observer-evidence-{suffix}"
    evidence_record_id = f"evidence-record-{suffix}"
    proposal_ref = f"fact-proposal-{suffix}"
    decision_id = f"decision-{suffix}"
    verified_fact_id = f"verified-fact-{suffix}"
    subject_ref = f"contact-{suffix}"
    correlation_id = f"corr-{suffix}"
    raw_sha256 = "a" * 64
    payload_digest = "b" * 64
    occurred_at = datetime(2026, 8, 6, 1, 59, tzinfo=UTC)
    proposal_recorded_at = datetime(2026, 8, 6, 2, 0, 4, tzinfo=UTC)
    decided_at = datetime(2026, 8, 6, 3, 5, tzinfo=UTC)
    evidence_document = {
        "schema_version": "2.0",
        "evidence_record_id": evidence_record_id,
        "site_id": site_id,
        "processing_purpose": "business_operations",
        "evidence_ref": {
            "evidence_id": observer_evidence_id,
            "observation_event_id": event_id,
            "raw_sha256": raw_sha256,
            "object_ref": f"cos://synthetic/{raw_object_id}",
            "media_type": "text/plain",
            "locator": {"message_start": 0, "message_end": 42},
            "created_at": occurred_at.isoformat(),
        },
        "data_classification": "Restricted",
        "review_status": "human_reviewed",
        "recorded_at": proposal_recorded_at.isoformat(),
    }
    proposal_document = {
        "schema_version": "1.0",
        "fact": {
            "subject_ref": subject_ref,
            "predicate": "requested_quantity",
            "value": {"type": "number", "number": 1000, "unit": "pcs"},
            "evidence_refs": [evidence_record_id],
        },
        "source_lineage": {
            "source_system": "manual_import",
            "source_record_refs": [event_id, evidence_record_id],
            "retrieved_at": proposal_recorded_at.isoformat(),
            "transformation_version": "fact-proposal-v1",
            "evidence_status": "synthetic",
        },
        "valid_time": {"start": occurred_at.isoformat(), "end": None},
        "recorded_time": proposal_recorded_at.isoformat(),
        "output_version": "fact-proposal-v1",
    }

    owner = connection("GBOS_GATE4_OWNER_USER")
    try:
        with owner.transaction(), owner.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO observer.raw_objects (
                    site_id, object_id, object_ref, sha256, media_type,
                    byte_size, retention_class
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    site_id,
                    raw_object_id,
                    f"cos://synthetic/{raw_object_id}",
                    raw_sha256,
                    "text/plain",
                    42,
                    "R1-operational",
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.observation_events (
                    site_id, event_id, raw_object_id, provider_event_id,
                    connector, channel, processing_purpose, consent_basis,
                    data_classification, retention_class, correlation_id,
                    occurred_at, ingested_at, document, raw_sha256,
                    occurred_minute
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                )
                """,
                (
                    site_id,
                    event_id,
                    raw_object_id,
                    f"provider-{suffix}",
                    "manual_import",
                    "manual",
                    "business_operations",
                    "synthetic_test",
                    "Restricted",
                    "R1-operational",
                    correlation_id,
                    occurred_at,
                    proposal_recorded_at,
                    json.dumps({"synthetic": True, "raw_sha256": raw_sha256}),
                    raw_sha256,
                    occurred_at.replace(second=0, microsecond=0),
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.evidence_refs (
                    site_id, evidence_id, event_id, raw_object_id, raw_sha256,
                    media_type, locator, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    site_id,
                    observer_evidence_id,
                    event_id,
                    raw_object_id,
                    raw_sha256,
                    "text/plain",
                    json.dumps({"message_start": 0, "message_end": 42}),
                    occurred_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO context.evidence_records (
                    site_id, evidence_record_id, observer_evidence_id,
                    processing_purpose, idempotency_key, payload_digest,
                    review_status, data_classification, document, recorded_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    site_id,
                    evidence_record_id,
                    observer_evidence_id,
                    "business_operations",
                    f"evidence-idem-{suffix}",
                    payload_digest,
                    "human_reviewed",
                    "Restricted",
                    json.dumps(evidence_document),
                    proposal_recorded_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO context.fact_proposals (
                    site_id, fact_proposal_record_id, processing_purpose,
                    idempotency_key, payload_digest, status, subject_ref,
                    predicate, confidence, document, recorded_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s
                )
                """,
                (
                    site_id,
                    proposal_ref,
                    "business_operations",
                    f"proposal-idem-{suffix}",
                    payload_digest,
                    "proposed",
                    subject_ref,
                    "requested_quantity",
                    0.96,
                    json.dumps(proposal_document),
                    proposal_recorded_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO context.fact_evidence (
                    site_id, fact_proposal_record_id, evidence_record_id
                ) VALUES (%s, %s, %s)
                """,
                (site_id, proposal_ref, evidence_record_id),
            )
    finally:
        owner.close()

    context = connection("GBOS_GATE4_CONTEXT_USER")
    try:
        storage = PostgresDecisionStorage(context)
        service = DecisionService(storage, clock=lambda: decided_at)
        request = ConfirmationRequest(
            site_id=site_id,
            processing_purpose="business_operations",
            proposal_ref=proposal_ref,
            expected_proposal_version="fact-proposal-v1",
            expected_proposal_revision=1,
            decision_id=decision_id,
            verified_fact_id=verified_fact_id,
            decision_kind=DecisionKind.HUMAN,
            operator=f"reviewer-{suffix}",
            decision_basis="Synthetic retained evidence exactly supports this fact.",
            evidence_refs=(evidence_record_id,),
            valid_start=occurred_at,
            valid_end=None,
            effective_at=decided_at,
            correlation_id=correlation_id,
        )

        result = service.confirm(request)
        trace = service.trace(site_id, decision_id)

        assert result.decision["proposal_ref"] == proposal_ref
        assert result.decision["input_fact_refs"] == []
        assert result.decision["output_fact_refs"] == [
            {"fact_id": verified_fact_id, "fact_version": 1}
        ]
        assert result.decision["evidence_refs"] == [evidence_record_id]
        assert result.decision["proposal_version"] == "fact-proposal-v1"
        assert result.decision["proposal_revision"] == 1
        assert result.fact["confirmation_decision_ref"] == decision_id
        assert result.fact["proposal_ref"] == proposal_ref
        assert result.fact["proposal_version"] == "fact-proposal-v1"
        assert result.fact["proposal_revision"] == 1
        assert result.fact["fact_version"] == 1
        assert result.fact["evidence_refs"] == [evidence_record_id]
        assert result.fact["source_lineage"]["source_record_refs"] == [
            event_id,
            evidence_record_id,
            proposal_ref,
        ]
        assert result.fact["source_lineage"]["transformation_version"] == ("gate4-confirm-v1")
        assert trace["proposal"] == {
            "proposal_ref": proposal_ref,
            "proposal_version": "fact-proposal-v1",
            "proposal_revision": 1,
            "payload_digest": payload_digest,
        }
        assert trace["decision"] == result.decision
        assert trace["facts"] == [result.fact]
        assert trace["evidence"] == [evidence_document]
        with context.transaction(), context.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                """
                SELECT status, proposal_version, proposal_revision, document
                FROM context.fact_proposals
                WHERE site_id = %s AND fact_proposal_record_id = %s
                """,
                (site_id, proposal_ref),
            )
            assert cursor.fetchone() == (
                "proposed",
                "fact-proposal-v1",
                1,
                proposal_document,
            )
            cursor.execute(
                """
                SELECT ref_role, fact_id, fact_version
                FROM context.decision_fact_refs
                WHERE site_id = %s AND decision_id = %s
                ORDER BY ref_role, fact_id, fact_version
                """,
                (site_id, decision_id),
            )
            assert cursor.fetchall() == [("output", verified_fact_id, 1)]
            cursor.execute(
                """
                SELECT evidence_record_id
                FROM context.decision_evidence_refs
                WHERE site_id = %s AND decision_id = %s
                """,
                (site_id, decision_id),
            )
            assert cursor.fetchall() == [(evidence_record_id,)]
            cursor.execute(
                """
                SELECT evidence_record_id
                FROM context.fact_evidence_refs
                WHERE site_id = %s AND fact_id = %s AND fact_version = 1
                """,
                (site_id, verified_fact_id),
            )
            assert cursor.fetchall() == [(evidence_record_id,)]
        with pytest.raises(TraceIntegrityError):
            service.trace(other_site, decision_id)
    finally:
        context.close()
