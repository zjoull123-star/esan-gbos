from __future__ import annotations

import builtins
import hashlib
from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v5.gateway import call_gateway, call_observer, scope_payload, v5_success
from esan_gbos.domain.email_review_policy import EMAIL_SEND_PARTICIPANT_ROLES_DIGEST
from esan_gbos.domain.v5_email_dto import (
    V5EmailDTOValidationError,
    map_inbox_detail,
    map_inbox_item,
)

EMAIL_INBOX_ROLES = frozenset({"CEO", "Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
EMAIL_COMMAND_ROLES = frozenset({"Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
EMAIL_REVEAL_ROLES = EMAIL_COMMAND_ROLES
_ALLOWED_STATES = frozenset(
    {
        "identity_pending",
        "unassigned",
        "assigned",
        "draft",
        "waiting_internal",
        "waiting_customer",
        "converted",
        "closed",
        "quarantined",
        "send_queued",
        "send_uncertain",
    }
)
_ALLOWED_SORTS = frozenset({"received_at_desc", "sla_due_at_asc"})
_INBOX_SUMMARY_FIELDS = frozenset(
    {
        "inbox_item_ref",
        "mailbox_label",
        "mailbox_role",
        "received_at",
        "state",
        "safe_summary",
        "team_ref",
        "revision",
    }
)


def _integer(value: int | str, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise BFFError("invalid_query", f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_query", f"{field} must be an integer") from error
    if result < 0 or (maximum is not None and result > maximum):
        raise BFFError("invalid_query", f"{field} is outside the allowed range")
    return result


def _boolean(value: bool | int | str, field: str) -> bool:
    if value in (True, 1, "1", "true"):
        return True
    if value in (False, 0, "0", "false"):
        return False
    raise BFFError("invalid_query", f"{field} must be a boolean")


def _optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def derive_inbox_command_authority(**values: Any) -> dict[str, Any]:
    from esan_gbos.api.internal.email_gateway_authority import (
        InboxCommandAuthorityConflict,
    )
    from esan_gbos.api.internal.email_gateway_authority import (
        derive_inbox_command_authority as derive,
    )

    try:
        return derive(**values)
    except InboxCommandAuthorityConflict as error:
        raise BFFError(
            "authority_conflict",
            "Inbox command authority changed",
            status=409,
        ) from error


def _label(doctype: str, reference: object, field: str) -> str | None:
    if not isinstance(reference, str) or not reference:
        return None
    try:
        value = frappe.get_value(doctype, reference, field)
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _public_inbox(value: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    expected = _INBOX_SUMMARY_FIELDS | (
        {"assignee_user_ref", "identity_state"} if detail else set()
    )
    if set(value) != expected:
        raise V5EmailDTOValidationError("unexpected fields in internal inbox projection")
    result = {
        **{key: item for key, item in value.items() if key != "team_ref"},
        "team_label": _label("GBOS Team", value["team_ref"], "team_name"),
    }
    if detail:
        result["assignee_label"] = _label("User", result.pop("assignee_user_ref"), "full_name")
    return result


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def list(
    state: str | None = None,
    mailbox_ref: str | None = None,
    sort: str = "received_at_desc",
    cursor: str | None = None,
    page_size: int | str = 25,
) -> dict[str, Any]:
    require_roles(EMAIL_INBOX_ROLES)
    payload: dict[str, Any] = {
        **scope_payload(business_read=True),
        "page_size": _integer(page_size, "page_size", maximum=50),
    }
    if value := _optional(state):
        if value not in _ALLOWED_STATES:
            raise BFFError("invalid_query", "state is not allowed")
        payload["state"] = value
    if value := _optional(mailbox_ref):
        payload["mailbox_ref"] = value
    if sort not in _ALLOWED_SORTS:
        raise BFFError("invalid_query", "sort is not allowed")
    if sort != "received_at_desc":
        payload["sort"] = sort
    if value := _optional(cursor):
        payload["cursor"] = value
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-inbox/list",
        purpose="email_inbox_read",
        payload=payload,
    )
    rows = data.get("inbox_items")
    next_cursor = data.get("next_cursor")
    if not isinstance(rows, builtins.list) or not (
        next_cursor is None or isinstance(next_cursor, str)
    ):
        raise BFFError("internal_error", "Email Inbox response is invalid", status=503)
    try:
        items = [map_inbox_item(_public_inbox(row, detail=False)) for row in rows]
    except (TypeError, V5EmailDTOValidationError) as error:
        raise BFFError("internal_error", "Email Inbox response is invalid", status=503) from error
    return v5_success({"inbox_items": items, "next_cursor": next_cursor}, next_cursor=next_cursor)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def get(inbox_item_ref: str) -> dict[str, Any]:
    require_roles(EMAIL_INBOX_ROLES)
    reference = _optional(inbox_item_ref)
    if reference is None:
        raise BFFError("invalid_query", "inbox_item_ref is required")
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-inbox/get",
        purpose="email_inbox_read",
        payload={**scope_payload(business_read=True), "inbox_item_ref": reference},
    )
    value = data.get("inbox_item")
    if not isinstance(value, dict):
        raise BFFError("not_found", "Inbox item was not found", status=404)
    try:
        detail = map_inbox_detail(_public_inbox(value, detail=True))
    except V5EmailDTOValidationError as error:
        raise BFFError("internal_error", str(error), status=503) from error
    return v5_success({"inbox_item": detail})


def _command(name: str, command: dict[str, Any], result_key: str) -> dict[str, Any]:
    require_roles(EMAIL_COMMAND_ROLES)
    if name in {"claim", "reassign", "link_business"}:
        command = {
            **command,
            "authority_receipt": derive_inbox_command_authority(
                command=name,
                inbox_item_ref=str(command["inbox_item_ref"]),
                expected_inbox_revision=int(command["expected_revision"]),
                target_user_ref=command.get("assignee_user_ref") if name == "reassign" else None,
                business_ref=command.get("business_ref") if name == "link_business" else None,
            ),
        }
    payload = {**scope_payload(business_read=True), **command}
    key = str(command["idempotency_key"])

    def execute() -> dict[str, Any]:
        data = call_gateway(
            method="POST",
            path=f"/internal/v1/bff/email-inbox/{name.replace('_', '-')}",
            purpose="email_inbox_command",
            payload=payload,
            idempotency_key=key,
        )
        value = data.get(result_key)
        if not isinstance(value, dict):
            raise BFFError("internal_error", "Email Inbox command response is invalid", status=503)
        return _public_command_result(result_key, value)

    result, replayed, original_request_id = run_idempotent(
        f"email_inbox.{name}", key, payload, execute, api_version="v5"
    )
    return v5_success(
        {result_key: result}, replayed=replayed, original_request_id=original_request_id
    )


def _public_command_result(result_key: str, value: dict[str, Any]) -> dict[str, Any]:
    if result_key == "inbox_item":
        expected = {
            "inbox_item_ref",
            "state",
            "team_ref",
            "assignee_user_ref",
            "conversation_ref",
            "business_links",
            "revision",
        }
        # A minimal fake/compatibility result is accepted only when it has no protected refs.
        if set(value) == {"inbox_item_ref", "state", "revision"}:
            return dict(value)
        if set(value) != expected:
            raise BFFError("internal_error", "Email Inbox command response is invalid", status=503)
        return {
            "inbox_item_ref": value["inbox_item_ref"],
            "state": value["state"],
            "team_label": _label("GBOS Team", value["team_ref"], "team_name"),
            "assignee_label": _label("User", value["assignee_user_ref"], "full_name"),
            "conversation_ref": value["conversation_ref"],
            "business_links": value["business_links"],
            "revision": value["revision"],
        }
    if result_key == "conversation":
        expected = {
            "conversation_ref",
            "team_ref",
            "lifecycle_state",
            "inbox_item_refs",
            "revision",
        }
        if set(value) != expected:
            raise BFFError("internal_error", "Email conversation response is invalid", status=503)
        return {
            "conversation_ref": value["conversation_ref"],
            "team_label": _label("GBOS Team", value["team_ref"], "team_name"),
            "lifecycle_state": value["lifecycle_state"],
            "inbox_item_refs": value["inbox_item_refs"],
            "revision": value["revision"],
        }
    raise BFFError("internal_error", "Email command result type is invalid", status=503)


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def claim(
    inbox_item_ref: str, expected_revision: int | str, idempotency_key: str
) -> dict[str, Any]:
    return _command(
        "claim",
        {
            "inbox_item_ref": inbox_item_ref,
            "expected_revision": _integer(expected_revision, "expected_revision"),
            "idempotency_key": idempotency_key,
        },
        "inbox_item",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def reassign(
    inbox_item_ref: str,
    expected_revision: int | str,
    idempotency_key: str,
    assignee_user_ref: str | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "inbox_item_ref": inbox_item_ref,
        "expected_revision": _integer(expected_revision, "expected_revision"),
        "idempotency_key": idempotency_key,
    }
    if value := _optional(assignee_user_ref):
        command["assignee_user_ref"] = value
    return _command("reassign", command, "inbox_item")


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def transition(
    inbox_item_ref: str,
    target_state: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    if target_state not in _ALLOWED_STATES:
        raise BFFError("invalid_query", "target_state is not allowed")
    return _command(
        "transition",
        {
            "inbox_item_ref": inbox_item_ref,
            "target_state": target_state,
            "expected_revision": _integer(expected_revision, "expected_revision"),
            "idempotency_key": idempotency_key,
        },
        "inbox_item",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def merge(
    suggestion_ref: str,
    left_inbox_item_ref: str,
    expected_suggestion_revision: int | str,
    expected_left_revision: int | str,
    expected_right_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _command(
        "merge",
        {
            "suggestion_ref": suggestion_ref,
            "left_inbox_item_ref": left_inbox_item_ref,
            "expected_suggestion_revision": _integer(
                expected_suggestion_revision, "expected_suggestion_revision"
            ),
            "expected_left_revision": _integer(expected_left_revision, "expected_left_revision"),
            "expected_right_revision": _integer(expected_right_revision, "expected_right_revision"),
            "idempotency_key": idempotency_key,
        },
        "conversation",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def split(
    conversation_ref: str,
    moved_inbox_item_refs: Any,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    if not isinstance(moved_inbox_item_refs, (builtins.list, tuple)):
        raise BFFError("invalid_query", "moved_inbox_item_refs must be a list")
    return _command(
        "split",
        {
            "conversation_ref": conversation_ref,
            "moved_inbox_item_refs": builtins.list(moved_inbox_item_refs),
            "expected_revision": _integer(expected_revision, "expected_revision"),
            "idempotency_key": idempotency_key,
        },
        "conversation",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def link_business(
    inbox_item_ref: str,
    business_ref: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _command(
        "link_business",
        {
            "inbox_item_ref": inbox_item_ref,
            "business_ref": business_ref,
            "expected_revision": _integer(expected_revision, "expected_revision"),
            "idempotency_key": idempotency_key,
        },
        "inbox_item",
    )


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def save_draft(
    inbox_item_ref: str,
    draft_ref: str,
    expected_revision: int | str,
    content: str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_COMMAND_ROLES)
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 131_072:
        raise BFFError("invalid_query", "Draft content is invalid")
    revision = _integer(expected_revision, "expected_revision")
    digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    common = {
        **scope_payload(business_read=True),
        "inbox_item_ref": inbox_item_ref,
        "draft_ref": draft_ref,
        "expected_revision": revision,
        "content_digest": digest,
        "participant_roles_digest": EMAIL_SEND_PARTICIPANT_ROLES_DIGEST,
        "idempotency_key": idempotency_key,
    }
    authorized = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-inbox/save-draft",
        purpose="email_inbox_command",
        payload={**common, "phase": "authorize"},
        idempotency_key=idempotency_key,
    )
    receipt = authorized.get("draft_authorization")
    if not isinstance(receipt, dict):
        raise BFFError("internal_error", "Draft authorization is invalid", status=503)
    material = call_observer(
        path="/internal/v1/bff/email-draft-material/save",
        purpose="email_draft_material",
        payload={
            "authorization": receipt,
            "content": content,
            "content_digest": digest,
            "idempotency_key": idempotency_key,
        },
        idempotency_key=idempotency_key,
    )
    if set(material) != {"evidence_ref", "digest", "revision"}:
        raise BFFError("internal_error", "Draft material response is invalid", status=503)
    committed = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-inbox/save-draft",
        purpose="email_inbox_command",
        payload={
            **common,
            "phase": "commit",
            "draft_authorization": receipt,
            "evidence_ref": material["evidence_ref"],
            "evidence_digest": material["digest"],
            "evidence_revision": material["revision"],
        },
        idempotency_key=idempotency_key,
    )
    draft = committed.get("draft")
    if not isinstance(draft, dict) or set(draft) != {"draft_ref", "revision", "state"}:
        raise BFFError("internal_error", "Draft projection response is invalid", status=503)
    return v5_success({"draft": draft})


@frappe.whitelist(allow_guest=True, methods=["POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def reveal(inbox_item_ref: str, evidence_ref: str) -> dict[str, Any]:
    require_roles(EMAIL_REVEAL_ROLES)
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-inbox/reveal",
        purpose="email_evidence_reveal",
        payload={
            **scope_payload(business_read=True),
            "inbox_item_ref": inbox_item_ref,
            "evidence_ref": evidence_ref,
        },
    )
    value = data.get("revealed")
    if not isinstance(value, dict) or set(value) != {"content", "media_type"}:
        raise BFFError("internal_error", "Evidence reveal response is invalid", status=503)
    return v5_success(value)
