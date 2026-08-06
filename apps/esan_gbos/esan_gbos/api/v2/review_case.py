from __future__ import annotations

import builtins
from typing import Any

import frappe
from frappe.utils import now_datetime

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import (
    BFFError,
    bff_endpoint,
    request_id,
    require_roles,
    success,
)
from esan_gbos.domain.query import CursorError, decode_cursor, encode_cursor
from esan_gbos.domain.review_dto import (
    ReviewDTOValidationError,
    canonical_payload_hash,
    validate_decision_payload,
    validate_evidence_references,
)
from esan_gbos.gbos.doctype.gbos_review_case.gbos_review_case import (
    build_case_payload,
    build_subject_snapshot,
)

REVIEW_ROLES = frozenset({"Reviewer"})


def _parse_json(value: object, *, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    return frappe.parse_json(value) if isinstance(value, str) else value


def _assigned_case(name: str, *, for_update: bool = False) -> Any:
    case = frappe.get_doc("GBOS Review Case", name, for_update=for_update)
    if case.assigned_reviewer != frappe.session.user:
        raise frappe.PermissionError
    return case


def _decision_dto(decision: Any) -> dict[str, Any]:
    return {
        "name": decision.name,
        "review_case": decision.review_case,
        "decision": decision.decision,
        "reviewer": decision.reviewer,
        "reason": decision.reason,
        "case_revision": decision.case_revision,
        "case_payload_sha256": decision.case_payload_sha256,
        "subject_doctype": decision.subject_doctype,
        "subject_name": decision.subject_name,
        "subject_revision": decision.subject_revision,
        "subject_payload_sha256": decision.subject_payload_sha256,
        "evidence_refs": _parse_json(decision.evidence_refs, fallback=[]),
        "policy_version": decision.policy_version,
        "payload_sha256": decision.payload_sha256,
        "request_id": decision.request_id,
        "decided_at": decision.decided_at,
    }


def _case_dto(case: Any) -> dict[str, Any]:
    snapshot = _parse_json(case.subject_snapshot, fallback={})
    evidence_refs = _parse_json(case.evidence_refs, fallback=[])
    return {
        "name": case.name,
        "title": case.title,
        "team": case.team,
        "assigned_reviewer": case.assigned_reviewer,
        "business_status": case.business_status,
        "review_status": case.review_status,
        "case_revision": case.revision,
        "case_payload_hash": case.case_payload_sha256,
        "subject": {
            "doctype": case.subject_doctype,
            "name": case.subject_name,
            "revision": case.subject_revision,
            "payload_hash": case.subject_payload_sha256,
            "snapshot": snapshot,
        },
        "evidence": [
            {"evidence_type": "Evidence", "reference": reference} for reference in evidence_refs
        ],
        "policy_reference": case.policy_version,
        "origin": case.origin,
        "decision_note": case.decision_note,
        "decided_by": case.decided_by,
        "decided_at": case.decided_at,
        "decision_record": case.decision_record,
        "decision_payload_sha256": case.decision_payload_sha256,
    }


def _conflict(
    conflict: str,
    message: str,
    *,
    expected: object | None = None,
    actual: object | None = None,
) -> BFFError:
    details: dict[str, Any] = {"conflict": conflict}
    if expected is not None:
        details["expected"] = expected
    if actual is not None:
        details["actual"] = actual
    return BFFError("revision_conflict", message, status=409, details=details)


def _integer(value: int | str, fieldname: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_dto", f"{fieldname} must be an integer") from error
    if isinstance(value, bool):
        raise BFFError("invalid_dto", f"{fieldname} must be an integer")
    return parsed


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list(
    cursor: str | None = None,
    page_size: int | str = 20,
) -> dict[str, Any]:
    require_roles(REVIEW_ROLES)
    length = _integer(page_size, "page_size")
    if not 1 <= length <= 50:
        raise BFFError("invalid_query", "page_size is outside the allowed range")
    filters = {
        "assigned_reviewer": frappe.session.user,
        "review_status": "Pending",
        "business_status": "Pending",
        "case_payload_sha256": ["is", "set"],
    }
    cursor_value: tuple[str, str] | None = None
    if cursor:
        try:
            cursor_value = decode_cursor(cursor)
        except CursorError as error:
            raise BFFError("invalid_cursor", str(error)) from error
    if cursor_value:
        same_timestamp_filters = {
            **filters,
            "modified": cursor_value[0],
            "name": ["<", cursor_value[1]],
        }
        rows = frappe.get_list(
            "GBOS Review Case",
            filters=same_timestamp_filters,
            fields=["name", "modified"],
            order_by="name desc",
            page_length=length + 1,
        )
        remaining = length + 1 - len(rows)
        if remaining > 0:
            rows.extend(
                frappe.get_list(
                    "GBOS Review Case",
                    filters={**filters, "modified": ["<", cursor_value[0]]},
                    fields=["name", "modified"],
                    order_by="modified desc, name desc",
                    page_length=remaining,
                )
            )
    else:
        rows = frappe.get_list(
            "GBOS Review Case",
            filters=filters,
            fields=["name", "modified"],
            order_by="modified desc, name desc",
            page_length=length + 1,
        )
    has_more = len(rows) > length
    rows = rows[:length]
    next_cursor = (
        encode_cursor(str(rows[-1]["modified"]), str(rows[-1]["name"]))
        if has_more and rows
        else None
    )
    cases = [_case_dto(_assigned_case(str(row["name"]))) for row in rows]
    return success(
        {
            "cases": cases,
            "total": frappe.db.count("GBOS Review Case", filters=filters),
            "page_size": length,
            "next_cursor": next_cursor,
        },
        page_size=length,
        next_cursor=next_cursor,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get(name: str) -> dict[str, Any]:
    require_roles(REVIEW_ROLES)
    case = _assigned_case(name)
    decision = None
    if case.decision_record:
        decision = _decision_dto(frappe.get_doc("GBOS Review Decision", case.decision_record))
    return success({"case": _case_dto(case), "decision": decision})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def decide(
    name: str,
    decision: str,
    decision_note: str,
    expected_revision: int | str,
    expected_subject_revision: int | str,
    idempotency_key: str,
    subject_payload_sha256: str,
    evidence_refs: str | builtins.list[str],
    policy_version: str,
    expected_case_payload_hash: str | None = None,
) -> dict[str, Any]:
    require_roles(REVIEW_ROLES)
    raw: dict[str, Any] = {
        "name": name,
        "decision": decision,
        "decision_note": decision_note,
        "expected_revision": _integer(expected_revision, "expected_revision"),
        "expected_subject_revision": _integer(
            expected_subject_revision,
            "expected_subject_revision",
        ),
        "idempotency_key": idempotency_key,
        "subject_payload_sha256": subject_payload_sha256,
        "evidence_refs": _parse_json(evidence_refs, fallback=[]),
        "policy_version": policy_version,
    }
    if expected_case_payload_hash is not None:
        raw["expected_case_payload_hash"] = expected_case_payload_hash
    try:
        payload = validate_decision_payload(raw)
    except ReviewDTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    def execute() -> dict[str, Any]:
        case = _assigned_case(payload["name"], for_update=True)
        if case.business_status != "Pending" or case.review_status != "Pending":
            raise _conflict(
                "case_not_pending",
                "Review Case is no longer Pending",
                actual=case.business_status,
            )
        if int(case.revision) != payload["expected_revision"]:
            raise _conflict(
                "case_revision",
                "Review Case revision is stale",
                expected=payload["expected_revision"],
                actual=int(case.revision),
            )

        try:
            stored_case_hash = canonical_payload_hash(build_case_payload(case))
            stored_evidence = validate_evidence_references(
                _parse_json(case.evidence_refs, fallback=[])
            )
        except ReviewDTOValidationError as error:
            raise _conflict("case_unpinned", "Review Case is not fully pinned") from error
        if stored_case_hash != case.case_payload_sha256:
            raise _conflict("case_payload", "Stored Review Case scope hash is stale")
        expected_case_hash = payload.get("expected_case_payload_hash")
        if expected_case_hash is not None and expected_case_hash != stored_case_hash:
            raise _conflict(
                "case_payload",
                "Review Case payload hash is stale",
                expected=expected_case_hash,
                actual=stored_case_hash,
            )
        if int(case.subject_revision or 0) != payload["expected_subject_revision"]:
            raise _conflict(
                "subject_revision",
                "Pinned subject revision differs from the command",
                expected=payload["expected_subject_revision"],
                actual=int(case.subject_revision or 0),
            )
        if case.subject_payload_sha256 != payload["subject_payload_sha256"]:
            raise _conflict(
                "subject_payload",
                "Pinned subject payload differs from the command",
                expected=payload["subject_payload_sha256"],
                actual=case.subject_payload_sha256,
            )
        if stored_evidence != payload["evidence_refs"]:
            raise _conflict("evidence_refs", "Evidence references differ from the pinned case")
        if case.policy_version != payload["policy_version"]:
            raise _conflict("policy_version", "Policy version differs from the pinned case")

        try:
            subject = frappe.get_doc(case.subject_doctype, case.subject_name)
        except frappe.DoesNotExistError as error:
            raise _conflict("subject_missing", "Review subject no longer exists") from error
        live_snapshot = build_subject_snapshot(subject)
        live_revision = int(live_snapshot["revision"])
        live_hash = canonical_payload_hash(live_snapshot)
        if live_revision != payload["expected_subject_revision"]:
            raise _conflict(
                "subject_revision",
                "Review subject revision is stale",
                expected=payload["expected_subject_revision"],
                actual=live_revision,
            )
        if live_hash != payload["subject_payload_sha256"]:
            raise _conflict(
                "subject_payload",
                "Review subject payload is stale",
                expected=payload["subject_payload_sha256"],
                actual=live_hash,
            )

        current_request_id = request_id()
        decided_at = now_datetime()
        decision_hash = canonical_payload_hash(payload)
        audit = frappe.get_doc(
            {
                "doctype": "GBOS Review Decision",
                "review_case": case.name,
                "decision": payload["decision"],
                "reviewer": frappe.session.user,
                "reason": payload["decision_note"],
                "case_revision": int(case.revision),
                "case_payload_sha256": stored_case_hash,
                "subject_doctype": case.subject_doctype,
                "subject_name": case.subject_name,
                "subject_revision": live_revision,
                "subject_payload_sha256": live_hash,
                "subject_snapshot": case.subject_snapshot,
                "evidence_refs": case.evidence_refs,
                "policy_version": case.policy_version,
                "payload_sha256": decision_hash,
                "request_id": current_request_id,
                "decided_at": decided_at,
            }
        ).insert(ignore_permissions=True)

        case.flags.gbos_review_command = True
        case.business_status = payload["decision"]
        case.review_status = payload["decision"]
        case.decision_note = payload["decision_note"]
        case.decided_by = frappe.session.user
        case.decided_at = decided_at
        case.decision_record = audit.name
        case.decision_payload_sha256 = decision_hash
        case.last_request_id = current_request_id
        case.save(ignore_permissions=True)
        return {
            "case": _case_dto(case),
            "decision": _decision_dto(audit),
        }

    result, replayed, original_request_id = run_idempotent(
        "review_case.decide",
        payload["idempotency_key"],
        payload,
        execute,
        api_version="v2",
    )
    return success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )
