"""Closed Frappe authority reads for the independent Email Gateway."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import frappe

from esan_gbos.domain.approved_command import (
    ApprovedCommandValidationError,
    validate_email_send_approved_command,
)
from esan_gbos.domain.email_review_policy import (
    EMAIL_SEND_REVIEW_POLICY,
    EmailSendReviewPolicyError,
    protected_user_ref,
    validate_email_send_approval_snapshot,
)
from esan_gbos.domain.external_identity_projection import (
    ExternalIdentityProjectionError,
    build_external_identity_projection,
    owner_eligibility_revision,
)
from esan_gbos.domain.permissions import EMAIL_GATEWAY_AUTHORITY_ROLE
from esan_gbos.domain.review_dto import ReviewDTOValidationError, canonical_payload_hash
from esan_gbos.email_gateway_authority_access import (
    email_gateway_authority_permission_scope,
    require_email_gateway_authority_scope,
)

_ROLE = EMAIL_GATEWAY_AUTHORITY_ROLE
_PURPOSE = "email_gateway_authority"
_ALLOWED_RUNTIME_ROLES = frozenset({_ROLE, "All", "Guest", "Website User"})
_TEXT = re.compile(r"[^\x00-\x1f\x7f]+")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,139}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@~-]{0,255}")
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_PROJECT_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "mapping_ref",
        "expected_mapping_revision",
        "expected_team_ref",
    }
)
_ROUTE_FIELDS = frozenset(
    {
        *_PROJECT_FIELDS,
        "expected_party_revision",
        "expected_team_revision",
        "expected_owner_eligibility_revision",
    }
)
_COMMAND_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "publication_ref",
        "attempt",
        "generation",
        "fence_token",
        "command_ref",
        "payload_digest",
    }
)
_INBOX_COMMAND_ROLES = frozenset({"Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_INBOX_SUPERVISOR_ROLES = frozenset({"Sales Manager", "Reviewer", "GBOS Admin"})


class _APIError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class InboxCommandAuthorityConflict(ValueError):
    """The live actor, target, or business authority could not be proven."""


def _endpoint[Endpoint: Callable[[dict[str, Any]], dict[str, Any]]](
    function: Endpoint,
) -> Endpoint:
    @wraps(function)
    def wrapped(payload: str | dict[str, Any]) -> dict[str, Any]:
        try:
            _set_no_store()
            frappe.local.response.pop("http_status_code", None)
            return function(_object(payload))
        except _APIError as error:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = error.status
            return {"error": {"code": error.code}}
        except frappe.PermissionError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 403
            return {"error": {"code": "permission_denied"}}
        except Exception:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 500
            return {"error": {"code": "internal_error"}}

    return wrapped  # type: ignore[return-value]


def _set_no_store() -> None:
    headers = frappe.local.response.get("headers")
    if not isinstance(headers, dict):
        headers = {}
        frappe.local.response["headers"] = headers
    headers["Cache-Control"] = "no-store"


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def project(payload: dict[str, Any]) -> dict[str, Any]:
    request = _validated_request(payload, _PROJECT_FIELDS)
    with email_gateway_authority_permission_scope(
        request_id=request["request_id"], auth_ref=request["auth_ref"]
    ):
        rows = _mapping_rows(request["mapping_ref"])
        if len(rows) != 1:
            raise _APIError("mapping_not_resolved", 404)
        try:
            projection = build_external_identity_projection(rows[0])
        except ExternalIdentityProjectionError:
            raise _APIError("mapping_not_resolved", 404) from None
        if (
            projection["mapping_revision"] != request["expected_mapping_revision"]
            or projection["team_ref"] != request["expected_team_ref"]
        ):
            raise _APIError("mapping_not_resolved", 404)
        if rows[0].get("target_eligible") != 1 and projection["status"] == "confirmed":
            raise _APIError("mapping_not_resolved", 404)
    return {"identity_projection": projection}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def resolve_route(payload: dict[str, Any]) -> dict[str, Any]:
    request = _validated_request(payload, _ROUTE_FIELDS)
    with email_gateway_authority_permission_scope(
        request_id=request["request_id"], auth_ref=request["auth_ref"]
    ):
        rows = _route_rows(request["mapping_ref"])
        decision = _route_decision(rows, request)
    return {"route_authority": decision}


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def resolve_email_send_command(payload: dict[str, Any]) -> dict[str, Any]:
    request = _validated_request(payload, _COMMAND_FIELDS)
    with email_gateway_authority_permission_scope(
        request_id=request["request_id"], auth_ref=request["auth_ref"]
    ):
        try:
            authority = _email_send_command_authority(request)
        except _APIError:
            raise
        except (
            ApprovedCommandValidationError,
            EmailSendReviewPolicyError,
            ExternalIdentityProjectionError,
            ReviewDTOValidationError,
            frappe.DoesNotExistError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            raise _APIError("email_send_authority_unavailable", 409) from None
    return {"email_send_authority": authority}


def derive_inbox_command_authority(
    *,
    command: str,
    inbox_item_ref: str,
    expected_inbox_revision: int,
    target_user_ref: str | None = None,
    business_ref: str | None = None,
) -> dict[str, Any]:
    """Derive one closed command receipt inside the authenticated Frappe request."""

    try:
        return _derive_inbox_command_authority(
            command=command,
            inbox_item_ref=inbox_item_ref,
            expected_inbox_revision=expected_inbox_revision,
            target_user_ref=target_user_ref,
            business_ref=business_ref,
        )
    except InboxCommandAuthorityConflict:
        raise
    except (
        _APIError,
        frappe.DoesNotExistError,
        frappe.PermissionError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise InboxCommandAuthorityConflict("authority_conflict") from None


def _derive_inbox_command_authority(
    *,
    command: str,
    inbox_item_ref: str,
    expected_inbox_revision: int,
    target_user_ref: str | None = None,
    business_ref: str | None = None,
) -> dict[str, Any]:

    actor = str(getattr(frappe.session, "user", ""))
    if (
        command not in {"claim", "reassign", "link_business"}
        or not actor
        or actor == "Guest"
        or isinstance(expected_inbox_revision, bool)
        or expected_inbox_revision < 1
    ):
        raise _APIError("authority_conflict", 409)
    rows = frappe.db.sql(
        """
        select actor.`name` as `actor_ref`, actor.`enabled` as `actor_enabled`,
               actor.`modified` as `actor_modified`,
               actor_member.`parent` as `actor_team_ref`,
               actor_member.`enabled` as `actor_membership_enabled`,
               actor_member.`modified` as `actor_membership_modified`,
               target.`name` as `target_user_ref`, target.`enabled` as `target_enabled`,
               target.`modified` as `target_modified`,
               target_member.`parent` as `target_team_ref`,
               target_member.`enabled` as `target_membership_enabled`,
               target_member.`modified` as `target_membership_modified`,
               actor_role.`role` as `actor_role`,
               actor.`modified` as `actor_eligibility_revision`
          from `tabUser` actor
          left join `tabHas Role` actor_role
            on actor_role.`parent` = actor.`name`
           and actor_role.`parenttype` = 'User'
           and actor_role.`parentfield` = 'roles'
          left join `tabGBOS Team Member` actor_member
            on actor_member.`user` = actor.`name`
          left join `tabUser` target on target.`name` = %(target_user_ref)s
          left join `tabGBOS Team Member` target_member
            on target_member.`user` = target.`name`
         where actor.`name` = %(actor_ref)s
         limit 201
         for update
        """,
        {"actor_ref": actor, "target_user_ref": target_user_ref},
        as_dict=True,
    )
    normalized = _rows(rows)
    if not normalized or len(normalized) > 200:
        raise _APIError("authority_conflict", 409)
    roles = sorted(
        {
            str(row.get("actor_role"))
            for row in normalized
            if isinstance(row.get("actor_role"), str)
            and row.get("actor_role") in _INBOX_COMMAND_ROLES
        }
    )
    required_roles = _INBOX_SUPERVISOR_ROLES if command == "reassign" else _INBOX_COMMAND_ROLES
    actor_teams = sorted(
        {
            _row_ref(row.get("actor_team_ref"))
            for row in normalized
            if row.get("actor_membership_enabled") == 1
        }
    )
    if (
        any(row.get("actor_ref") != actor or row.get("actor_enabled") != 1 for row in normalized)
        or not set(roles) & required_roles
        or not actor_teams
    ):
        raise _APIError("authority_conflict", 409)
    actor_revision = _authority_revision(
        {
            "actor_ref": actor,
            "actor_modified": normalized[0].get("actor_modified"),
            "roles": roles,
            "teams": [
                [row.get("actor_team_ref"), row.get("actor_membership_modified")]
                for row in normalized
                if row.get("actor_membership_enabled") == 1
            ],
        }
    )
    target_teams: list[str] = []
    target_revision: str | None = None
    if target_user_ref is not None:
        target_teams = sorted(
            {
                _row_ref(row.get("target_team_ref"))
                for row in normalized
                if row.get("target_membership_enabled") == 1
            }
        )
        if (
            any(
                row.get("target_user_ref") != target_user_ref or row.get("target_enabled") != 1
                for row in normalized
            )
            or not target_teams
        ):
            raise _APIError("authority_conflict", 409)
        target_revision = _authority_revision(
            {
                "target_user_ref": target_user_ref,
                "target_modified": normalized[0].get("target_modified"),
                "teams": [
                    [row.get("target_team_ref"), row.get("target_membership_modified")]
                    for row in normalized
                    if row.get("target_membership_enabled") == 1
                ],
            }
        )
    business_team_ref: str | None = None
    business_revision: int | str | None = None
    if business_ref is not None:
        business_team_ref, business_revision = _business_link_authority(business_ref)
    return {
        "schema_version": "1.0",
        "command": command,
        "actor_ref_digest": _user_authority_digest(actor),
        "actor_roles": roles,
        "actor_team_refs": actor_teams,
        "actor_eligibility_revision": actor_revision,
        "inbox_item_ref": _ref(inbox_item_ref),
        "expected_inbox_revision": expected_inbox_revision,
        "target_user_ref_digest": (
            None if target_user_ref is None else _user_authority_digest(target_user_ref)
        ),
        "target_team_refs": target_teams,
        "target_eligibility_revision": target_revision,
        "business_ref": business_ref,
        "business_team_ref": business_team_ref,
        "business_revision": business_revision,
    }


def _business_link_authority(business_ref: str) -> tuple[str, int | str]:
    reference = _ref(business_ref)
    if reference.startswith("PTY-"):
        doctype, team_field = "GBOS Party Profile", "team"
    elif reference.startswith("CNT-"):
        doctype, team_field = "Contact", "custom_esan_team"
    elif reference.startswith("CRM-LEAD-"):
        doctype, team_field = "CRM Lead", "custom_esan_team"
    elif reference.startswith("CRM-DEAL-"):
        doctype, team_field = "CRM Deal", "custom_esan_team"
    else:
        raise _APIError("authority_conflict", 409)
    document = frappe.get_doc(doctype, reference, for_update=True)
    team = _row_ref(getattr(document, team_field, None))
    if doctype == "GBOS Party Profile":
        if (
            getattr(document, "business_status", None) != "Active"
            or getattr(document, "review_status", None) != "Approved"
        ):
            raise _APIError("authority_conflict", 409)
        revision: int | str = _row_positive_integer(getattr(document, "revision", None))
    else:
        revision = _authority_revision(
            {
                "doctype": doctype,
                "name": reference,
                "team": team,
                "modified": getattr(document, "modified", None),
            }
        )
    return team, revision


def _authority_revision(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _user_authority_digest(user_ref: str) -> str:
    return _authority_revision(
        {"site_id": str(getattr(frappe.local, "site", "")), "user_ref": user_ref}
    )


def _email_send_command_authority(request: Mapping[str, Any]) -> dict[str, Any]:
    require_email_gateway_authority_scope()
    try:
        publication = frappe.get_doc(
            "GBOS Command Publication", request["publication_ref"], for_update=True
        )
        approved = frappe.get_doc("GBOS Approved Command", request["command_ref"], for_update=True)
        case = frappe.get_doc("GBOS Review Case", approved.review_case, for_update=True)
        approval = frappe.get_doc(
            "GBOS Email Send Approval", approved.email_send_approval, for_update=True
        )
        command = validate_email_send_approved_command(frappe.parse_json(approved.command_payload))
        publication_command = validate_email_send_approved_command(
            frappe.parse_json(publication.command_payload)
        )
        current_approval = validate_email_send_approval_snapshot(_approval_snapshot(approval))
        expires_at = _document_time(command["approval_expires_at"])
        lease_expires_at = _document_time(publication.lease_expires_at)
        approved_issued_at = _document_time(approved.issued_at)
        approved_expires_at = _document_time(approved.expires_at)
    except (
        ApprovedCommandValidationError,
        EmailSendReviewPolicyError,
        frappe.DoesNotExistError,
        ValueError,
        TypeError,
    ):
        raise _APIError("email_send_authority_unavailable", 409) from None

    payload_digest = "sha256:" + command["payload_sha256"]
    if (
        publication.name != request["publication_ref"]
        or command["site_id"] != request["site_id"]
        or publication.publication_status != "Claimed"
        or publication.approved_command != approved.name
        or approved.name != request["command_ref"]
        or command["command_id"] != approved.name
        or int(publication.attempt or 0) != request["attempt"]
        or int(publication.generation or 0) != request["generation"]
        or publication.fence_token != request["fence_token"]
        or lease_expires_at <= datetime.now().astimezone()
        or publication.payload_digest != request["payload_digest"]
        or request["payload_digest"] != payload_digest
        or publication_command != command
        or approved.payload_sha256 != command["payload_sha256"]
        or approved.actor_user_ref != command["actor_user_ref"]
        or approved.policy_version != command["review_policy_version"]
        or approved.command_type != command["command_type"]
        or approved.idempotency_key != command["idempotency_key"]
        or approved.stable_client_request_id != command["stable_client_request_id"]
        or approved_issued_at != _document_time(command["issued_at"])
        or approved_expires_at != expires_at
        or case.subject_doctype != "GBOS Email Send Approval"
        or case.last_request_id != command["request_id"]
        or case.subject_name != approval.name
        or case.approved_command != approved.name
        or case.command_publication != publication.name
        or case.business_status != "Approved"
        or case.review_status != "Approved"
        or int(case.revision or 0) != command["review_case_revision"]
        or _review_case_ref(case.name) != command["review_case_ref"]
        or case.policy_version != EMAIL_SEND_REVIEW_POLICY
        or case.policy_version != command["review_policy_version"]
        or case.team != command["team_ref"]
        or case.assigned_reviewer != case.decided_by
        or protected_user_ref(command["site_id"], case.assigned_reviewer)
        != command["actor_user_ref"]
        or approval.business_status != "Approved"
        or approval.review_status != "Approved"
        or approval.last_request_id != command["request_id"]
        or int(approval.revision or 0) != int(case.subject_revision or 0) + 1
        or approval.payload_sha256 != canonical_payload_hash(current_approval)
        or current_approval != _command_approval_snapshot(command)
        or expires_at <= datetime.now().astimezone()
        or _pinned_subject_snapshot(case)
        != _email_approval_subject_snapshot(approval, revision=int(case.subject_revision or 0))
        or canonical_payload_hash(_pinned_subject_snapshot(case)) != case.subject_payload_sha256
        or canonical_payload_hash(_review_case_payload(case)) != case.case_payload_sha256
    ):
        raise _APIError("email_send_authority_unavailable", 409)

    route = _recipient_authority(command)
    return {
        "audience": "email-command-executor",
        "granted_scopes": ["email-send-execute"],
        "site_id": command["site_id"],
        "processing_purpose": command["processing_purpose"],
        "team_ref": route["team_ref"],
        "authenticated_actor_user_ref": command["actor_user_ref"],
        "delegated_approver_user_ref": command["delegated_approver_user_ref"],
        "review_case_ref": command["review_case_ref"],
        "review_case_revision": command["review_case_revision"],
        "review_policy_version": command["review_policy_version"],
        "party_ref": route["party_ref"],
        "party_revision": route["party_revision"],
        "team_revision": route["team_revision"],
        "owner_user_ref": command["owner_user_ref"],
        "owner_eligibility_revision": route["owner_eligibility_revision"],
        "participants": command["participants"],
        "final_mime_evidence_ref": command["final_mime_evidence_ref"],
        "final_mime_digest": command["final_mime_digest"],
        "evidence_refs": command["evidence_refs"],
        "request_id": command["request_id"],
        "idempotency_key": command["idempotency_key"],
        "stable_client_request_id": command["stable_client_request_id"],
        "replay_payload_sha256": command["payload_sha256"],
    }


def _recipient_authority(command: Mapping[str, Any]) -> dict[str, Any]:
    route: dict[str, Any] | None = None
    for participant in command["participants"]:
        if participant["address_role"] == "sender":
            continue
        rows = _route_rows(participant["identity_mapping_ref"], for_update=True)
        current = _current_recipient_route(rows, participant, command)
        if route is None:
            route = current
        elif current != route:
            raise _APIError("email_send_authority_unavailable", 409)
    if route is None:
        raise _APIError("email_send_authority_unavailable", 409)
    return route


def _current_recipient_route(
    rows: list[dict[str, Any]], participant: Mapping[str, Any], command: Mapping[str, Any]
) -> dict[str, Any]:
    if len(rows) != 1:
        raise _APIError("email_send_authority_unavailable", 409)
    row = rows[0]
    try:
        projection = build_external_identity_projection(row)
        party_revision = _row_positive_integer(row.get("party_revision"))
        team_revision = _row_positive_integer(row.get("team_revision"))
        owner_name = _row_ref(row.get("owner_user_ref"))
        party_ref = _row_ref(row.get("party_ref"))
        owner_revision = owner_eligibility_revision(
            {
                "name": party_ref,
                "revision": party_revision,
                "team": projection["team_ref"],
                "owner_user": owner_name,
            },
            {
                "owner_enabled": row.get("owner_enabled"),
                "owner_user_type": row.get("owner_user_type"),
                "membership_ref": row.get("membership_ref"),
                "membership_parent": row.get("membership_parent"),
                "membership_user": row.get("membership_user"),
                "membership_enabled": row.get("membership_enabled"),
                "membership_modified": row.get("membership_modified"),
                "team_revision": team_revision,
            },
        )
        owner_ref = protected_user_ref(command["site_id"], owner_name)
    except ExternalIdentityProjectionError, EmailSendReviewPolicyError, ValueError, TypeError:
        raise _APIError("email_send_authority_unavailable", 409) from None
    if (
        projection["status"] != "confirmed"
        or projection["target_type"] != "Party"
        or projection["mapping_ref"] != participant["identity_mapping_ref"]
        or projection["mapping_revision"] != participant["identity_mapping_revision"]
        or projection["team_ref"] != command["team_ref"]
        or row.get("target_eligible") != 1
        or party_ref != command["party_ref"]
        or party_revision != command["party_revision"]
        or team_revision != command["team_revision"]
        or row.get("party_status") != "Active"
        or row.get("party_review_status") != "Approved"
        or row.get("team_status") != "Active"
        or row.get("team_review_status") != "Approved"
        or row.get("owner_enabled") != 1
        or row.get("owner_user_type") != "System User"
        or row.get("membership_enabled") != 1
        or row.get("membership_parent") != projection["team_ref"]
        or row.get("membership_user") != owner_name
        or owner_ref != command["owner_user_ref"]
        or owner_revision != command["owner_eligibility_revision"]
    ):
        raise _APIError("email_send_authority_unavailable", 409)
    return {
        "party_ref": party_ref,
        "party_revision": party_revision,
        "team_ref": projection["team_ref"],
        "team_revision": team_revision,
        "owner_eligibility_revision": owner_revision,
    }


def _command_approval_snapshot(command: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": command["site_id"],
        "processing_purpose": command["processing_purpose"],
        "team_ref": command["team_ref"],
        "assignee_user_ref": command["delegated_approver_user_ref"],
        "approval_expires_at": command["approval_expires_at"],
        "mailbox_ref": command["mailbox_ref"],
        "mailbox_config_revision": command["mailbox_config_revision"],
        "inbox_item_ref": command["inbox_item_ref"],
        "inbox_item_revision": command["inbox_item_revision"],
        "conversation_ref": command["conversation_ref"],
        "conversation_revision": command["conversation_revision"],
        "reply_draft_ref": command["reply_draft_ref"],
        "reply_draft_revision": command["reply_draft_revision"],
        "reply_draft_digest": command["reply_draft_digest"],
        "participants": command["participants"],
        "party_ref": command["party_ref"],
        "party_revision": command["party_revision"],
        "team_revision": command["team_revision"],
        "owner_user_ref": command["owner_user_ref"],
        "owner_eligibility_revision": command["owner_eligibility_revision"],
        "final_mime_evidence_ref": command["final_mime_evidence_ref"],
        "final_mime_digest": command["final_mime_digest"],
        "evidence_refs": command["evidence_refs"],
        "stable_client_request_id": command["stable_client_request_id"],
    }


def _approval_snapshot(approval: Any) -> dict[str, Any]:
    def value(field: str) -> Any:
        result = getattr(approval, field)
        if field in {"participants", "evidence_refs"} and isinstance(result, str):
            return frappe.parse_json(result)
        return result

    return {
        "schema_version": "1.0",
        "site_id": value("site_id"),
        "processing_purpose": value("processing_purpose"),
        "team_ref": value("team"),
        "assignee_user_ref": value("assignee_user_ref"),
        "approval_expires_at": value("approval_expires_at"),
        "mailbox_ref": value("mailbox_ref"),
        "mailbox_config_revision": value("mailbox_config_revision"),
        "inbox_item_ref": value("inbox_item_ref"),
        "inbox_item_revision": value("inbox_item_revision"),
        "conversation_ref": value("conversation_ref"),
        "conversation_revision": value("conversation_revision"),
        "reply_draft_ref": value("reply_draft_ref"),
        "reply_draft_revision": value("reply_draft_revision"),
        "reply_draft_digest": value("reply_draft_digest"),
        "participants": value("participants"),
        "party_ref": value("party_ref"),
        "party_revision": value("party_revision"),
        "team_revision": value("team_revision"),
        "owner_user_ref": value("owner_user_ref"),
        "owner_eligibility_revision": value("owner_eligibility_revision"),
        "final_mime_evidence_ref": value("final_mime_evidence_ref"),
        "final_mime_digest": value("final_mime_digest"),
        "evidence_refs": value("evidence_refs"),
        "stable_client_request_id": value("stable_client_request_id"),
    }


def _email_approval_subject_snapshot(approval: Any, *, revision: int) -> dict[str, Any]:
    return {
        "doctype": "GBOS Email Send Approval",
        "name": approval.name,
        "revision": revision,
        "site_id": approval.site_id,
        "processing_purpose": approval.processing_purpose,
        "team": approval.team,
        "assignee_user_ref": approval.assignee_user_ref,
        "approval_expires_at": approval.approval_expires_at,
        "mailbox_ref": approval.mailbox_ref,
        "mailbox_config_revision": approval.mailbox_config_revision,
        "inbox_item_ref": approval.inbox_item_ref,
        "inbox_item_revision": approval.inbox_item_revision,
        "conversation_ref": approval.conversation_ref,
        "conversation_revision": approval.conversation_revision,
        "reply_draft_ref": approval.reply_draft_ref,
        "reply_draft_revision": approval.reply_draft_revision,
        "reply_draft_digest": approval.reply_draft_digest,
        "participants": frappe.parse_json(approval.participants),
        "party_ref": approval.party_ref,
        "party_revision": approval.party_revision,
        "team_revision": approval.team_revision,
        "owner_user_ref": approval.owner_user_ref,
        "owner_eligibility_revision": approval.owner_eligibility_revision,
        "final_mime_evidence_ref": approval.final_mime_evidence_ref,
        "final_mime_digest": approval.final_mime_digest,
        "evidence_refs": frappe.parse_json(approval.evidence_refs),
        "stable_client_request_id": approval.stable_client_request_id,
        "payload_sha256": approval.payload_sha256,
    }


def _pinned_subject_snapshot(case: Any) -> dict[str, Any]:
    try:
        value = frappe.parse_json(case.subject_snapshot)
    except Exception:
        raise _APIError("email_send_authority_unavailable", 409) from None
    if not isinstance(value, dict):
        raise _APIError("email_send_authority_unavailable", 409)
    return value


def _review_case_payload(case: Any) -> dict[str, Any]:
    try:
        subject_snapshot = frappe.parse_json(case.subject_snapshot)
        evidence_refs = frappe.parse_json(case.evidence_refs)
    except Exception:
        raise _APIError("email_send_authority_unavailable", 409) from None
    payload = {
        "title": case.title,
        "team": case.team,
        "assigned_reviewer": case.assigned_reviewer,
        "subject_doctype": case.subject_doctype,
        "subject_name": case.subject_name,
        "subject_revision": int(case.subject_revision or 0),
        "subject_payload_sha256": case.subject_payload_sha256,
        "subject_snapshot": subject_snapshot,
        "evidence_refs": evidence_refs,
        "policy_version": case.policy_version,
    }
    if case.approval_expires_at not in (None, ""):
        payload["approval_expires_at"] = case.approval_expires_at
    return payload


def _route_decision(
    rows: list[dict[str, Any]],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    resolved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    unavailable = {
        "route_status": "unassigned",
        "safe_reason_code": "owner_unavailable",
        "resolved_at": resolved_at,
    }
    if len(rows) != 1:
        return unavailable
    row = rows[0]
    try:
        resolved_at = _timestamp(row.get("resolved_at"))
        projection = build_external_identity_projection(row)
        party_revision = _row_positive_integer(row.get("party_revision"))
        team_revision = _row_positive_integer(row.get("team_revision"))
        owner = _row_ref(row.get("owner_user_ref"))
        party_ref = _row_ref(row.get("party_ref"))
        state = {
            "owner_enabled": row.get("owner_enabled"),
            "owner_user_type": row.get("owner_user_type"),
            "membership_ref": row.get("membership_ref"),
            "membership_parent": row.get("membership_parent"),
            "membership_user": row.get("membership_user"),
            "membership_enabled": row.get("membership_enabled"),
            "membership_modified": row.get("membership_modified"),
            "team_revision": team_revision,
        }
        owner_revision = owner_eligibility_revision(
            {
                "name": party_ref,
                "revision": party_revision,
                "team": projection["team_ref"],
                "owner_user": owner,
            },
            state,
        )
    except ExternalIdentityProjectionError, ValueError, TypeError:
        return unavailable
    if (
        projection["status"] != "confirmed"
        or projection["target_type"] != "Party"
        or row.get("target_eligible") != 1
        or row.get("party_status") != "Active"
        or row.get("party_review_status") != "Approved"
        or row.get("owner_enabled") != 1
        or row.get("owner_user_type") != "System User"
        or row.get("membership_enabled") != 1
        or row.get("membership_parent") != projection["team_ref"]
        or row.get("membership_user") != owner
        or projection["mapping_revision"] != request["expected_mapping_revision"]
        or projection["team_ref"] != request["expected_team_ref"]
        or party_revision != request["expected_party_revision"]
        or team_revision != request["expected_team_revision"]
        or owner_revision != request["expected_owner_eligibility_revision"]
    ):
        return unavailable
    return {
        "route_status": "assigned",
        "party_ref": party_ref,
        "party_revision": party_revision,
        "team_ref": projection["team_ref"],
        "team_revision": team_revision,
        "owner_user_ref": owner,
        "owner_eligibility_revision": owner_revision,
        "resolved_at": resolved_at,
    }


def _validated_request(payload: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    if set(payload) != fields:
        raise _APIError("invalid_authority_request", 422)
    _authenticate(payload)
    normalized: dict[str, Any] = {
        "site_id": _site(payload.get("site_id")),
        "processing_purpose": _text(payload.get("processing_purpose"), 80),
        "request_id": _text(payload.get("request_id"), 256),
        "auth_ref": _text(payload.get("auth_ref"), 140),
    }
    if fields in {_PROJECT_FIELDS, _ROUTE_FIELDS}:
        normalized.update(
            mapping_ref=_ref(payload.get("mapping_ref")),
            expected_mapping_revision=_positive_integer(payload.get("expected_mapping_revision")),
            expected_team_ref=_ref(payload.get("expected_team_ref")),
        )
    if fields == _ROUTE_FIELDS:
        normalized.update(
            expected_party_revision=_positive_integer(payload.get("expected_party_revision")),
            expected_team_revision=_positive_integer(payload.get("expected_team_revision")),
            expected_owner_eligibility_revision=_digest(
                payload.get("expected_owner_eligibility_revision")
            ),
        )
    if fields == _COMMAND_FIELDS:
        normalized.update(
            publication_ref=_prefixed_ref(payload.get("publication_ref"), "PUB"),
            attempt=_positive_integer(payload.get("attempt")),
            generation=_positive_integer(payload.get("generation")),
            fence_token=_prefixed_ref(payload.get("fence_token"), "FNC"),
            command_ref=_prefixed_ref(payload.get("command_ref"), "CMD"),
            payload_digest=_digest(payload.get("payload_digest")),
        )
    return normalized


def _authenticate(payload: Mapping[str, Any]) -> None:
    request = getattr(frappe.local, "request", None)
    actor = str(getattr(frappe.session, "user", ""))
    if request is None or str(getattr(request, "method", "")).upper() != "POST":
        raise _APIError("method_not_allowed", 405)
    headers = request.headers
    authorization = str(headers.get("Authorization") or "")
    roles = set(frappe.get_roles(actor))
    if (
        not authorization.startswith("token ")
        or ":" not in authorization[6:]
        or not all(authorization[6:].split(":", 1))
        or actor in {"", "Guest"}
        or _ROLE not in roles
        or bool(roles - _ALLOWED_RUNTIME_ROLES)
    ):
        raise _APIError("authentication_required", 401)
    identities = frappe.conf.get("gbos_email_gateway_authority_identities")
    auth_ref = _text(payload.get("auth_ref"), 140)
    identity = identities.get(auth_ref) if isinstance(identities, Mapping) else None
    site_id = _site(payload.get("site_id"))
    purpose = _text(payload.get("processing_purpose"), 80)
    request_id = _text(payload.get("request_id"), 256)
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"user", "site_id", "processing_purposes"}
        or identity.get("user") != actor
        or identity.get("site_id") != site_id
        or site_id != str(getattr(frappe.local, "site", ""))
        or purpose != _PURPOSE
        or identity.get("processing_purposes") != [_PURPOSE]
        or headers.get("X-Site-ID") != site_id
        or headers.get("X-Processing-Purpose") != purpose
        or headers.get("X-Request-ID") != request_id
        or headers.get("X-GBOS-Frappe-Auth-Ref") != auth_ref
    ):
        raise _APIError("identity_scope_mismatch", 403)


def _mapping_rows(mapping_ref: str) -> list[dict[str, Any]]:
    require_email_gateway_authority_scope()
    rows = frappe.db.sql(
        """
        select mapping.`name` as `mapping_ref`, mapping.`revision` as `mapping_revision`,
               mapping.`team` as `team_ref`, mapping.`identity_type` as `target_type`,
               mapping.`user` as `user_ref`, mapping.`party_profile` as `party_ref`,
               mapping.`review_status`, mapping.`business_status`,
               mapping.`modified` as `resolved_at`,
               case when mapping.`identity_type` = 'User' then exists (
                    select 1 from `tabUser` u where u.`name` = mapping.`user` and u.`enabled` = 1)
                    and exists (select 1 from `tabGBOS Team Member` m
                    where m.`parent` = mapping.`team`
                    and m.`user` = mapping.`user` and m.`enabled` = 1)
                    when mapping.`identity_type` = 'Party' then exists (
                    select 1 from `tabGBOS Party Profile` p where p.`name` = mapping.`party_profile`
                    and p.`team` = mapping.`team`) else 0 end as `target_eligible`
        from `tabGBOS External Identity` mapping where mapping.`name` = %(mapping_ref)s limit 3
        """,
        {"mapping_ref": mapping_ref},
        as_dict=True,
    )
    return _rows(rows)


def _route_rows(mapping_ref: str, *, for_update: bool = False) -> list[dict[str, Any]]:
    require_email_gateway_authority_scope()
    query = """
        select mapping.`name` as `mapping_ref`, mapping.`revision` as `mapping_revision`,
               mapping.`team` as `team_ref`, mapping.`identity_type` as `target_type`,
               mapping.`user` as `user_ref`, mapping.`party_profile` as `party_ref`,
               mapping.`review_status`, mapping.`business_status`,
               mapping.`modified` as `resolved_at`,
               party.`revision` as `party_revision`, party.`business_status` as `party_status`,
               party.`review_status` as `party_review_status`, team.`revision` as `team_revision`,
               team.`business_status` as `team_status`,
               team.`review_status` as `team_review_status`,
               party.`owner_user` as `owner_user_ref`, owner_user.`enabled` as `owner_enabled`,
               owner_user.`user_type` as `owner_user_type`, member.`name` as `membership_ref`,
               member.`parent` as `membership_parent`, member.`user` as `membership_user`,
               member.`enabled` as `membership_enabled`, member.`modified` as `membership_modified`,
               case when party.`name` is not null
                    and party.`team` = mapping.`team` then 1 else 0 end
                    as `target_eligible`
        from `tabGBOS External Identity` mapping
        left join `tabGBOS Party Profile` party on party.`name` = mapping.`party_profile`
        left join `tabGBOS Team` team on team.`name` = party.`team`
        left join `tabUser` owner_user on owner_user.`name` = party.`owner_user`
        left join `tabGBOS Team Member` member on member.`parent` = party.`team`
             and member.`user` = party.`owner_user`
        where mapping.`name` = %(mapping_ref)s limit 3
        """
    if for_update:
        query += " for update"
    rows = frappe.db.sql(
        query,
        {"mapping_ref": mapping_ref},
        as_dict=True,
    )
    return _rows(rows)


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise _APIError("authority_conflict", 409)
    return [dict(row) for row in value]


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError("invalid_authority_request", 422) from None
    if not isinstance(value, Mapping):
        raise _APIError("invalid_authority_request", 422)
    return dict(value)


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not _TEXT.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _site(value: object) -> str:
    if not isinstance(value, str) or not _SITE.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _prefixed_ref(value: object, prefix: str) -> str:
    result = _ref(value)
    if not result.startswith(prefix + "-"):
        raise _APIError("invalid_authority_request", 422)
    return result


def _row_ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ValueError
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _APIError("invalid_authority_request", 422)
    return value


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2147483647:
        raise _APIError("invalid_authority_request", 422)
    return value


def _row_positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2147483647:
        raise ValueError
    return value


def _timestamp(value: object) -> str:
    if isinstance(value, datetime):
        result = value.isoformat()
        return f"{result}Z" if value.tzinfo is None else result
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return value


def _document_time(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError from None
    else:
        raise ValueError
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _review_case_ref(value: object) -> str:
    name = _row_ref(value)
    if not name.startswith("REV-"):
        raise ValueError
    return "RVC-" + name[4:]


__all__ = [
    "InboxCommandAuthorityConflict",
    "derive_inbox_command_authority",
    "project",
    "resolve_email_send_command",
    "resolve_route",
]
