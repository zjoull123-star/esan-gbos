from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.action_guard.models import ActionRequest, EvaluationPhase, GuardOutcome
from services.action_guard.policy import ActionGuard

NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)


def request(
    action_type: str,
    *,
    scopes: tuple[str, ...] = ("gbos-propose",),
    site_id: str = "gbos.localhost",
    purpose: str = "sales_follow_up",
) -> ActionRequest:
    return ActionRequest(
        request_id="action-request-SYNTH-001",
        site_id=site_id,
        processing_purpose=purpose,
        action_type=action_type,
        requested_by="sales-agent-SYNTH-001",
        target_ref="work-item-SYNTH-001",
        target_revision=0,
        evidence_refs=("evidence-record-SYNTH-001",),
        granted_scopes=scopes,
        correlation_id="corr-SYNTH-001",
        payload={"summary": "Create an internal follow-up proposal."},
    )


@pytest.mark.parametrize(
    "action_type",
    [
        "internal.ai_draft.propose",
        "internal.fact.propose",
        "internal.work_item.propose",
        "internal.review_case.propose",
    ],
)
def test_scoped_internal_proposals_are_allowed(action_type: str) -> None:
    result = ActionGuard().evaluate(request(action_type), now=NOW)

    assert result.outcome is GuardOutcome.ALLOW
    assert result.reason_codes == ("scoped_internal_proposal",)
    assert result.policy_version == "action-guard-v1"


def test_context_read_requires_exact_scope_and_never_accepts_kingdee_scope() -> None:
    allowed = ActionGuard().evaluate(
        request("context.evidence.get", scopes=("gbos-read",)),
        now=NOW,
    )
    missing_scope = ActionGuard().evaluate(
        request("context.evidence.get", scopes=("gbos-propose",)),
        now=NOW,
    )
    kingdee_scope = ActionGuard().evaluate(
        request("context.evidence.get", scopes=("kingdee-read",)),
        now=NOW,
    )

    assert allowed.outcome is GuardOutcome.ALLOW
    assert missing_scope.outcome is GuardOutcome.DENY
    assert kingdee_scope.outcome is GuardOutcome.DENY


@pytest.mark.parametrize(
    "action_type",
    [
        "internal.fact.confirm",
        "internal.work_item.transition",
        "internal.supplier.select",
        "formal.quotation.publish",
        "external.message.send",
        "deal.won",
        "order.create",
        "payment.create",
    ],
)
def test_commitments_and_business_state_changes_require_human(
    action_type: str,
) -> None:
    result = ActionGuard().evaluate(request(action_type), now=NOW)

    assert result.outcome is GuardOutcome.REQUIRE_HUMAN
    assert "human_authorization_required" in result.reason_codes


@pytest.mark.parametrize(
    "action_type",
    [
        "kingdee.material.update",
        "kingdee.sales_order.save",
        "kingdee.bill.submit",
        "kingdee.bill.audit",
        "kingdee.bill.unaudit",
        "kingdee.receivable.payment",
        "internal.record.delete",
        "internal.record.archive",
        "direct_database.write",
        "frappe.client.set_value",
    ],
)
def test_kingdee_destructive_and_generic_writes_are_always_denied(
    action_type: str,
) -> None:
    result = ActionGuard().evaluate(
        request(action_type, scopes=("gbos-read", "gbos-propose", "kingdee-read")),
        now=NOW,
    )

    assert result.outcome is GuardOutcome.DENY
    assert "forbidden_capability" in result.reason_codes


def test_unknown_action_and_missing_evidence_fail_closed() -> None:
    unknown = ActionGuard().evaluate(request("internal.magic.do_anything"), now=NOW)
    no_evidence_request = request("internal.work_item.propose")
    no_evidence = ActionGuard().evaluate(
        ActionRequest(
            request_id=no_evidence_request.request_id,
            site_id=no_evidence_request.site_id,
            processing_purpose=no_evidence_request.processing_purpose,
            action_type=no_evidence_request.action_type,
            requested_by=no_evidence_request.requested_by,
            target_ref=no_evidence_request.target_ref,
            target_revision=no_evidence_request.target_revision,
            evidence_refs=(),
            granted_scopes=no_evidence_request.granted_scopes,
            correlation_id=no_evidence_request.correlation_id,
            payload=no_evidence_request.payload,
        ),
        now=NOW,
    )

    assert unknown.outcome is GuardOutcome.DENY
    assert "unknown_action" in unknown.reason_codes
    assert no_evidence.outcome is GuardOutcome.DENY
    assert "evidence_required" in no_evidence.reason_codes


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "draft_mutation",
        "approved_command",
        "execution",
        "frappe_write",
        "kingdee_mutation",
        "external_send",
    ],
)
def test_post_result_blocks_execution_claims(forbidden_key: str) -> None:
    action = request("internal.work_item.propose")
    result = ActionGuard().evaluate(
        action,
        phase=EvaluationPhase.POST_RESULT,
        result_payload={
            "site_id": action.site_id,
            "processing_purpose": action.processing_purpose,
            "target_revision": action.target_revision,
            "evidence_refs": list(action.evidence_refs),
            forbidden_key: {"attempted": True},
        },
        now=NOW,
    )

    assert result.outcome is GuardOutcome.DENY
    assert "execution_shape_forbidden" in result.reason_codes


@pytest.mark.parametrize(
    "nested_value",
    [
        {"proposal": {"execution": {"attempted": True}}},
        {"proposal": {"items": [{"approved_command": {"command": "write"}}]}},
        {"proposal": [{"result": {"tool_calls": []}}]},
        {"proposal": {"commercial": {"formal_price": "100.00"}}},
        {"proposal": {"DraftMutation": {"doctype": "CRM Deal"}}},
        {"proposal": {"ApprovedCommand": {"action": "submit"}}},
        {"proposal": {"toolCalls": [{"name": "send"}]}},
        {"proposal": {"commercial": {"formalPrice": "100.00"}}},
    ],
)
def test_post_result_recursively_blocks_forbidden_result_shapes(
    nested_value: dict[str, object],
) -> None:
    action = request("internal.work_item.propose")
    result = ActionGuard().evaluate(
        action,
        phase=EvaluationPhase.POST_RESULT,
        result_payload={
            "site_id": action.site_id,
            "processing_purpose": action.processing_purpose,
            "target_revision": action.target_revision,
            "evidence_refs": list(action.evidence_refs),
            **nested_value,
        },
        now=NOW,
    )

    assert result.outcome is GuardOutcome.DENY
    assert "execution_shape_forbidden" in result.reason_codes


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("site_id", "other.localhost", "site_mismatch"),
        ("processing_purpose", "purchase_sourcing", "purpose_mismatch"),
        ("target_revision", 8, "target_revision_mismatch"),
        ("evidence_refs", ["other-evidence"], "evidence_mismatch"),
    ],
)
def test_post_result_rechecks_scope_versions_and_evidence(
    field: str,
    value: object,
    reason: str,
) -> None:
    action = request("internal.work_item.propose")
    payload: dict[str, object] = {
        "site_id": action.site_id,
        "processing_purpose": action.processing_purpose,
        "target_revision": action.target_revision,
        "evidence_refs": list(action.evidence_refs),
        "proposal": {"summary": "Internal proposal only."},
    }
    payload[field] = value

    result = ActionGuard().evaluate(
        action,
        phase=EvaluationPhase.POST_RESULT,
        result_payload=payload,
        now=NOW,
    )

    assert result.outcome is GuardOutcome.DENY
    assert reason in result.reason_codes


def test_pre_and_post_decisions_are_deterministic_but_phase_bound() -> None:
    guard = ActionGuard()
    action = request("internal.work_item.propose")
    pre_first = guard.evaluate(action, now=NOW)
    pre_second = guard.evaluate(action, now=NOW)
    post = guard.evaluate(
        action,
        phase=EvaluationPhase.POST_RESULT,
        result_payload={
            "site_id": action.site_id,
            "processing_purpose": action.processing_purpose,
            "target_revision": action.target_revision,
            "evidence_refs": list(action.evidence_refs),
            "proposal": {"summary": "Internal proposal only."},
        },
        now=NOW,
    )

    assert pre_first == pre_second
    assert pre_first.guard_decision_id != post.guard_decision_id
    assert post.outcome is GuardOutcome.ALLOW
