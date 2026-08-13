from __future__ import annotations

import builtins
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v5.gateway import call_gateway, scope_payload, v5_success
from esan_gbos.domain.v5_email_dto import (
    V5EmailDTOValidationError,
    map_inbox_detail,
    map_inbox_item,
)

EMAIL_INBOX_ROLES = frozenset({"CEO", "Sales Manager", "Sales User", "Reviewer", "GBOS Admin"})
_ALLOWED_STATES = frozenset({"identity_pending", "unassigned"})
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


def _optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


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
    if value := _optional(cursor):
        payload["cursor"] = value
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/inbox/list",
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
        path="/internal/v1/bff/inbox/get",
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
