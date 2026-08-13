from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, request_id, require_roles
from esan_gbos.api.v5.gateway import (
    active_site,
    call_gateway,
    call_observer,
    scope_payload,
    v5_success,
)
from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    build_email_send_approved_command,
)
from esan_gbos.domain.email_review_policy import (
    EMAIL_SEND_PARTICIPANT_ROLES_DIGEST,
    EMAIL_SEND_REVIEW_POLICY,
    EmailSendReviewPolicyError,
    authorize_email_send_owner,
    email_send_participant_roles,
    protect_live_email_send_snapshot,
    protected_user_ref,
)
from esan_gbos.domain.external_identity_projection import owner_eligibility_revision
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
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPAQUE_EMAIL = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_GATEWAY_SNAPSHOT_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "team_ref",
        "assignee_user_name",
        "mailbox_ref",
        "mailbox_config_revision",
        "inbox_item_ref",
        "inbox_item_revision",
        "conversation_ref",
        "conversation_revision",
        "party_ref",
        "owner_user_name",
        "reply_draft_ref",
        "reply_draft_revision",
        "reply_draft_digest",
    }
)
_GATEWAY_AUTHORIZE_FIELDS = frozenset(
    {"gateway_snapshot", "draft_authorization", "draft_evidence_ref"}
)
_GATEWAY_VALIDATE_FIELDS = frozenset({"gateway_snapshot", "participants"})
_APPROVAL_TTL = timedelta(minutes=10)


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
    inbox_item_ref: str,
    draft_ref: str,
    expected_revision: int | str,
    expected_draft_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_SEND_ROLES)
    actor = str(frappe.session.user)
    payload = {
        "inbox_item_ref": _bounded_ref(inbox_item_ref, "inbox_item_ref"),
        "draft_ref": _bounded_ref(draft_ref, "draft_ref"),
        "expected_revision": _positive_integer(expected_revision, "expected_revision"),
        "expected_draft_revision": _positive_integer(
            expected_draft_revision, "expected_draft_revision"
        ),
        "idempotency_key": _bounded_ref(idempotency_key, "idempotency_key", maximum=256),
    }

    def execute() -> dict[str, Any]:
        protected = _derive_submission_snapshot(payload, actor=actor, issued_at=datetime.now(UTC))
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
        payload,
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
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_SEND_ROLES)
    actor = str(frappe.session.user)
    revision = _positive_integer(expected_revision, "expected_revision")
    note = _bounded_note(decision_note)
    payload = {
        "review_case_name": _bounded_ref(review_case_name, "review_case_name"),
        "expected_revision": revision,
        "decision_note": note,
        "idempotency_key": _bounded_ref(idempotency_key, "idempotency_key", maximum=256),
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
        live_authority = _derive_approval_live_snapshot(
            pinned,
            actor=actor,
            issued_at=issued_at,
        )
        authorize_email_send_owner(
            pinned,
            live_snapshot=live_authority,
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


def _derive_submission_snapshot(
    payload: Mapping[str, Any],
    *,
    actor: str,
    issued_at: datetime,
) -> dict[str, Any]:
    authorized = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-send/authority",
        purpose="email_inbox_command",
        payload={
            **scope_payload(business_read=True),
            "phase": "authorize",
            "inbox_item_ref": payload["inbox_item_ref"],
            "draft_ref": payload["draft_ref"],
            "expected_inbox_revision": payload["expected_revision"],
            "expected_draft_revision": payload["expected_draft_revision"],
            "participant_roles_digest": EMAIL_SEND_PARTICIPANT_ROLES_DIGEST,
        },
    )
    authority = _send_authority(authorized, expected_fields=_GATEWAY_AUTHORIZE_FIELDS)
    gateway_snapshot = _gateway_snapshot(authority.get("gateway_snapshot"), actor=actor)
    authorization = authority.get("draft_authorization")
    if not isinstance(authorization, dict):
        raise BFFError("internal_error", "Draft authorization is invalid", status=503)
    draft_evidence_ref = _bounded_ref(
        authority.get("draft_evidence_ref"), "draft_evidence_ref", maximum=512
    )
    finalized = call_observer(
        path="/internal/v1/bff/email-draft-material/finalize",
        purpose="email_draft_material",
        payload={
            "authorization": authorization,
            "draft_evidence_ref": draft_evidence_ref,
            "draft_digest": gateway_snapshot["reply_draft_digest"],
            "draft_revision": gateway_snapshot["reply_draft_revision"],
            "participant_roles": email_send_participant_roles(),
            "idempotency_key": payload["idempotency_key"],
        },
        idempotency_key=str(payload["idempotency_key"]),
    )
    final = _finalized_material(finalized)
    validated = _validate_gateway_authority(
        gateway_snapshot,
        participant_projection=final["participants"],
    )
    return _protect_live(
        _current_live_snapshot(
            validated,
            actor=actor,
            issued_at=issued_at,
            approval_expires_at=issued_at + _APPROVAL_TTL,
            final_mime_evidence_ref=final["evidence_ref"],
            final_mime_digest=final["digest"],
            stable_client_request_id=make_gbos_name("CLI"),
        ),
        actor,
    )


def _derive_approval_live_snapshot(
    pinned: Mapping[str, Any],
    *,
    actor: str,
    issued_at: datetime,
) -> dict[str, Any]:
    expected_gateway = {
        "site_id": pinned["site_id"],
        "processing_purpose": pinned["processing_purpose"],
        "team_ref": pinned["team_ref"],
        "assignee_user_name": actor,
        "mailbox_ref": pinned["mailbox_ref"],
        "mailbox_config_revision": pinned["mailbox_config_revision"],
        "inbox_item_ref": pinned["inbox_item_ref"],
        "inbox_item_revision": pinned["inbox_item_revision"],
        "conversation_ref": pinned["conversation_ref"],
        "conversation_revision": pinned["conversation_revision"],
        "party_ref": pinned["party_ref"],
        "owner_user_name": actor,
        "reply_draft_ref": pinned["reply_draft_ref"],
        "reply_draft_revision": pinned["reply_draft_revision"],
        "reply_draft_digest": pinned["reply_draft_digest"],
    }
    projection = [
        {
            "address_role": item["address_role"],
            "opaque_address_ref": item["opaque_address_ref"],
        }
        for item in pinned["participants"]
    ]
    validated = _validate_gateway_authority(
        _gateway_snapshot(expected_gateway, actor=actor),
        participant_projection=projection,
    )
    return _protect_live(
        _current_live_snapshot(
            validated,
            actor=actor,
            issued_at=issued_at,
            approval_expires_at=_parse_timestamp(pinned["approval_expires_at"]),
            final_mime_evidence_ref=pinned["final_mime_evidence_ref"],
            final_mime_digest=pinned["final_mime_digest"],
            stable_client_request_id=pinned["stable_client_request_id"],
        ),
        actor,
    )


def _validate_gateway_authority(
    expected_gateway_snapshot: dict[str, Any],
    *,
    participant_projection: object,
) -> dict[str, Any]:
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-send/authority",
        purpose="email_inbox_command",
        payload={
            **scope_payload(business_read=True),
            "phase": "validate",
            "expected_gateway_snapshot": expected_gateway_snapshot,
            "participant_projection": participant_projection,
        },
    )
    authority = _send_authority(data, expected_fields=_GATEWAY_VALIDATE_FIELDS)
    gateway_snapshot = _gateway_snapshot(
        authority.get("gateway_snapshot"),
        actor=expected_gateway_snapshot["owner_user_name"],
    )
    if gateway_snapshot != expected_gateway_snapshot:
        raise BFFError("authority_conflict", "Gateway authority changed", status=409)
    participants = _mapped_participants(authority.get("participants"))
    return {"gateway_snapshot": gateway_snapshot, "participants": participants}


def _current_live_snapshot(
    validated: Mapping[str, Any],
    *,
    actor: str,
    issued_at: datetime,
    approval_expires_at: datetime,
    final_mime_evidence_ref: object,
    final_mime_digest: object,
    stable_client_request_id: object,
) -> dict[str, Any]:
    gateway = _gateway_snapshot(validated.get("gateway_snapshot"), actor=actor)
    participants = _mapped_participants(validated.get("participants"))
    recipient = participants[1]
    authority = _current_frappe_party_authority(
        mapping_ref=recipient["identity_mapping_ref"],
        expected_mapping_revision=recipient["identity_mapping_revision"],
        expected_team_ref=gateway["team_ref"],
        expected_party_ref=gateway["party_ref"],
        actor=actor,
    )
    if issued_at.tzinfo is None or approval_expires_at.tzinfo is None:
        raise BFFError("internal_error", "Email approval clock is invalid", status=503)
    return {
        "schema_version": "1.0",
        **gateway,
        "participants": participants,
        "party_revision": authority["party_revision"],
        "team_revision": authority["team_revision"],
        "owner_eligibility_revision": authority["owner_eligibility_revision"],
        "approval_expires_at": approval_expires_at.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "final_mime_evidence_ref": _bounded_ref(final_mime_evidence_ref, "final_mime_evidence_ref"),
        "final_mime_digest": _bounded_digest(final_mime_digest, "final_mime_digest"),
        "evidence_refs": [_bounded_ref(final_mime_evidence_ref, "final_mime_evidence_ref")],
        "stable_client_request_id": _bounded_ref(
            stable_client_request_id, "stable_client_request_id"
        ),
    }


def _current_frappe_party_authority(
    *,
    mapping_ref: object,
    expected_mapping_revision: object,
    expected_team_ref: object,
    expected_party_ref: object,
    actor: str,
) -> dict[str, Any]:
    rows = frappe.db.sql(
        """
        select mapping.`name` as `mapping_ref`, mapping.`revision` as `mapping_revision`,
               mapping.`team` as `team_ref`, mapping.`identity_type`,
               mapping.`party_profile` as `party_ref`, mapping.`review_status`,
               mapping.`business_status`, party.`revision` as `party_revision`,
               party.`team` as `party_team_ref`,
               party.`business_status` as `party_status`,
               party.`review_status` as `party_review_status`,
               party.`owner_user` as `owner_user_ref`, team.`revision` as `team_revision`,
               team.`business_status` as `team_status`,
               team.`review_status` as `team_review_status`, user.`enabled` as `owner_enabled`,
               user.`user_type` as `owner_user_type`, member.`name` as `membership_ref`,
               member.`parent` as `membership_parent`, member.`user` as `membership_user`,
               member.`enabled` as `membership_enabled`, member.`modified` as `membership_modified`
          from `tabGBOS External Identity` mapping
          join `tabGBOS Party Profile` party on party.`name` = mapping.`party_profile`
          join `tabGBOS Team` team on team.`name` = party.`team`
          join `tabUser` user on user.`name` = party.`owner_user`
          join `tabGBOS Team Member` member on member.`parent` = party.`team`
               and member.`user` = party.`owner_user`
         where mapping.`name` = %(mapping_ref)s limit 3 for update
        """,
        {"mapping_ref": mapping_ref},
        as_dict=True,
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise BFFError("authority_conflict", "Party authority is unavailable", status=409)
    row = dict(rows[0])
    if (
        row.get("mapping_ref") != mapping_ref
        or row.get("mapping_revision") != expected_mapping_revision
        or row.get("team_ref") != expected_team_ref
        or row.get("identity_type") != "Party"
        or row.get("party_ref") != expected_party_ref
        or row.get("party_team_ref") != expected_team_ref
        or row.get("review_status") != "Approved"
        or row.get("business_status") != "Active"
        or row.get("party_status") != "Active"
        or row.get("party_review_status") != "Approved"
        or row.get("team_status") != "Active"
        or row.get("team_review_status") != "Approved"
        or row.get("owner_user_ref") != actor
        or row.get("owner_enabled") != 1
        or row.get("owner_user_type") != "System User"
        or row.get("membership_parent") != expected_team_ref
        or row.get("membership_user") != actor
        or row.get("membership_enabled") != 1
    ):
        raise BFFError("authority_conflict", "Party authority changed", status=409)
    party_revision = _positive_integer(row.get("party_revision"), "party_revision")
    team_revision = _positive_integer(row.get("team_revision"), "team_revision")
    eligibility = owner_eligibility_revision(
        {
            "name": expected_party_ref,
            "revision": party_revision,
            "team": expected_team_ref,
            "owner_user": actor,
        },
        {
            "team_revision": team_revision,
            "owner_enabled": row.get("owner_enabled"),
            "owner_user_type": row.get("owner_user_type"),
            "membership_ref": row.get("membership_ref"),
            "membership_parent": row.get("membership_parent"),
            "membership_user": row.get("membership_user"),
            "membership_enabled": row.get("membership_enabled"),
            "membership_modified": row.get("membership_modified"),
        },
    )
    return {
        "party_revision": party_revision,
        "team_revision": team_revision,
        "owner_eligibility_revision": eligibility,
    }


def _send_authority(
    value: object,
    *,
    expected_fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"send_authority"}:
        raise BFFError("internal_error", "Gateway authority response is invalid", status=503)
    authority = value["send_authority"]
    if not isinstance(authority, dict) or set(authority) != expected_fields:
        raise BFFError("internal_error", "Gateway authority response is invalid", status=503)
    return authority


def _gateway_snapshot(value: object, *, actor: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _GATEWAY_SNAPSHOT_FIELDS:
        raise BFFError("internal_error", "Gateway authority snapshot is invalid", status=503)
    result = dict(value)
    if (
        result.get("site_id") != active_site()
        or result.get("assignee_user_name") != actor
        or result.get("owner_user_name") != actor
        or _DIGEST.fullmatch(str(result.get("reply_draft_digest"))) is None
    ):
        raise BFFError("authority_conflict", "Gateway authority changed", status=409)
    return result


def _finalized_material(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "evidence_ref",
        "digest",
        "role_binding",
        "participants",
    }:
        raise BFFError("internal_error", "Final email material response is invalid", status=503)
    if value.get("role_binding") != EMAIL_SEND_PARTICIPANT_ROLES_DIGEST:
        raise BFFError("authority_conflict", "Participant role binding changed", status=409)
    return dict(value)


def _mapped_participants(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise BFFError("authority_conflict", "Participant authority is invalid", status=409)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        expected = (
            {"address_role", "opaque_address_ref"}
            if index == 0
            else {
                "address_role",
                "opaque_address_ref",
                "identity_mapping_ref",
                "identity_mapping_revision",
            }
        )
        if not isinstance(item, dict) or set(item) != expected:
            raise BFFError("authority_conflict", "Participant authority is invalid", status=409)
        if item.get("address_role") != ("sender" if index == 0 else "to") or (
            not isinstance(item.get("opaque_address_ref"), str)
            or _OPAQUE_EMAIL.fullmatch(item["opaque_address_ref"]) is None
        ):
            raise BFFError("authority_conflict", "Participant authority is invalid", status=409)
        normalized = dict(item)
        if index == 1:
            normalized["identity_mapping_ref"] = _bounded_ref(
                item.get("identity_mapping_ref"), "identity_mapping_ref"
            )
            normalized["identity_mapping_revision"] = _positive_integer(
                item.get("identity_mapping_revision"), "identity_mapping_revision"
            )
        result.append(normalized)
    return result


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise BFFError("authority_conflict", "Approval timestamp is invalid", status=409)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BFFError("authority_conflict", "Approval timestamp is invalid", status=409) from None
    if parsed.tzinfo is None:
        raise BFFError("authority_conflict", "Approval timestamp is invalid", status=409)
    return parsed.astimezone(UTC)


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


def _bounded_ref(value: object, field: str, *, maximum: int = 140) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BFFError("invalid_dto", f"{field} is invalid")
    return value


def _bounded_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BFFError("authority_conflict", f"{field} is invalid", status=409)
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
    if not 1 <= result <= 2_147_483_647:
        raise BFFError("invalid_dto", f"{field} must be a positive integer")
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


__all__ = ["approve", "submit_for_review"]
