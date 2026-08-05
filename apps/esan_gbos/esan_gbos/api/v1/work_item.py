from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import (
    BFFError,
    bff_endpoint,
    parse_json_object,
    request_id,
    require_doc_permission,
    require_roles,
    success,
)
from esan_gbos.domain.access_policy import WORK_READ_ROLES
from esan_gbos.domain.dto import DTOValidationError, validate_payload
from esan_gbos.domain.query import (
    CursorError,
    decode_cursor,
    encode_cursor,
    validate_work_filters,
)
from esan_gbos.domain.revision import RevisionConflict, next_revision
from esan_gbos.domain.state_machine import InvalidTransition, validate_transition

READ_ROLES = WORK_READ_ROLES
WRITE_ROLES = {
    "GBOS Admin",
    "Sales Manager",
    "Sales User",
    "Purchase Manager",
    "Buyer",
    "Product/R&D",
}
WORK_FIELDS = [
    "name",
    "title",
    "team",
    "assigned_to",
    "priority",
    "due_date",
    "origin",
    "business_status",
    "review_status",
    "revision",
    "reference_doctype",
    "reference_name",
    "modified",
]


@frappe.whitelist(methods=["GET"])
@bff_endpoint("GET")
def list(
    filters: str | dict[str, Any] | None = None,
    cursor: str | None = None,
    page_size: int = 25,
) -> dict[str, Any]:
    """List work items through a fixed filter/field contract."""
    require_roles(READ_ROLES)
    try:
        safe_filters = validate_work_filters(parse_json_object(filters))
    except (ValueError, CursorError) as error:
        raise BFFError("invalid_query", str(error)) from error
    size = max(1, min(int(page_size), 50))
    cursor_value: tuple[str, str] | None = None
    if cursor:
        try:
            cursor_value = decode_cursor(cursor)
        except CursorError as error:
            raise BFFError("invalid_cursor", str(error)) from error

    if cursor_value:
        same_timestamp_filters = {
            **safe_filters,
            "modified": cursor_value[0],
            "name": ["<", cursor_value[1]],
        }
        rows = frappe.get_list(
            "GBOS Work Item",
            filters=same_timestamp_filters,
            fields=WORK_FIELDS,
            order_by="name desc",
            page_length=size + 1,
        )
        remaining = size + 1 - len(rows)
        if remaining > 0:
            older_filters = {
                **safe_filters,
                "modified": ["<", cursor_value[0]],
            }
            rows.extend(
                frappe.get_list(
                    "GBOS Work Item",
                    filters=older_filters,
                    fields=WORK_FIELDS,
                    order_by="modified desc, name desc",
                    page_length=remaining,
                )
            )
    else:
        rows = frappe.get_list(
            "GBOS Work Item",
            filters=safe_filters,
            fields=WORK_FIELDS,
            order_by="modified desc, name desc",
            page_length=size + 1,
        )

    has_more = len(rows) > size
    rows = rows[:size]
    next_cursor = (
        encode_cursor(str(rows[-1]["modified"]), str(rows[-1]["name"]))
        if has_more and rows
        else None
    )
    return success(rows, next_cursor=next_cursor, page_size=size)


@frappe.whitelist(methods=["POST"])
@bff_endpoint("POST")
def transition(
    name: str,
    to_status: str,
    expected_revision: int,
    idempotency_key: str,
    reason: str | None = None,
) -> dict[str, Any]:
    require_roles(WRITE_ROLES)
    raw = {
        "name": name,
        "to_status": to_status,
        "expected_revision": int(expected_revision),
        "idempotency_key": idempotency_key,
    }
    if reason is not None:
        raw["reason"] = reason
    try:
        payload = validate_payload("work_item.transition", raw)
    except DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    def execute() -> dict[str, Any]:
        doc = frappe.get_doc("GBOS Work Item", name)
        require_doc_permission(doc, "write")
        try:
            next_revision(expected=payload["expected_revision"], current=int(doc.revision))
            validate_transition("work", doc.business_status, to_status)
        except RevisionConflict as error:
            raise BFFError("revision_conflict", str(error), status=409) from error
        except InvalidTransition as error:
            raise BFFError("invalid_transition", str(error), status=409) from error
        doc.business_status = to_status
        doc.last_request_id = request_id()
        if to_status == "Blocked":
            doc.blocked_reason = reason
        doc.save()
        return {
            "doctype": doc.doctype,
            "name": doc.name,
            "business_status": doc.business_status,
            "revision": doc.revision,
        }

    result, replayed, original_request_id = run_idempotent(
        "work_item.transition",
        idempotency_key,
        payload,
        execute,
    )
    return success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )
