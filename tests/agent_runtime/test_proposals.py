from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from services.action_guard.policy import ActionGuard
from services.agent_runtime import (
    AgentKind,
    AgentOrchestrator,
    AgentTaskSubmission,
    CostMetadata,
    DeterministicLocalProvider,
    FactVersionRef,
    IdempotencyConflict,
    InMemoryAgentTaskRepository,
    InvocationReferences,
    LeaseConflict,
    LocalPilotTaskPayload,
    MaterializationEnvelope,
    ModelInvocationRecord,
    TaskStatus,
    TokenUsageMetadata,
    TrustedMaterializer,
    ValidationError,
)
from services.agent_runtime.agents import AgentInput
from services.agent_runtime.models import canonical_payload_digest

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def task_payload() -> LocalPilotTaskPayload:
    return LocalPilotTaskPayload.from_mapping(
        {
            "schema_version": "local-pilot-agent-task-v1",
            "evidence_refs": ["evidence-1"],
            "fact_version_refs": [{"fact_id": "fact-1", "fact_version": 1}],
            "subject": {"revision": 1},
            "request": {
                "requested_by": "sales-agent",
                "decision_ref": "decision-1",
                "expected_action_type": "internal.work_item.propose",
                "candidate_refs": [],
            },
        }
    )


def enqueue_and_claim(repository: InMemoryAgentTaskRepository) -> None:
    repository.enqueue(
        AgentTaskSubmission(
            task_id="task-1",
            site_id="site-a",
            processing_purpose="sales_follow_up",
            idempotency_key="idem-1",
            agent_type="sales",
            subject_type="CRM Deal",
            subject_ref="deal-1",
            due_at=NOW,
            priority=50,
            max_attempts=3,
            causation_id="cause-1",
            correlation_id="correlation-1",
            payload=task_payload().to_mapping(),
        ),
        now=NOW,
    )
    claimed = repository.claim_for_execution(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None


def execution_result():
    request = AgentInput(
        task_id="task-1",
        site_id="site-a",
        processing_purpose="sales_follow_up",
        agent_kind=AgentKind.SALES,
        requested_by="sales-agent",
        subject_type="CRM Deal",
        subject_ref="deal-1",
        subject_revision=1,
        evidence_refs=("evidence-1",),
        fact_version_refs=(FactVersionRef("fact-1", 1),),
        decision_ref="decision-1",
        correlation_id="correlation-1",
        raw_context="Tokenization input loaded just in time.",
        expected_action_type="internal.work_item.propose",
    )
    runtime = AgentOrchestrator(
        provider=DeterministicLocalProvider(),
        guard=ActionGuard(),
        known_evidence_refs={"evidence-1"},
        known_fact_refs={("fact-1", 1)},
        known_subject_refs={("CRM Deal", "deal-1")},
    )
    return runtime.execute(request, now=NOW + timedelta(seconds=1))


def invocation(
    suffix: str,
    *,
    idempotency_key: str | None = None,
    output_digest: str = "a" * 64,
) -> ModelInvocationRecord:
    return ModelInvocationRecord(
        invocation_id=f"invocation-{suffix}",
        site_id="site-a",
        provider="deepseek",
        requested_model="deepseek-v4-flash",
        observed_model="deepseek-v4-flash",
        prompt_version="sales-local-pilot-v1",
        output_schema_version="sales-proposal-v1.0",
        policy_version="model-gateway-policy-v1",
        tokenizer_version="stable-hmac-tokenizer-v1",
        request_id="task-1",
        response_id=f"response-{suffix}",
        started_at=NOW,
        completed_at=NOW,
        latency_ms=1,
        status="succeeded",
        token_usage=TokenUsageMetadata.known(10, 5, 15),
        cost=CostMetadata.known(Decimal("0.001"), "USD"),
        network_call_count=1,
        tool_call_count=0,
        external_send_count=0,
        references=InvocationReferences(
            evidence_refs=("evidence-1",),
            tokenization_receipt_refs=("receipt-1",),
        ),
        idempotency_key=idempotency_key or f"model-call-{suffix}",
        attempt=1,
        retry_count=0,
        finish_code="stop",
        error_code=None,
        budget_status="normal",
        price_catalog_version="catalog-v1",
        output_digest=output_digest,
    )


def test_atomic_completion_persists_all_invocations_proposal_outbox_and_task() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue_and_claim(repository)
    result = replace(execution_result(), invocations=(invocation("1"), invocation("2")))

    completed = repository.complete_with_proposal(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=2),
        result=result,
    )

    proposal = repository.get_proposal("site-a", "task-1", attempt=1)
    outbox = repository.get_materialization("site-a", "task-1", attempt=1)
    assert completed.status is TaskStatus.SUCCEEDED
    assert proposal is not None
    assert proposal.invocation_ids == ("invocation-1", "invocation-2")
    assert proposal.origin == "AI"
    assert proposal.review_status == "AI Draft"
    assert outbox is not None
    assert outbox.status == "pending"
    assert [record.invocation_id for record in repository.invocations("site-a")] == [
        "invocation-1",
        "invocation-2",
    ]


def test_atomic_completion_rolls_back_everything_on_mid_bundle_conflict() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue_and_claim(repository)
    duplicate_key = "same-key"
    result = replace(
        execution_result(),
        invocations=(
            invocation("1", idempotency_key=duplicate_key),
            invocation("2", idempotency_key=duplicate_key, output_digest="b" * 64),
        ),
    )

    with pytest.raises(IdempotencyConflict):
        repository.complete_with_proposal(
            "site-a",
            "task-1",
            worker_id="worker-1",
            expected_attempt=1,
            now=NOW + timedelta(seconds=2),
            result=result,
        )

    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.status is TaskStatus.RUNNING
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None
    assert repository.get_materialization("site-a", "task-1", attempt=1) is None
    assert repository.invocations("site-a") == ()


def test_atomic_completion_replay_is_idempotent_but_different_result_conflicts() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue_and_claim(repository)
    result = replace(execution_result(), invocations=(invocation("1"),))

    first = repository.complete_with_proposal(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=2),
        result=result,
    )
    replay = repository.complete_with_proposal(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=3),
        result=result,
    )

    assert replay == first
    assert len(repository.invocations("site-a")) == 1
    with pytest.raises(LeaseConflict, match="attempt"):
        repository.complete_with_proposal(
            "site-a",
            "task-1",
            worker_id="worker-1",
            expected_attempt=2,
            now=NOW + timedelta(seconds=3),
            result=result,
        )
    changed_proposal = {
        **result.action_proposal,
        "payload": {"summary": "different"},
        "payload_digest": canonical_payload_digest({"summary": "different"}),
    }
    with pytest.raises(IdempotencyConflict):
        repository.complete_with_proposal(
            "site-a",
            "task-1",
            worker_id="worker-1",
            expected_attempt=1,
            now=NOW + timedelta(seconds=3),
            result=replace(result, action_proposal=changed_proposal),
        )


@pytest.mark.parametrize(
    "envelope",
    [
        MaterializationEnvelope(
            origin="Human",
            review_status="AI Draft",
            action_type="internal.work_item.propose",
            proposal={"payload": {"title": "Draft"}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="Approved",
            action_type="internal.work_item.propose",
            proposal={"payload": {"title": "Draft"}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="external.message.send",
            proposal={"payload": {"body": "hello"}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.work_item.propose",
            proposal={"payload": {"DraftMutation": {"doctype": "CRM Deal"}}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.review_case.propose",
            proposal={"payload": {"formal_discount": 10}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.review_case.propose",
            proposal={"payload": {"summary": "Select final supplier"}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.work_item.propose",
            proposal={"payload": {"summary": "Call +86 138 0013 8000"}},
        ),
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.work_item.propose",
            proposal={"payload": {"person_name": "Alice Smith"}},
        ),
    ],
)
def test_trusted_materializer_recursively_rejects_authoritative_or_outbound_shapes(
    envelope: MaterializationEnvelope,
) -> None:
    with pytest.raises(ValidationError):
        TrustedMaterializer().materialize(envelope)


def test_trusted_materializer_only_emits_ai_draft_work_items_or_review_cases() -> None:
    materializer = TrustedMaterializer()

    work_item = materializer.materialize(
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.work_item.propose",
            proposal={"payload": {"title": "Internal follow-up"}},
        )
    )
    ceo_observation = materializer.materialize(
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.ai_draft.propose",
            proposal={
                "payload": {
                    "title": "Communication-based observation",
                    "is_official_metric": False,
                }
            },
        )
    )

    assert (work_item.doctype, work_item.values["origin"], work_item.values["review_status"]) == (
        "GBOS Work Item",
        "AI",
        "AI Draft",
    )
    assert ceo_observation.doctype == "GBOS Review Case"
    assert ceo_observation.values["observation_kind"] == "informal_communication_observation"
    assert ceo_observation.values["source_basis"] == "communications"
    assert ceo_observation.values["is_official_metric"] is False
    assert "Metrics" not in repr(ceo_observation)


def test_trusted_materializer_can_only_submit_existing_ai_draft_to_pending() -> None:
    intent = TrustedMaterializer().materialize(
        MaterializationEnvelope(
            origin="AI",
            review_status="AI Draft",
            action_type="internal.work_item.transition.propose",
            proposal={
                "payload": {
                    "target_doctype": "GBOS Work Item",
                    "target_ref": "WORK-1",
                    "from_review_status": "AI Draft",
                    "to_review_status": "Pending",
                }
            },
        )
    )
    assert intent.operation == "submit"
    assert intent.values["review_status"] == "Pending"
