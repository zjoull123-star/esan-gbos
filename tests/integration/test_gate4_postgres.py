from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.errors import InsufficientPrivilege, ObjectNotInPrerequisiteState

from services.action_guard.policy import ActionGuard
from services.agent_runtime import (
    AgentKind,
    AgentOrchestrator,
    AgentTaskSubmission,
    CostMetadata,
    DeterministicLocalProvider,
    FactVersionRef,
    FrappeDraftReceipt,
    IdempotencyConflict,
    InvocationReferences,
    LeaseConflict,
    LocalPilotTaskPayload,
    ModelInvocationRecord,
    PostgresAgentReadService,
    PostgresAgentTaskRepository,
    PostgresModelInvocationRepository,
    TaskStatus,
    TokenUsageMetadata,
)
from services.agent_runtime.agents import AgentInput
from services.agent_runtime.models import canonical_payload_digest
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


def invocation(
    site_id: str,
    suffix: str,
    *,
    request_id: str | None = None,
    evidence_ref: str | None = None,
    idempotency_key: str | None = None,
    output_digest: str = "a" * 64,
) -> ModelInvocationRecord:
    now = datetime.now(UTC)
    return ModelInvocationRecord(
        invocation_id=f"invocation-{suffix}",
        site_id=site_id,
        provider="deepseek",
        requested_model="deepseek-v4-flash",
        observed_model="deepseek-v4-flash",
        prompt_version="sales-local-pilot-v1",
        output_schema_version="sales-proposal-v1.0",
        policy_version="model-gateway-policy-v1",
        tokenizer_version="stable-hmac-tokenizer-v1",
        request_id=request_id or f"request-{suffix}",
        response_id=f"response-{suffix}",
        started_at=now,
        completed_at=now,
        latency_ms=10,
        status="succeeded",
        token_usage=TokenUsageMetadata.known(10, 5, 15),
        cost=CostMetadata.known(Decimal("0.001"), "USD"),
        network_call_count=1,
        tool_call_count=0,
        external_send_count=0,
        references=InvocationReferences(
            evidence_refs=(evidence_ref or f"evidence-{suffix}",),
            tokenization_receipt_refs=(f"receipt-{suffix}",),
        ),
        idempotency_key=idempotency_key or f"invocation-idem-{suffix}",
        attempt=1,
        retry_count=0,
        finish_code="stop",
        error_code=None,
        budget_status="normal",
        price_catalog_version="catalog-v1",
        output_digest=output_digest,
    )


def local_pilot_submission(
    site_id: str,
    suffix: str,
    *,
    max_attempts: int = 3,
) -> AgentTaskSubmission:
    now = datetime.now(UTC)
    return AgentTaskSubmission(
        task_id=f"worker-task-{suffix}",
        site_id=site_id,
        processing_purpose="sales_follow_up",
        idempotency_key=f"worker-idem-{suffix}",
        agent_type="sales",
        subject_type="CRM Deal",
        subject_ref=f"worker-deal-{suffix}",
        due_at=now,
        priority=50,
        max_attempts=max_attempts,
        causation_id=f"worker-cause-{suffix}",
        correlation_id=f"worker-corr-{suffix}",
        payload=LocalPilotTaskPayload.from_mapping(
            {
                "schema_version": "local-pilot-agent-task-v1",
                "evidence_refs": [f"worker-evidence-{suffix}"],
                "fact_version_refs": [{"fact_id": f"worker-fact-{suffix}", "fact_version": 1}],
                "subject": {"revision": 1},
                "request": {
                    "requested_by": f"sales-agent-{suffix}",
                    "decision_ref": f"worker-decision-{suffix}",
                    "expected_action_type": "internal.work_item.propose",
                    "candidate_refs": [],
                },
            }
        ).to_mapping(),
    )


def agent_result(request: AgentTaskSubmission):
    evidence_ref = f"worker-evidence-{request.task_id.removeprefix('worker-task-')}"
    fact_ref = f"worker-fact-{request.task_id.removeprefix('worker-task-')}"
    runtime = AgentOrchestrator(
        provider=DeterministicLocalProvider(),
        guard=ActionGuard(),
        known_evidence_refs={evidence_ref},
        known_fact_refs={(fact_ref, 1)},
        known_subject_refs={(request.subject_type, request.subject_ref)},
    )
    return runtime.execute(
        AgentInput(
            task_id=request.task_id,
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            agent_kind=AgentKind.SALES,
            requested_by=f"sales-agent-{request.task_id.removeprefix('worker-task-')}",
            subject_type=request.subject_type,
            subject_ref=request.subject_ref,
            subject_revision=1,
            evidence_refs=(evidence_ref,),
            fact_version_refs=(FactVersionRef(fact_ref, 1),),
            decision_ref=f"worker-decision-{request.task_id.removeprefix('worker-task-')}",
            correlation_id=request.correlation_id,
            raw_context="Tokenization input resolved at execution time.",
            expected_action_type="internal.work_item.propose",
        ),
        now=request.due_at + timedelta(seconds=1),
    )


def test_gate4_model_invocation_migration_is_ledgered_and_forces_rls() -> None:
    conn = connection("GBOS_GATE4_OWNER_USER")
    try:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT migration_name
                FROM observer.schema_migrations
                WHERE migration_name = %s
                """,
                ("agent/002_local_pilot_model_runtime.sql",),
            )
            assert cursor.fetchone() == ("agent/002_local_pilot_model_runtime.sql",)
            cursor.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE oid = 'agent_runtime.model_invocations'::regclass
                """
            )
            assert cursor.fetchone() == (True, True)
    finally:
        conn.close()


def test_gate4_model_invocation_repository_is_site_isolated_and_cross_write_fails() -> None:
    suffix = uuid4().hex
    site_a = f"gate4-invocation-a-{suffix}.localhost"
    site_b = f"gate4-invocation-b-{suffix}.localhost"
    record_a = invocation(site_a, f"a-{suffix}")
    record_b = invocation(site_b, f"b-{suffix}")
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresModelInvocationRepository(conn)
        assert repository.append(record_a) == record_a
        assert repository.append(record_a) == record_a
        assert repository.append(record_b) == record_b
        assert repository.get(site_a, record_b.invocation_id) is None
        assert repository.list(site_a) == (record_a,)

        with (
            pytest.raises(InsufficientPrivilege),
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_a,))
            cursor.execute(
                """
                INSERT INTO agent_runtime.model_invocations (
                    site_id, invocation_id, idempotency_key, provider,
                    requested_model, prompt_version, output_schema_version,
                    policy_version, tokenizer_version, request_id,
                    started_at, completed_at, latency_ms, status,
                    token_usage_status, cost_status, network_call_count,
                    tool_call_count, external_send_count, attempt, retry_count,
                    error_code, budget_status
                ) VALUES (
                    %s, %s, %s, 'deepseek', 'deepseek-v4-flash',
                    'sales-local-pilot-v1', 'sales-proposal-v1.0',
                    'model-gateway-policy-v1', 'stable-hmac-tokenizer-v1',
                    %s, %s, %s, 1, 'failed', 'unknown', 'unknown',
                    0, 0, 0, 1, 0, 'budget_hard_stop', 'hard_stop'
                )
                """,
                (
                    site_b,
                    f"cross-{suffix}",
                    f"cross-idem-{suffix}",
                    f"cross-request-{suffix}",
                    record_a.started_at,
                    record_a.completed_at,
                ),
            )
    finally:
        conn.close()


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
            expected_attempt=1,
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


def test_gate4_agent_worker_migration_is_ledgered_once_and_forces_rls() -> None:
    conn = connection("GBOS_GATE4_OWNER_USER")
    try:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT migration_name, COUNT(*)
                FROM observer.schema_migrations
                WHERE migration_name = %s
                GROUP BY migration_name
                """,
                ("agent/003_local_pilot_agent_worker.sql",),
            )
            assert cursor.fetchone() == ("agent/003_local_pilot_agent_worker.sql", 1)
            cursor.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE oid IN (
                    'agent_runtime.action_proposals'::regclass,
                    'agent_runtime.proposal_materialization_outbox'::regclass
                )
                ORDER BY relname
                """
            )
            assert cursor.fetchall() == [
                ("action_proposals", True, True),
                ("proposal_materialization_outbox", True, True),
            ]
    finally:
        conn.close()


def test_gate4_same_worker_stale_attempt_is_rejected_after_reclaim() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-worker-fence-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = local_pilot_submission(site_id, suffix)
        repository.enqueue(request, now=request.due_at)
        first = repository.claim_for_execution(
            site_id,
            worker_id="same-worker",
            now=request.due_at,
            lease_duration=timedelta(seconds=10),
        )
        recovered = repository.claim_for_execution(
            site_id,
            worker_id="same-worker",
            now=request.due_at + timedelta(seconds=11),
            lease_duration=timedelta(seconds=30),
        )
        assert first is not None
        assert recovered is not None
        assert recovered.metadata.attempt == 2

        with pytest.raises(LeaseConflict, match="attempt"):
            repository.heartbeat(
                site_id,
                request.task_id,
                worker_id="same-worker",
                expected_attempt=1,
                now=request.due_at + timedelta(seconds=12),
                lease_duration=timedelta(seconds=30),
            )
        with pytest.raises(LeaseConflict, match="attempt"):
            repository.succeed(
                site_id,
                request.task_id,
                worker_id="same-worker",
                expected_attempt=1,
                now=request.due_at + timedelta(seconds=12),
            )
    finally:
        conn.close()


def test_gate4_task_payload_is_database_immutable_for_agent_role() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-payload-immutable-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = local_pilot_submission(site_id, suffix)
        repository.enqueue(request, now=request.due_at)

        with (
            pytest.raises(ObjectNotInPrerequisiteState),
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                """
                UPDATE agent_runtime.agent_tasks
                SET payload = %s::jsonb
                WHERE site_id = %s AND task_id = %s
                """,
                (
                    json.dumps({"raw_context": "Alice at alice@example.com"}),
                    site_id,
                    request.task_id,
                ),
            )

        stored = repository.get(site_id, request.task_id)
        assert stored is not None
        assert stored.payload_digest == request.payload_digest
    finally:
        conn.close()


def test_gate4_atomic_proposal_bundle_rolls_back_replays_and_is_site_isolated() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-bundle-{suffix}.localhost"
    other_site = f"gate4-bundle-other-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = local_pilot_submission(site_id, suffix)
        repository.enqueue(request, now=request.due_at)
        claim = repository.claim_for_execution(
            site_id,
            worker_id="bundle-worker",
            now=request.due_at,
            lease_duration=timedelta(seconds=30),
        )
        assert claim is not None
        base_result = agent_result(request)
        evidence_ref = f"worker-evidence-{suffix}"
        duplicate_key = f"duplicate-model-key-{suffix}"
        first_invocation = invocation(
            site_id,
            f"bundle-a-{suffix}",
            request_id=request.task_id,
            evidence_ref=evidence_ref,
            idempotency_key=duplicate_key,
        )
        conflicting_invocation = invocation(
            site_id,
            f"bundle-b-{suffix}",
            request_id=request.task_id,
            evidence_ref=evidence_ref,
            idempotency_key=duplicate_key,
            output_digest="b" * 64,
        )
        with pytest.raises(IdempotencyConflict):
            repository.complete_with_proposal(
                site_id,
                request.task_id,
                worker_id="bundle-worker",
                expected_attempt=1,
                now=request.due_at + timedelta(seconds=2),
                result=replace(
                    base_result,
                    invocations=(first_invocation, conflicting_invocation),
                ),
            )

        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                "SELECT status FROM agent_runtime.agent_tasks WHERE site_id = %s AND task_id = %s",
                (site_id, request.task_id),
            )
            assert cursor.fetchone() == ("running",)
            for table in (
                "model_invocations",
                "action_proposals",
                "proposal_materialization_outbox",
            ):
                cursor.execute(
                    f"SELECT COUNT(*) FROM agent_runtime.{table} WHERE site_id = %s",
                    (site_id,),
                )
                assert cursor.fetchone() == (0,)

        second_invocation = replace(
            conflicting_invocation,
            idempotency_key=f"unique-model-key-{suffix}",
        )
        result = replace(
            base_result,
            invocations=(first_invocation, second_invocation),
        )
        completed = repository.complete_with_proposal(
            site_id,
            request.task_id,
            worker_id="bundle-worker",
            expected_attempt=1,
            now=request.due_at + timedelta(seconds=2),
            result=result,
        )
        replay = repository.complete_with_proposal(
            site_id,
            request.task_id,
            worker_id="bundle-worker",
            expected_attempt=1,
            now=request.due_at + timedelta(seconds=3),
            result=result,
        )
        assert completed == replay
        assert completed.status is TaskStatus.SUCCEEDED
        assert repository.get(other_site, request.task_id) is None

        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            for table, expected in (
                ("model_invocations", 2),
                ("action_proposals", 1),
                ("proposal_materialization_outbox", 1),
            ):
                cursor.execute(
                    f"SELECT COUNT(*) FROM agent_runtime.{table} WHERE site_id = %s",
                    (site_id,),
                )
                assert cursor.fetchone() == (expected,)
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (other_site,))
            cursor.execute(
                "SELECT COUNT(*) FROM agent_runtime.action_proposals WHERE site_id = %s",
                (site_id,),
            )
            assert cursor.fetchone() == (0,)

        with (
            pytest.raises(InsufficientPrivilege),
            conn.transaction(),
            conn.cursor() as cursor,
        ):
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (other_site,))
            cursor.execute(
                """
                INSERT INTO agent_runtime.action_proposals (
                    site_id, proposal_id, idempotency_key, task_id, task_attempt,
                    action_type, status, origin, review_status, subject_type,
                    subject_ref, subject_revision, evidence_refs,
                    fact_version_refs, invocation_ids, payload_digest,
                    bundle_digest, document, created_at
                ) VALUES (
                    %s, %s, %s, %s, 2, 'internal.work_item.propose',
                    'proposed', 'AI', 'AI Draft', 'CRM Deal', %s, 1,
                    %s::jsonb, %s::jsonb, '[]'::jsonb, %s, %s, '{}'::jsonb, %s
                )
                """,
                (
                    site_id,
                    f"cross-proposal-{suffix}",
                    f"cross-proposal-idem-{suffix}",
                    request.task_id,
                    request.subject_ref,
                    json.dumps([evidence_ref]),
                    json.dumps([[f"worker-fact-{suffix}", 1]]),
                    "c" * 64,
                    "d" * 64,
                    request.due_at,
                ),
            )

        changed_payload = {"summary": "different valid proposal"}
        with pytest.raises(IdempotencyConflict):
            repository.complete_with_proposal(
                site_id,
                request.task_id,
                worker_id="bundle-worker",
                expected_attempt=1,
                now=request.due_at + timedelta(seconds=3),
                result=replace(
                    result,
                    action_proposal={
                        **result.action_proposal,
                        "payload": changed_payload,
                        "payload_digest": canonical_payload_digest(changed_payload),
                    },
                ),
            )
    finally:
        conn.close()


def _complete_materializable_proposal(
    repository: PostgresAgentTaskRepository,
    *,
    site_id: str,
    suffix: str,
    action_type: str = "internal.work_item.propose",
) -> AgentTaskSubmission:
    request = local_pilot_submission(site_id, suffix)
    repository.enqueue(request, now=request.due_at)
    claim = repository.claim_for_execution(
        site_id,
        worker_id="materialization-source-worker",
        now=request.due_at,
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    result = agent_result(request)
    payload: dict[str, object] = {"summary": f"Draft {suffix}"}
    if action_type == "internal.ai_draft.propose":
        payload["is_official_metric"] = False
    proposal = {
        **result.action_proposal,
        "action_type": action_type,
        "payload": payload,
        "payload_digest": canonical_payload_digest(payload),
    }
    repository.complete_with_proposal(
        site_id,
        request.task_id,
        worker_id="materialization-source-worker",
        expected_attempt=1,
        now=request.due_at + timedelta(seconds=2),
        result=replace(result, action_proposal=proposal),
    )
    return request


def test_gate4_local_read_service_is_site_scoped_partial_and_cursor_stable() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-read-{suffix}.localhost"
    other_site = f"gate4-read-other-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        invocation_repository = PostgresModelInvocationRepository(conn)
        known = replace(
            invocation(site_id, f"usage-known-{suffix}"),
            token_usage=TokenUsageMetadata.known(80, 20, 100),
            cost=CostMetadata.known(Decimal("50"), "USD"),
        )
        unknown = replace(
            invocation(site_id, f"usage-unknown-{suffix}"),
            token_usage=TokenUsageMetadata.unknown(),
            cost=CostMetadata.unknown(),
            price_catalog_version=None,
        )
        other = replace(
            invocation(other_site, f"usage-other-{suffix}"),
            cost=CostMetadata.known(Decimal("100"), "USD"),
        )
        invocation_repository.append(known)
        invocation_repository.append(unknown)
        invocation_repository.append(other)

        period = known.started_at.strftime("%Y-%m")
        read_service = PostgresAgentReadService(conn)
        usage = read_service.get_usage(site_id, period)
        other_usage = read_service.get_usage(other_site, period)
        assert (usage.tokens, usage.token_state) == (100, "partial")
        assert (usage.cost.amount, usage.cost.state, usage.state) == (
            Decimal("50"),
            "partial",
            "soft_limit",
        )
        assert other_usage.state == "hard_limit"

        task_repository = PostgresAgentTaskRepository(conn)
        for index, action_type in enumerate(
            (
                "internal.work_item.propose",
                "internal.review_case.propose",
                "internal.ai_draft.propose",
            ),
            start=1,
        ):
            _complete_materializable_proposal(
                task_repository,
                site_id=site_id,
                suffix=f"{suffix}-{index}",
                action_type=action_type,
            )
        first = read_service.list_drafts(site_id, page_size=2)
        assert len(first.drafts) == 2
        assert first.next_cursor is not None
        second = read_service.list_drafts(
            site_id,
            cursor=first.next_cursor,
            page_size=2,
        )
        assert len(second.drafts) == 1
        assert second.next_cursor is None
        assert {draft.kind for draft in (*first.drafts, *second.drafts)} == {
            "Work Item",
            "Review Case",
            "CEO Informal Observation",
        }
        assert read_service.get_draft(other_site, first.drafts[0].draft_id) is None
    finally:
        conn.close()


def test_gate4_materialization_lease_fence_receipt_replay_and_rls() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-materialization-{suffix}.localhost"
    other_site = f"gate4-materialization-other-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = _complete_materializable_proposal(
            repository,
            site_id=site_id,
            suffix=suffix,
        )
        first = repository.claim_materialization(
            site_id,
            worker_id="same-materializer",
            now=request.due_at + timedelta(seconds=3),
            lease_duration=timedelta(seconds=5),
        )
        recovered = repository.claim_materialization(
            site_id,
            worker_id="same-materializer",
            now=request.due_at + timedelta(seconds=9),
            lease_duration=timedelta(seconds=10),
        )
        assert first is not None
        assert recovered is not None
        assert (first.attempt, recovered.attempt) == (1, 2)
        receipt = FrappeDraftReceipt(
            doctype="GBOS Work Item",
            name=f"WORK-{suffix}",
            revision=0,
            request_id=recovered.materialization_id,
            request_digest="a" * 64,
        )

        with pytest.raises(LeaseConflict):
            repository.acknowledge_materialization(
                site_id,
                recovered.materialization_id,
                worker_id="same-materializer",
                expected_attempt=1,
                now=request.due_at + timedelta(seconds=10),
                receipt=receipt,
            )

        acknowledged = repository.acknowledge_materialization(
            site_id,
            recovered.materialization_id,
            worker_id="same-materializer",
            expected_attempt=2,
            now=request.due_at + timedelta(seconds=10),
            receipt=receipt,
        )
        replay = repository.acknowledge_materialization(
            site_id,
            recovered.materialization_id,
            worker_id="same-materializer",
            expected_attempt=2,
            now=request.due_at + timedelta(seconds=11),
            receipt=receipt,
        )
        assert acknowledged == replay
        with pytest.raises(IdempotencyConflict, match="body conflict"):
            repository.acknowledge_materialization(
                site_id,
                recovered.materialization_id,
                worker_id="same-materializer",
                expected_attempt=2,
                now=request.due_at + timedelta(seconds=11),
                receipt=replace(receipt, name=f"WORK-CHANGED-{suffix}"),
            )
        assert (
            repository.claim_materialization(
                other_site,
                worker_id="other-materializer",
                now=request.due_at + timedelta(seconds=12),
                lease_duration=timedelta(seconds=5),
            )
            is None
        )
        assert repository.materialization_health(site_id).to_wire() == {
            "ready": True,
            "pending": 0,
            "running": 0,
            "retry": 0,
            "dead_letter": 0,
        }
    finally:
        conn.close()


def test_gate4_materialization_claim_carries_only_trusted_task_and_model_metadata() -> None:
    suffix = uuid4().hex
    site_id = f"gate4-materialization-context-{suffix}.localhost"
    conn = connection("GBOS_GATE4_AGENT_USER")
    try:
        repository = PostgresAgentTaskRepository(conn)
        request = local_pilot_submission(site_id, suffix)
        repository.enqueue(request, now=request.due_at)
        claimed_task = repository.claim_for_execution(
            site_id,
            worker_id="materialization-context-source",
            now=request.due_at,
            lease_duration=timedelta(seconds=30),
        )
        assert claimed_task is not None
        result = agent_result(request)
        evidence_ref = f"worker-evidence-{suffix}"
        model_record = invocation(
            site_id,
            f"materialization-context-{suffix}",
            request_id=request.task_id,
            evidence_ref=evidence_ref,
        )
        repository.complete_with_proposal(
            site_id,
            request.task_id,
            worker_id="materialization-context-source",
            expected_attempt=1,
            now=request.due_at + timedelta(seconds=2),
            result=replace(result, invocations=(model_record,)),
        )

        materialization = repository.claim_materialization(
            site_id,
            worker_id="materialization-context-worker",
            now=request.due_at + timedelta(seconds=3),
            lease_duration=timedelta(seconds=10),
        )

        assert materialization is not None
        envelope = materialization.envelope
        assert envelope.proposal_id == materialization.proposal_id
        assert envelope.task_id == request.task_id
        assert envelope.processing_purpose == request.processing_purpose
        assert envelope.subject_type == request.subject_type
        assert envelope.subject_ref == request.subject_ref
        assert envelope.subject_revision == 1
        assert envelope.evidence_refs == (evidence_ref,)
        assert envelope.model_name == model_record.requested_model
        assert envelope.model_version == model_record.observed_model
        assert "raw_context" not in repr(envelope)
    finally:
        conn.close()


def test_gate4_materialization_migration_is_ledgered_once_and_keeps_rls() -> None:
    conn = connection("GBOS_GATE4_OWNER_USER")
    try:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT migration_name, COUNT(*)
                FROM observer.schema_migrations
                WHERE migration_name = %s
                GROUP BY migration_name
                """,
                ("agent/004_local_pilot_materialization.sql",),
            )
            assert cursor.fetchone() == (
                "agent/004_local_pilot_materialization.sql",
                1,
            )
            cursor.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE oid =
                    'agent_runtime.proposal_materialization_outbox'::regclass
                """
            )
            assert cursor.fetchone() == (True, True)
    finally:
        conn.close()
