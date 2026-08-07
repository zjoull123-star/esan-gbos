from __future__ import annotations

import builtins
from typing import Any
from urllib.parse import quote

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, request_id, require_roles
from esan_gbos.api.v4.gateway import call_local, v4_success
from esan_gbos.domain.query import CursorError, decode_cursor, encode_cursor
from esan_gbos.domain.state_machine import InvalidTransition, validate_ai_draft_transition
from esan_gbos.domain.v4_dto import (
    V4DTOValidationError,
    map_ai_draft,
    validate_ai_draft_submit,
)

AI_DRAFT_ROLES = frozenset({"Reviewer", "GBOS Admin"})
_DRAFT_DOCTYPES = (
    "GBOS Work Item",
    "GBOS Review Case",
    "GBOS Informal Observation",
)
_KINDS = {
    "GBOS Work Item": "Work Item",
    "GBOS Review Case": "Review Case",
    "GBOS Informal Observation": "CEO Informal Observation",
}
_SUBJECT_FIELDS = {
    "GBOS Work Item": "title",
    "GBOS Review Case": "title",
    "GBOS Informal Observation": "subject",
}


def _integer(value: int | str, field: str, *, query: bool = False) -> int:
    code = "invalid_query" if query else "invalid_dto"
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError(code, f"{field} must be an integer") from error
    if isinstance(value, bool):
        raise BFFError(code, f"{field} must be an integer")
    return parsed


def _reviewer_names() -> dict[str, set[str]] | None:
    if "GBOS Admin" in frappe.get_roles():
        return None
    actor = frappe.session.user
    cases = frappe.get_all(
        "GBOS Review Case",
        filters={"assigned_reviewer": actor},
        fields=["name", "subject_doctype", "subject_name"],
    )
    names: dict[str, set[str]] = {doctype: set() for doctype in _DRAFT_DOCTYPES}
    for row in cases:
        names["GBOS Review Case"].add(str(row["name"]))
        doctype = str(row.get("subject_doctype") or "")
        name = str(row.get("subject_name") or "")
        if doctype in names and name:
            names[doctype].add(name)
    assigned = frappe.get_all(
        "GBOS Work Item",
        filters={"assigned_to": actor},
        fields=["name", "reference_doctype", "reference_name"],
    )
    for row in assigned:
        names["GBOS Work Item"].add(str(row["name"]))
        if row.get("reference_doctype") == "GBOS Informal Observation" and row.get(
            "reference_name"
        ):
            names["GBOS Informal Observation"].add(str(row["reference_name"]))
    return names


def _rows(
    *,
    status: str | None,
    cursor: tuple[str, str] | None,
    page_size: int,
) -> builtins.list[dict[str, Any]]:
    allowed = _reviewer_names()
    result: builtins.list[dict[str, Any]] = []
    for doctype in _DRAFT_DOCTYPES:
        if allowed is not None and not allowed[doctype]:
            continue
        filters: dict[str, Any] = {
            "origin": "AI",
            "review_status": status or ["in", ["AI Draft", "Pending"]],
        }
        if allowed is not None:
            filters["name"] = ["in", sorted(allowed[doctype])]
        subject_field = _SUBJECT_FIELDS[doctype]
        rows = frappe.get_list(
            doctype,
            filters=filters,
            fields=[
                "name",
                subject_field,
                "review_status",
                "origin",
                "origin_reference",
                "revision",
                "modified",
            ],
            order_by="modified desc, name desc",
            page_length=page_size + 1,
        )
        for row in rows:
            modified = str(row["modified"])
            name = str(row["name"])
            if cursor is not None and (modified, name) >= cursor:
                continue
            result.append(
                {
                    "doctype": doctype,
                    "draft_id": name,
                    "kind": _KINDS[doctype],
                    "status": str(row["review_status"]),
                    "origin": str(row["origin"]),
                    "origin_reference": str(row.get("origin_reference") or ""),
                    "subject": str(row[subject_field]),
                    "revision": int(row["revision"]),
                    "modified": modified,
                }
            )
    result.sort(key=lambda row: (row["modified"], row["draft_id"]), reverse=True)
    return result


def _enrich(
    rows: builtins.list[dict[str, Any]],
) -> builtins.list[dict[str, Any]]:
    if not rows:
        return []
    mapped: builtins.list[dict[str, Any]] = []
    try:
        for row in rows:
            origin_reference = row.get("origin_reference")
            if not isinstance(origin_reference, str) or not origin_reference:
                raise V4DTOValidationError("AI draft omitted its Agent origin reference")
            data = call_local(
                "Agent",
                method="GET",
                path=f"/internal/v1/ai-drafts/{quote(origin_reference, safe='')}",
                purpose="ai_draft_review",
            )
            supplement = data.get("draft")
            if not isinstance(supplement, dict):
                raise V4DTOValidationError("Agent omitted a requested draft")
            if supplement.get("draft_id") != origin_reference:
                raise V4DTOValidationError("Agent returned a mismatched draft")
            mapped.append(
                map_ai_draft(
                    {
                        "draft_id": row["draft_id"],
                        "kind": row["kind"],
                        "status": row["status"],
                        "origin": row["origin"],
                        "subject": row["subject"],
                        "revision": row["revision"],
                        "evidence": supplement.get("evidence"),
                        "model": supplement.get("model"),
                    }
                )
            )
    except V4DTOValidationError as error:
        raise BFFError("internal_error", "Agent draft response is invalid", status=503) from error
    return mapped


def _find_draft(draft_id: str, *, for_update: bool = False) -> Any:
    allowed = _reviewer_names()
    for doctype in _DRAFT_DOCTYPES:
        if not frappe.db.exists(doctype, draft_id):
            continue
        if allowed is not None and draft_id not in allowed[doctype]:
            raise BFFError("permission_denied", "Draft is outside assigned scope", status=403)
        doc = frappe.get_doc(doctype, draft_id, for_update=for_update)
        if doc.origin != "AI" or doc.review_status not in {"AI Draft", "Pending"}:
            raise BFFError("not_found", "AI draft was not found", status=404)
        return doc
    raise BFFError("not_found", "AI draft was not found", status=404)


def _local_row(doc: Any) -> dict[str, Any]:
    return {
        "doctype": doc.doctype,
        "draft_id": doc.name,
        "kind": _KINDS[doc.doctype],
        "status": doc.review_status,
        "origin": doc.origin,
        "origin_reference": doc.origin_reference,
        "subject": doc.get(_SUBJECT_FIELDS[doc.doctype]),
        "revision": int(doc.revision),
        "modified": str(doc.modified),
    }


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list(
    status: str | None = None,
    cursor: str | None = None,
    page_size: int | str = 25,
) -> dict[str, Any]:
    require_roles(AI_DRAFT_ROLES)
    if status is not None and status not in {"AI Draft", "Pending"}:
        raise BFFError("invalid_query", "status is not allowed")
    size = _integer(page_size, "page_size", query=True)
    if not 1 <= size <= 50:
        raise BFFError("invalid_query", "page_size is outside the allowed range")
    cursor_value: tuple[str, str] | None = None
    if cursor:
        try:
            cursor_value = decode_cursor(cursor)
        except CursorError as error:
            raise BFFError("invalid_cursor", str(error)) from error
    rows = _rows(status=status, cursor=cursor_value, page_size=size)
    has_more = len(rows) > size
    page = rows[:size]
    next_cursor = (
        encode_cursor(page[-1]["modified"], page[-1]["draft_id"]) if has_more and page else None
    )
    return v4_success(
        {"drafts": _enrich(page), "next_cursor": next_cursor},
        next_cursor=next_cursor,
        page_size=size,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get(draft_id: str) -> dict[str, Any]:
    require_roles(AI_DRAFT_ROLES)
    if not isinstance(draft_id, str) or not draft_id.strip():
        raise BFFError("invalid_query", "draft_id is required")
    draft = _enrich([_local_row(_find_draft(draft_id.strip()))])[0]
    return v4_success({"draft": draft})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def submit_for_review(
    draft_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(AI_DRAFT_ROLES)
    try:
        payload = validate_ai_draft_submit(
            {
                "draft_id": draft_id,
                "expected_revision": _integer(expected_revision, "expected_revision"),
                "idempotency_key": idempotency_key,
            }
        )
    except V4DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    def execute() -> dict[str, Any]:
        doc = _find_draft(payload["draft_id"], for_update=True)
        if int(doc.revision) != payload["expected_revision"]:
            raise BFFError(
                "revision_conflict",
                "AI draft revision is stale",
                status=409,
                details={
                    "expected": payload["expected_revision"],
                    "actual": int(doc.revision),
                },
            )
        try:
            validate_ai_draft_transition(
                origin=doc.origin,
                before=doc.review_status,
                after="Pending",
            )
        except InvalidTransition as error:
            raise BFFError("invalid_transition", str(error), status=409) from error
        doc.flags.gbos_ai_draft_command = True
        if doc.doctype == "GBOS Review Case":
            doc.flags.gbos_review_command = True
        doc.review_status = "Pending"
        doc.last_request_id = request_id()
        doc.save(ignore_permissions=True)
        return {"draft": _enrich([_local_row(doc)])[0]}

    result, replayed, original_request_id = run_idempotent(
        "ai_draft.submit_for_review",
        payload["idempotency_key"],
        payload,
        execute,
        api_version="v4",
    )
    return v4_success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )
