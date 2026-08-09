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
    MaterializationContext,
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


def _materialization_envelope(
    action_type: str,
    payload: dict[str, object],
    *,
    subject_type: str,
    subject_ref: str,
) -> MaterializationEnvelope:
    return MaterializationEnvelope(
        proposal_id="proposal-1",
        task_id="task-1",
        processing_purpose="metric_reporting",
        origin="AI",
        review_status="AI Draft",
        action_type=action_type,
        subject_type=subject_type,
        subject_ref=subject_ref,
        subject_revision=3,
        evidence_refs=("evidence-1",),
        model_name="deepseek-v4-flash",
        model_version="deepseek-v4-flash",
        policy_version="action-guard-v1",
        proposal={"payload": payload},
    )


def test_trusted_materializer_emits_only_closed_frappe_doctype_fields() -> None:
    materializer = TrustedMaterializer()
    context = MaterializationContext(team="team-a")

    work_item = materializer.materialize(
        _materialization_envelope(
            "internal.work_item.propose",
            {
                "title": "Internal follow-up",
                "summary": "Provider prose must not leak into an unsupported field.",
                "subject_ref": "untrusted-ref",
            },
            subject_type="CRM Deal",
            subject_ref="deal-1",
        ),
        context=context,
    )
    ceo_observation = materializer.materialize(
        _materialization_envelope(
            "internal.ai_draft.propose",
            {
                "title": "Communication-based observation",
                "summary": "Leadership communication pattern",
                "synthetic": True,
                "display_label": "Synthetic",
                "source_mode": "communications",
                "is_official_metric": False,
                "is_official_forecast": False,
                "requires_human_review": True,
            },
            subject_type="GBOS Synthetic Executive Snapshot",
            subject_ref="snapshot-1",
        ),
        context=context,
    )

    assert work_item.doctype == "GBOS Work Item"
    assert set(work_item.values) == {
        "title",
        "team",
        "reference_doctype",
        "reference_name",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    }
    assert work_item.values["origin_reference"] == "proposal-1"
    assert work_item.values["reference_name"] == "deal-1"
    assert ceo_observation.doctype == "GBOS Informal Observation"
    assert set(ceo_observation.values) == {
        "subject",
        "summary_zh",
        "team",
        "evidence_refs",
        "model_name",
        "model_version",
        "is_official_metric",
        "origin",
        "origin_reference",
        "review_status",
    }
    assert ceo_observation.values["origin_reference"] == "proposal-1"
    assert ceo_observation.values["evidence_refs"] == [
        {"evidence_ref": "evidence-1", "locator_ref": "evidence-1"}
    ]
    assert ceo_observation.values["is_official_metric"] is False
    assert "Communication-based observation" not in repr(ceo_observation)
    assert "Leadership communication pattern" not in repr(ceo_observation)
    assert "evidence-1" not in repr(ceo_observation)


def test_trusted_materializer_pins_review_case_from_trusted_metadata() -> None:
    subject_snapshot = {
        "doctype": "GBOS Sourcing Event",
        "name": "source-1",
        "revision": 3,
        "title": "Trusted sourcing event",
    }
    subject_digest = canonical_payload_digest(subject_snapshot)
    intent = TrustedMaterializer().materialize(
        _materialization_envelope(
            "internal.review_case.propose",
            {
                "title": "Supplier review",
                "summary": "Unsupported provider prose",
                "candidate_refs": ["untrusted-candidate"],
                "recommendation": "untrusted-recommendation",
                "subject_ref": "untrusted-ref",
            },
            subject_type="GBOS Sourcing Event",
            subject_ref="source-1",
        ),
        context=MaterializationContext(
            team="team-a",
            assigned_reviewer="reviewer@example.com",
            subject_snapshot=subject_snapshot,
            subject_payload_digest=subject_digest,
        ),
    )

    assert intent.doctype == "GBOS Review Case"
    assert set(intent.values) == {
        "title",
        "team",
        "assigned_reviewer",
        "subject_doctype",
        "subject_name",
        "subject_revision",
        "subject_payload_sha256",
        "subject_snapshot",
        "case_payload_sha256",
        "evidence_refs",
        "policy_version",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    }
    assert intent.values["origin_reference"] == "proposal-1"
    assert intent.values["subject_name"] == "source-1"
    assert intent.values["evidence_refs"] == '["evidence-1"]'


def test_trusted_materializer_fails_closed_without_controlled_team() -> None:
    envelope = _materialization_envelope(
        "internal.work_item.propose",
        {"title": "Internal follow-up"},
        subject_type="CRM Deal",
        subject_ref="deal-1",
    )

    with pytest.raises(ValidationError, match="team"):
        TrustedMaterializer().materialize(envelope)


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
