from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .email_send import (
    EmailSendAuthorityReceipt,
    verify_email_send_command,
    verify_email_send_post_result,
)
from .models import (
    ActionRequest,
    EvaluationPhase,
    GuardDecision,
    GuardOutcome,
    VerifiedEmailSendCommand,
    VerifiedEmailSendOutboxReceipt,
)

POLICY_VERSION = "action-guard-v1"

READ_ACTION_SCOPES = {
    "context.evidence.get": "gbos-read",
    "context.fact.get": "gbos-read",
    "context.fact.list": "gbos-read",
    "context.decision.get": "gbos-read",
}

INTERNAL_PROPOSALS = {
    "internal.ai_draft.propose",
    "internal.fact.propose",
    "internal.work_item.propose",
    "internal.review_case.propose",
}

HUMAN_REQUIRED_ACTIONS = {
    "internal.fact.confirm",
    "internal.work_item.transition",
    "internal.supplier.select",
    "formal.quotation.publish",
    "external.message.send",
    "deal.won",
    "deal.lost",
    "order.create",
    "payment.create",
}

FORBIDDEN_EXACT_ACTIONS = {
    "internal.record.delete",
    "internal.record.archive",
    "direct_database.write",
    "frappe.client.insert",
    "frappe.client.set_value",
    "frappe.client.delete",
}

FORBIDDEN_RESULT_KEYS = {
    "tool_calls",
    "draft_mutation",
    "approved_command",
    "execution",
    "frappe_write",
    "kingdee_mutation",
    "external_send",
    "formal_price",
    "formal_discount",
    "payment",
    "delivery_commitment",
    "order",
    "deal_won",
    "deal_lost",
    "selected_supplier",
    "official_kpi",
}


class ActionGuard:
    """Pure, deterministic policy decision point for Gate 4 actions."""

    policy_version = POLICY_VERSION

    def verify_email_send(
        self,
        command: Mapping[str, Any],
        *,
        authority: EmailSendAuthorityReceipt,
        now: datetime,
    ) -> VerifiedEmailSendCommand:
        """Use the purpose-specific verifier; generic human review never executes send."""

        return verify_email_send_command(command, authority=authority, now=now)

    def verify_email_send_result(
        self,
        verified: VerifiedEmailSendCommand,
        result_payload: Mapping[str, Any],
    ) -> VerifiedEmailSendOutboxReceipt:
        return verify_email_send_post_result(verified, result_payload)

    def evaluate(
        self,
        request: ActionRequest,
        *,
        now: datetime,
        phase: EvaluationPhase = EvaluationPhase.PRE_TOOL,
        result_payload: Mapping[str, Any] | None = None,
    ) -> GuardDecision:
        outcome, reasons = self._classify_request(request)
        if phase is EvaluationPhase.POST_RESULT and outcome is GuardOutcome.ALLOW:
            outcome, reasons = self._validate_result(request, result_payload)
        decision_id = self._decision_id(request, phase, result_payload)
        return GuardDecision(
            guard_decision_id=decision_id,
            request_id=request.request_id,
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            action_type=request.action_type,
            target_revision=request.target_revision,
            evaluation_phase=phase,
            outcome=outcome,
            policy_version=self.policy_version,
            reason_codes=reasons,
            evidence_refs=request.evidence_refs,
            evaluated_at=now,
            correlation_id=request.correlation_id,
        )

    def _classify_request(
        self,
        request: ActionRequest,
    ) -> tuple[GuardOutcome, tuple[str, ...]]:
        if not request.evidence_refs:
            return GuardOutcome.DENY, ("evidence_required",)
        if request.action_type.startswith("kingdee."):
            return GuardOutcome.DENY, ("forbidden_capability",)
        if request.action_type in FORBIDDEN_EXACT_ACTIONS:
            return GuardOutcome.DENY, ("forbidden_capability",)
        if request.action_type in READ_ACTION_SCOPES:
            required_scope = READ_ACTION_SCOPES[request.action_type]
            if required_scope not in request.granted_scopes:
                return GuardOutcome.DENY, ("required_scope_missing",)
            return GuardOutcome.ALLOW, ("scoped_read",)
        if request.action_type in INTERNAL_PROPOSALS:
            if "gbos-propose" not in request.granted_scopes:
                return GuardOutcome.DENY, ("required_scope_missing",)
            return GuardOutcome.ALLOW, ("scoped_internal_proposal",)
        if request.action_type in HUMAN_REQUIRED_ACTIONS:
            if "gbos-propose" not in request.granted_scopes:
                return GuardOutcome.DENY, ("required_scope_missing",)
            return GuardOutcome.REQUIRE_HUMAN, ("human_authorization_required",)
        return GuardOutcome.DENY, ("unknown_action",)

    def _validate_result(
        self,
        request: ActionRequest,
        result_payload: Mapping[str, Any] | None,
    ) -> tuple[GuardOutcome, tuple[str, ...]]:
        if result_payload is None:
            return GuardOutcome.DENY, ("result_required",)
        if _contains_forbidden_result_key(result_payload):
            return GuardOutcome.DENY, ("execution_shape_forbidden",)
        if result_payload.get("site_id") != request.site_id:
            return GuardOutcome.DENY, ("site_mismatch",)
        if result_payload.get("processing_purpose") != request.processing_purpose:
            return GuardOutcome.DENY, ("purpose_mismatch",)
        if result_payload.get("target_revision") != request.target_revision:
            return GuardOutcome.DENY, ("target_revision_mismatch",)
        evidence_refs = result_payload.get("evidence_refs")
        if not isinstance(evidence_refs, list) or tuple(evidence_refs) != request.evidence_refs:
            return GuardOutcome.DENY, ("evidence_mismatch",)
        return GuardOutcome.ALLOW, ("post_result_verified",)

    def _decision_id(
        self,
        request: ActionRequest,
        phase: EvaluationPhase,
        result_payload: Mapping[str, Any] | None,
    ) -> str:
        value = {
            "request_id": request.request_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "action_type": request.action_type,
            "target_ref": request.target_ref,
            "target_revision": request.target_revision,
            "evidence_refs": request.evidence_refs,
            "scopes": request.granted_scopes,
            "payload": dict(request.payload),
            "phase": phase.value,
            "result": None if result_payload is None else dict(result_payload),
            "policy_version": self.policy_version,
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"guard-{hashlib.sha256(encoded).hexdigest()[:32]}"


def _contains_forbidden_result_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).casefold()
            normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
            if normalized in FORBIDDEN_RESULT_KEYS:
                return True
            if _contains_forbidden_result_key(nested):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_result_key(item) for item in value)
    return False
