from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, request_id, require_roles
from esan_gbos.api.v5.gateway import active_site, v5_success
from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    build_email_send_approved_command,
)
from esan_gbos.domain.email_review_policy import (
    EMAIL_SEND_REVIEW_POLICY,
    EmailSendReviewPolicyError,
    authorize_email_send_owner,
    protect_live_email_send_snapshot,
    protected_user_ref,
)
from esan_gbos.domain.naming import make_gbos_name
from esan_gbos.domain.review_dto import canonical_payload_hash
from esan_gbos.gbos.doctype.gbos_email_send_approval.gbos_email_send_approval import (
    approval_snapshot,
)
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)

EMAIL_SEND_ROLES = frozenset({"Sales User", "Sales Manager", "Reviewer", "GBOS Admin"})
_REVIEW_CASE_NAME = re.compile(r"^REV-(?P<ulid>[0-9A-HJKMNP-TV-Z]{26})$")


def email_command_permission_query(user: str | None = None) -> str:
    del user
    return "1=0"


def has_email_command_permission(
    doc: object,
    user: str | None = None,
    permission_type: str | None = None,
    ptype: str | None = None,
    **kwargs: object,
) -> bool:
    del doc, user, permission_type, ptype, kwargs
    return False


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def submit_for_review(
    live_authority_snapshot: str | dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_SEND_ROLES)
    actor = str(frappe.session.user)
    protected = _protect_live(live_authority_snapshot, actor)

    def execute() -> dict[str, Any]:
        approval = frappe.get_doc(_approval_values(protected)).insert(ignore_permissions=True)
        subject_snapshot = build_subject_snapshot(approval)
        subject_digest = canonical_payload_hash(subject_snapshot)
        if approval.payload_sha256 != canonical_payload_hash(approval_snapshot(approval)):
            raise BFFError("authority_conflict", "Approval subject hash is invalid", status=409)
        provisional = frappe.get_doc(
            {
                "doctype": "GBOS Review Case",
                "title": "Email send approval",
                "team": protected["team_ref"],
                "assigned_reviewer": actor,
                "subject_doctype": "GBOS Email Send Approval",
                "subject_name": approval.name,
                "subject_revision": int(approval.revision),
                "subject_payload_sha256": subject_digest,
                "subject_snapshot": _json(subject_snapshot),
                "evidence_refs": _json(protected["evidence_refs"]),
                "policy_version": EMAIL_SEND_REVIEW_POLICY,
                "approval_expires_at": protected["approval_expires_at"],
                "origin": "Integration",
                "origin_reference": protected["stable_client_request_id"],
                "business_status": "Pending",
                "review_status": "Pending",
            }
        )
        provisional.case_payload_sha256 = canonical_payload_hash(build_case_payload(provisional))
        case = provisional.insert(ignore_permissions=True)
        return _submit_receipt(case, approval)

    result, replayed, original_request_id = run_idempotent(
        "email_send.submit_for_review",
        idempotency_key,
        {"approval": protected, "idempotency_key": idempotency_key},
        execute,
        api_version="v5",
    )
    return v5_success(result, replayed=replayed, original_request_id=original_request_id)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def approve(
    review_case_name: str,
    expected_revision: int | str,
    decision_note: str,
    live_authority_snapshot: str | dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_SEND_ROLES)
    actor = str(frappe.session.user)
    protected = _protect_live(live_authority_snapshot, actor)
    revision = _positive_integer(expected_revision, "expected_revision")
    note = _bounded_note(decision_note)
    payload = {
        "review_case_name": _bounded_ref(review_case_name, "review_case_name"),
        "expected_revision": revision,
        "decision_note": note,
        "live_authority": protected,
        "idempotency_key": idempotency_key,
    }

    def execute() -> dict[str, Any]:
        return _approve_locked(
            payload=payload,
            actor=actor,
            issued_at=datetime.now(UTC),
        )

    result, replayed, original_request_id = run_idempotent(
        "email_send.approve",
        idempotency_key,
        payload,
        execute,
        api_version="v5",
    )
    return v5_success(result, replayed=replayed, original_request_id=original_request_id)


def _approve_locked(
    *,
    payload: dict[str, Any],
    actor: str,
    issued_at: datetime,
) -> dict[str, Any]:
    case = frappe.get_doc("GBOS Review Case", payload["review_case_name"], for_update=True)
    if (
        case.subject_doctype != "GBOS Email Send Approval"
        or case.policy_version != EMAIL_SEND_REVIEW_POLICY
    ):
        raise BFFError(
            "specialized_review_required",
            "Review Case is not an email send approval",
            status=409,
        )
    if case.business_status != "Pending" or case.review_status != "Pending":
        raise BFFError("review_not_pending", "Review Case is no longer pending", status=409)
    if int(case.revision or 0) != payload["expected_revision"]:
        raise BFFError("revision_conflict", "Review Case revision is stale", status=409)
    approval = frappe.get_doc("GBOS Email Send Approval", case.subject_name, for_update=True)
    pinned = approval_snapshot(approval)
    if canonical_payload_hash(pinned) != approval.payload_sha256:
        raise BFFError("authority_conflict", "Approval subject hash is stale", status=409)
    subject_snapshot = build_subject_snapshot(approval)
    if (
        int(case.subject_revision or 0) != int(approval.revision or 0)
        or canonical_payload_hash(subject_snapshot) != case.subject_payload_sha256
        or canonical_payload_hash(build_case_payload(case)) != case.case_payload_sha256
    ):
        raise BFFError("authority_conflict", "Review Case pins are stale", status=409)
    try:
        authorize_email_send_owner(
            pinned,
            live_snapshot=payload["live_authority"],
            actor_user_ref=protected_user_ref(pinned["site_id"], actor),
            assigned_reviewer=protected_user_ref(pinned["site_id"], case.assigned_reviewer),
            case_team_ref=case.team,
            case_policy_version=case.policy_version,
            now=issued_at,
        )
    except EmailSendReviewPolicyError as error:
        raise BFFError("authority_conflict", error.reason_code, status=409) from error

    command_id = make_gbos_name("CMD")
    current_request_id = request_id()
    anticipated_case_revision = int(case.revision) + 1
    try:
        command = build_email_send_approved_command(
            pinned,
            command_id=command_id,
            actor_user_ref=protected_user_ref(pinned["site_id"], actor),
            review_case_ref=_review_case_ref(case.name),
            review_case_revision=anticipated_case_revision,
            request_id=current_request_id,
            idempotency_key=payload["idempotency_key"],
            issued_at=issued_at,
        )
    except ApprovedCommandValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    decision_payload = {
        "review_case": case.name,
        "decision": "Approved",
        "reviewer_ref": command["actor_user_ref"],
        "reason": payload["decision_note"],
        "case_revision": int(case.revision),
        "case_payload_sha256": case.case_payload_sha256,
        "subject_name": approval.name,
        "subject_revision": int(approval.revision),
        "subject_payload_sha256": case.subject_payload_sha256,
        "evidence_refs": pinned["evidence_refs"],
        "policy_version": EMAIL_SEND_REVIEW_POLICY,
        "request_id": current_request_id,
    }
    decision_hash = canonical_payload_hash(decision_payload)
    decision = frappe.get_doc(
        {
            "doctype": "GBOS Review Decision",
            "review_case": case.name,
            "decision": "Approved",
            "reviewer": actor,
            "reason": payload["decision_note"],
            "case_revision": int(case.revision),
            "case_payload_sha256": case.case_payload_sha256,
            "subject_doctype": case.subject_doctype,
            "subject_name": approval.name,
            "subject_revision": int(approval.revision),
            "subject_payload_sha256": case.subject_payload_sha256,
            "subject_snapshot": case.subject_snapshot,
            "evidence_refs": case.evidence_refs,
            "policy_version": EMAIL_SEND_REVIEW_POLICY,
            "payload_sha256": decision_hash,
            "request_id": current_request_id,
            "decided_at": issued_at,
        }
    ).insert(ignore_permissions=True)
    approved_command = frappe.get_doc(
        {
            "doctype": "GBOS Approved Command",
            "review_case": case.name,
            "email_send_approval": approval.name,
            "actor_user_ref": command["actor_user_ref"],
            "policy_version": EMAIL_SEND_REVIEW_POLICY,
            "command_type": command["command_type"],
            "command_payload": _json(command),
            "payload_sha256": command["payload_sha256"],
            "idempotency_key": command["idempotency_key"],
            "stable_client_request_id": command["stable_client_request_id"],
            "issued_at": command["issued_at"],
            "expires_at": command["approval_expires_at"],
        }
    ).insert(ignore_permissions=True, set_name=command_id)
    publication = frappe.get_doc(
        {
            "doctype": "GBOS Command Publication",
            "approved_command": approved_command.name,
            "command_payload": _json(command),
            "payload_digest": "sha256:" + command["payload_sha256"],
            "publication_status": "Pending",
            "attempt": 0,
            "generation": 0,
            "max_attempts": 5,
        }
    ).insert(ignore_permissions=True)

    case.flags.gbos_review_command = True
    case.business_status = "Approved"
    case.review_status = "Approved"
    case.decision_note = payload["decision_note"]
    case.decided_by = actor
    case.decided_at = issued_at
    case.decision_record = decision.name
    case.decision_payload_sha256 = decision_hash
    case.approved_command = approved_command.name
    case.command_publication = publication.name
    case.last_request_id = current_request_id
    case.save(ignore_permissions=True)
    if int(case.revision) != anticipated_case_revision:
        raise BFFError("revision_conflict", "Review Case revision changed unexpectedly", status=409)

    approval.flags.gbos_email_send_decision = True
    approval.business_status = "Approved"
    approval.review_status = "Approved"
    approval.last_request_id = current_request_id
    approval.save(ignore_permissions=True)
    return {
        "review_case_ref": case.name,
        "review_case_revision": int(case.revision),
        "email_send_approval_ref": approval.name,
        "approved_command_ref": approved_command.name,
        "command_publication_ref": publication.name,
        "payload_digest": publication.payload_digest,
        "status": "approved",
    }


def _approval_values(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "doctype": "GBOS Email Send Approval",
        "site_id": snapshot["site_id"],
        "processing_purpose": snapshot["processing_purpose"],
        "team": snapshot["team_ref"],
        "assignee_user_ref": snapshot["assignee_user_ref"],
        "approval_expires_at": snapshot["approval_expires_at"],
        "mailbox_ref": snapshot["mailbox_ref"],
        "mailbox_config_revision": snapshot["mailbox_config_revision"],
        "inbox_item_ref": snapshot["inbox_item_ref"],
        "inbox_item_revision": snapshot["inbox_item_revision"],
        "conversation_ref": snapshot["conversation_ref"],
        "conversation_revision": snapshot["conversation_revision"],
        "reply_draft_ref": snapshot["reply_draft_ref"],
        "reply_draft_revision": snapshot["reply_draft_revision"],
        "reply_draft_digest": snapshot["reply_draft_digest"],
        "participants": _json(snapshot["participants"]),
        "party_ref": snapshot["party_ref"],
        "party_revision": snapshot["party_revision"],
        "team_revision": snapshot["team_revision"],
        "owner_user_ref": snapshot["owner_user_ref"],
        "owner_eligibility_revision": snapshot["owner_eligibility_revision"],
        "final_mime_evidence_ref": snapshot["final_mime_evidence_ref"],
        "final_mime_digest": snapshot["final_mime_digest"],
        "evidence_refs": _json(snapshot["evidence_refs"]),
        "stable_client_request_id": snapshot["stable_client_request_id"],
        "origin": "Integration",
        "origin_reference": snapshot["stable_client_request_id"],
        "business_status": "Pending",
        "review_status": "Pending",
    }


def _protect_live(value: str | dict[str, Any], actor: str) -> dict[str, Any]:
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    try:
        return protect_live_email_send_snapshot(
            parsed,
            site_id=active_site(),
            authenticated_user_name=actor,
        )
    except EmailSendReviewPolicyError as error:
        raise BFFError("authority_conflict", error.reason_code, status=409) from error


def _review_case_ref(name: str) -> str:
    match = _REVIEW_CASE_NAME.fullmatch(name)
    if match is None:
        raise BFFError("authority_conflict", "Review Case identity is invalid", status=409)
    return "RVC-" + match.group("ulid")


def _submit_receipt(case: Any, approval: Any) -> dict[str, Any]:
    return {
        "review_case_ref": case.name,
        "review_case_revision": int(case.revision),
        "email_send_approval_ref": approval.name,
        "approval_revision": int(approval.revision),
        "approval_expires_at": str(approval.approval_expires_at),
        "status": "pending",
    }


def _bounded_ref(value: object, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 140 or value != value.strip():
        raise BFFError("invalid_dto", f"{field} is invalid")
    return value


def _bounded_note(value: object) -> str:
    if not isinstance(value, str):
        raise BFFError("invalid_dto", "decision_note is invalid")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 2000:
        raise BFFError("invalid_dto", "decision_note is invalid")
    return normalized


def _positive_integer(value: int | str, field: str) -> int:
    if isinstance(value, bool):
        raise BFFError("invalid_dto", f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_dto", f"{field} must be a positive integer") from error
    if result < 1:
        raise BFFError("invalid_dto", f"{field} must be a positive integer")
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = ["approve", "submit_for_review"]
