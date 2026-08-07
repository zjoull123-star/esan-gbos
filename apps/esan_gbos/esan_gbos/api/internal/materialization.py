"""Authenticated Frappe boundary for controlled Agent AI Draft materialization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from typing import Any

import frappe

_TRUSTED_ROLE = "Agent TrustedMaterializer"
_MODEL = "deepseek-v4-flash"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_TEXT = re.compile(r"^[^\r\n]+$")
_ALLOWED_SUBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "GBOS Demand Signal": (
        "title",
        "team",
        "party_profile",
        "product_brief",
        "quantity",
        "uom",
        "needed_by",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Party Profile": (
        "party_name",
        "team",
        "crm_organization",
        "contact",
        "crm_lead",
        "crm_deal",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Product Brief": (
        "title",
        "team",
        "party_profile",
        "deal",
        "description",
        "target_quantity",
        "target_uom",
        "target_date",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Sample Feedback": (
        "team",
        "sample_project",
        "sample_iteration",
        "summary",
        "rating",
        "received_on",
        "received_from_contact",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Sample Iteration": (
        "team",
        "sample_project",
        "iteration_number",
        "summary",
        "started_on",
        "completed_on",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Sample Project": (
        "title",
        "team",
        "party_profile",
        "product_brief",
        "owner_user",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Sample Shipment": (
        "team",
        "sample_project",
        "sample_iteration",
        "carrier",
        "tracking_number",
        "shipped_on",
        "delivered_on",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Sourcing Event": (
        "title",
        "team",
        "demand_signal",
        "selected_supplier",
        "owner_user",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
    "GBOS Work Item": (
        "title",
        "team",
        "assigned_to",
        "priority",
        "due_date",
        "reference_doctype",
        "reference_name",
        "blocked_reason",
        "origin",
        "origin_reference",
        "business_status",
        "review_status",
    ),
}
_CREATE_FIELDS = {
    "GBOS Work Item": frozenset(
        {
            "title",
            "team",
            "reference_doctype",
            "reference_name",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
        }
    ),
    "GBOS Review Case": frozenset(
        {
            "title",
            "team",
            "assigned_reviewer",
            "subject_doctype",
            "subject_name",
            "subject_revision",
            "subject_payload_sha256",
            "subject_snapshot",
            "case_payload_sha256",
            "evidence_refs",
            "policy_version",
            "origin",
            "origin_reference",
            "business_status",
            "review_status",
        }
    ),
    "GBOS Informal Observation": frozenset(
        {
            "subject",
            "summary_zh",
            "team",
            "evidence_refs",
            "model_name",
            "model_version",
            "is_official_metric",
            "origin",
            "origin_reference",
            "review_status",
        }
    ),
}
_RESOLVE_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "task_id",
        "proposal_id",
        "subject_type",
        "subject_ref",
        "subject_revision",
    }
)
_APPLY_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "request_id",
        "auth_ref",
        "request_digest",
        "intent",
    }
)
_INTENT_FIELDS = frozenset({"operation", "doctype", "values"})
_SUBMIT_FIELDS = frozenset({"name", "origin", "review_status"})


class _APIError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def canonical_request_digest(intent: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(intent, ensure_ascii=False).encode()).hexdigest()


def _endpoint[Endpoint: Callable[[dict[str, Any]], dict[str, Any]]](
    function: Endpoint,
) -> Endpoint:
    @wraps(function)
    def wrapped(payload: str | dict[str, Any]) -> dict[str, Any]:
        try:
            frappe.local.response.pop("http_status_code", None)
            return function(_object(payload, "payload"))
        except _APIError as error:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = error.status
            return {"error": {"code": error.code}}
        except frappe.PermissionError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 403
            return {"error": {"code": "permission_denied"}}
        except frappe.DoesNotExistError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 404
            return {"error": {"code": "not_found"}}
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 409
            return {"error": {"code": "request_in_progress"}}
        except frappe.ValidationError:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 422
            return {"error": {"code": "validation_error"}}
        except Exception:
            frappe.db.rollback()
            frappe.local.response["http_status_code"] = 500
            return {"error": {"code": "internal_error"}}

    return wrapped  # type: ignore[return-value]


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def resolve_context(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, _RESOLVE_FIELDS, "invalid_context_request")
    identity = _authenticate(payload)
    del identity
    subject_type = _text(payload["subject_type"], maximum=140)
    subject_ref = _text(payload["subject_ref"], maximum=256)
    revision = _positive_integer(payload["subject_revision"])
    snapshot, digest, team, reviewer = _controlled_context(
        subject_type,
        subject_ref,
        revision,
    )
    return {
        "site_id": payload["site_id"],
        "request_id": payload["request_id"],
        "subject_type": subject_type,
        "subject_ref": subject_ref,
        "subject_revision": revision,
        "team": team,
        "assigned_reviewer": reviewer,
        "subject_snapshot": snapshot,
        "subject_payload_digest": digest,
    }


@frappe.whitelist(methods=["POST"])  # type: ignore[untyped-decorator]
@_endpoint
def apply_draft(payload: dict[str, Any]) -> dict[str, Any]:
    _exact_fields(payload, _APPLY_FIELDS, "invalid_materialization_request")
    _authenticate(payload)
    request_id = _text(payload["request_id"], maximum=256)
    request_digest = payload["request_digest"]
    intent = _object(payload["intent"], "intent")
    _exact_fields(intent, _INTENT_FIELDS, "invalid_intent")
    if (
        not isinstance(request_digest, str)
        or _DIGEST.fullmatch(request_digest) is None
        or canonical_request_digest(intent) != request_digest
    ):
        raise _APIError("invalid_request_digest", 422)
    return _run_idempotent(
        request_id=request_id,
        request_digest=request_digest,
        payload=payload,
        intent=intent,
    )


def _authenticate(payload: dict[str, Any]) -> dict[str, Any]:
    request = getattr(frappe.local, "request", None)
    if request is None or str(getattr(request, "method", "")).upper() != "POST":
        raise _APIError("method_not_allowed", 405)
    identities = frappe.conf.get("gbos_agent_materialization_identities")
    if not isinstance(identities, dict) or not identities:
        raise _APIError("service_unconfigured", 503)
    actor = str(getattr(frappe.session, "user", ""))
    headers = request.headers
    authorization = str(headers.get("Authorization") or "")
    if (
        not authorization.startswith("token ")
        or ":" not in authorization[6:]
        or not authorization[6:].split(":", 1)[0]
        or not authorization[6:].split(":", 1)[1]
        or actor == "Guest"
        or _TRUSTED_ROLE not in frappe.get_roles(actor)
    ):
        raise _APIError("authentication_required", 401)
    auth_ref = _text(payload.get("auth_ref"), maximum=140)
    identity = identities.get(auth_ref)
    if not isinstance(identity, dict) or set(identity) != {
        "user",
        "site_id",
        "processing_purposes",
    }:
        raise _APIError("identity_scope_mismatch", 403)
    active_site = str(getattr(frappe.local, "site", ""))
    site_id = _text(payload.get("site_id"), maximum=140)
    purpose = _text(payload.get("processing_purpose"), maximum=80)
    request_id = _text(payload.get("request_id"), maximum=256)
    purposes = identity.get("processing_purposes")
    if (
        identity.get("user") != actor
        or identity.get("site_id") != site_id
        or site_id != active_site
        or not isinstance(purposes, list)
        or not all(isinstance(item, str) for item in purposes)
        or purpose not in purposes
        or headers.get("X-Site-ID") != site_id
        or headers.get("X-Processing-Purpose") != purpose
        or headers.get("X-Request-ID") != request_id
        or headers.get("X-GBOS-Frappe-Auth-Ref") != auth_ref
    ):
        raise _APIError("identity_scope_mismatch", 403)
    return identity


def _controlled_context(
    subject_type: str,
    subject_ref: str,
    revision: int,
) -> tuple[dict[str, Any], str, str, str]:
    if subject_type not in _ALLOWED_SUBJECT_FIELDS:
        raise _APIError("subject_not_allowed", 422)
    subject = frappe.get_doc(subject_type, subject_ref)
    subject.check_permission("read")
    if int(subject.get("revision") or 0) != revision:
        raise _APIError("revision_conflict", 409)
    team = _text(subject.get("team"), maximum=140)
    team_doc = frappe.get_doc("GBOS Team", team)
    team_doc.check_permission("read")
    reviewers = {
        user
        for member in (team_doc.get("members") or [])
        if (_member_value(member, "team_role") == "Reviewer")
        and int(_member_value(member, "enabled") or 0) == 1
        and (user := str(_member_value(member, "user") or ""))
        and int(frappe.db.get_value("User", user, "enabled") or 0) == 1
        and "Reviewer" in frappe.get_roles(user)
    }
    if len(reviewers) != 1:
        raise _APIError("reviewer_scope_ambiguous", 409)
    snapshot: dict[str, Any] = {
        "doctype": subject_type,
        "name": subject_ref,
        "revision": revision,
    }
    for field_name in _ALLOWED_SUBJECT_FIELDS[subject_type]:
        snapshot[field_name] = _json_value(subject.get(field_name))
    return (
        snapshot,
        _canonical_digest(snapshot),
        team,
        next(iter(reviewers)),
    )


def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    operation = intent.get("operation")
    doctype = intent.get("doctype")
    values = _object(intent.get("values"), "values")
    if operation == "create":
        if doctype not in _CREATE_FIELDS:
            raise _APIError("invalid_intent", 422)
        _exact_fields(values, _CREATE_FIELDS[str(doctype)], "invalid_intent")
        _validate_create(str(doctype), values)
    elif operation == "submit":
        if doctype not in {"GBOS Work Item", "GBOS Review Case"}:
            raise _APIError("invalid_intent", 422)
        _exact_fields(values, _SUBMIT_FIELDS, "invalid_intent")
        if values.get("origin") != "AI" or values.get("review_status") != "Pending":
            raise _APIError("invalid_intent", 422)
        _text(values.get("name"), maximum=256)
    else:
        raise _APIError("invalid_intent", 422)
    return {"operation": operation, "doctype": doctype, "values": values}


def _validate_create(doctype: str, values: dict[str, Any]) -> None:
    if values.get("origin") != "AI" or values.get("review_status") != "AI Draft":
        raise _APIError("invalid_intent", 422)
    _text(values.get("origin_reference"), maximum=256)
    team = _text(values.get("team"), maximum=140)
    team_doc = frappe.get_doc("GBOS Team", team)
    team_doc.check_permission("read")
    if doctype == "GBOS Work Item":
        if values.get("business_status") != "Open":
            raise _APIError("invalid_intent", 422)
        reference_type = _text(values.get("reference_doctype"), maximum=140)
        reference_name = _text(values.get("reference_name"), maximum=256)
        if reference_type not in _ALLOWED_SUBJECT_FIELDS:
            raise _APIError("invalid_intent", 422)
        reference = frappe.get_doc(reference_type, reference_name)
        reference.check_permission("read")
        if reference.get("team") != team:
            raise _APIError("identity_scope_mismatch", 403)
        _text(values.get("title"), maximum=140)
    elif doctype == "GBOS Informal Observation":
        if (
            values.get("model_name") != _MODEL
            or values.get("model_version") != _MODEL
            or values.get("is_official_metric") is not False
        ):
            raise _APIError("invalid_intent", 422)
        _text(values.get("subject"), maximum=140)
        _text(values.get("summary_zh"), maximum=2000)
        evidence = values.get("evidence_refs")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(
                not isinstance(item, dict)
                or set(item) != {"evidence_ref", "locator_ref"}
                or item["evidence_ref"] != item["locator_ref"]
                or not isinstance(item["evidence_ref"], str)
                or not item["evidence_ref"]
                for item in evidence
            )
        ):
            raise _APIError("invalid_intent", 422)
    else:
        _validate_review_case(values, expected_team=team)


def _validate_review_case(values: dict[str, Any], *, expected_team: str) -> None:
    if values.get("business_status") != "Pending":
        raise _APIError("invalid_intent", 422)
    subject_type = _text(values.get("subject_doctype"), maximum=140)
    subject_name = _text(values.get("subject_name"), maximum=256)
    revision = _positive_integer(values.get("subject_revision"))
    snapshot, digest, team, reviewer = _controlled_context(
        subject_type,
        subject_name,
        revision,
    )
    stored_snapshot = _object(values.get("subject_snapshot"), "subject_snapshot")
    evidence = _list_of_text(values.get("evidence_refs"), "evidence_refs")
    case_payload = {
        "title": values.get("title"),
        "team": values.get("team"),
        "assigned_reviewer": values.get("assigned_reviewer"),
        "subject_doctype": subject_type,
        "subject_name": subject_name,
        "subject_revision": revision,
        "subject_payload_sha256": digest,
        "subject_snapshot": snapshot,
        "evidence_refs": evidence,
        "policy_version": values.get("policy_version"),
    }
    if (
        expected_team != team
        or values.get("assigned_reviewer") != reviewer
        or stored_snapshot != snapshot
        or values.get("subject_payload_sha256") != digest
        or values.get("case_payload_sha256") != _canonical_digest(case_payload)
    ):
        raise _APIError("invalid_intent", 422)
    _text(values.get("title"), maximum=140)
    _text(values.get("policy_version"), maximum=140)


def _run_idempotent(
    *,
    request_id: str,
    request_digest: str,
    payload: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    name = _audit_name(
        site_id=str(payload["site_id"]),
        auth_ref=str(payload["auth_ref"]),
        request_id=request_id,
    )
    existing = _load_audit(name)
    if existing is not None:
        if existing["request_digest"] != request_digest:
            raise _APIError("idempotency_conflict", 409)
        if existing["status"] != "Completed" or not isinstance(existing["output"], dict):
            raise _APIError("request_in_progress", 409)
        return existing["output"]
    normalized_intent = _validate_intent(intent)
    audit = frappe.get_doc(
        {
            "doctype": "Integration Request",
            "request_id": request_id,
            "integration_request_service": "esan_gbos.internal.materialization",
            "request_description": "Governed Agent AI Draft materialization",
            "status": "Authorized",
            "data": _canonical_json(
                {
                    "site_id": payload["site_id"],
                    "processing_purpose": payload["processing_purpose"],
                    "auth_ref": payload["auth_ref"],
                    "actor": frappe.session.user,
                    "request_id": request_id,
                    "request_digest": request_digest,
                }
            ),
        }
    )
    audit.check_permission("create")
    audit.insert(set_name=name)
    receipt = _execute_intent(
        normalized_intent,
        request_id=request_id,
        request_digest=request_digest,
    )
    audit.status = "Completed"
    audit.output = _canonical_json(receipt)
    audit.reference_doctype = receipt["doctype"]
    audit.reference_docname = receipt["name"]
    audit.check_permission("write")
    audit.save()
    return receipt


def _execute_intent(
    intent: dict[str, Any],
    *,
    request_id: str,
    request_digest: str,
) -> dict[str, Any]:
    operation = str(intent["operation"])
    doctype = str(intent["doctype"])
    values = dict(intent["values"])
    if operation == "create":
        doc = frappe.get_doc(
            {
                "doctype": doctype,
                **values,
                "last_request_id": request_id,
            }
        )
        doc.check_permission("create")
        doc.insert()
    else:
        doc = frappe.get_doc(doctype, values["name"])
        doc.check_permission("write")
        allowed_business = (
            {"Open", "In Progress", "Blocked"} if doctype == "GBOS Work Item" else {"Pending"}
        )
        if (
            doc.get("origin") != "AI"
            or doc.get("review_status") != "AI Draft"
            or doc.get("business_status") not in allowed_business
        ):
            raise _APIError("invalid_transition", 409)
        doc.flags.gbos_ai_draft_command = True
        if doctype == "GBOS Review Case":
            doc.flags.gbos_review_command = True
        doc.review_status = "Pending"
        doc.last_request_id = request_id
        doc.save()
    return {
        "site_id": str(getattr(frappe.local, "site", "")),
        "doctype": doctype,
        "name": str(doc.name),
        "revision": int(doc.get("revision") or 0),
        "request_id": request_id,
        "request_digest": request_digest,
    }


def _audit_name(*, site_id: str, auth_ref: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{site_id}\0{auth_ref}\0{request_id}".encode()).hexdigest()
    return f"GBOS-MATERIALIZE-{digest[:44]}"


def _load_audit(name: str) -> dict[str, Any] | None:
    if not frappe.db.exists("Integration Request", name):
        return None
    audit = frappe.get_doc("Integration Request", name)
    audit.check_permission("read")
    try:
        data = json.loads(audit.get("data") or "{}")
        output = json.loads(audit.get("output")) if audit.get("output") else None
    except TypeError, json.JSONDecodeError:
        raise _APIError("idempotency_conflict", 409) from None
    return {
        "request_digest": data.get("request_digest"),
        "status": audit.get("status"),
        "output": output,
    }


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError(f"invalid_{field_name}", 422) from None
    if not isinstance(value, dict):
        raise _APIError(f"invalid_{field_name}", 422)
    return dict(value)


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], code: str) -> None:
    if set(value) != set(expected):
        raise _APIError(code, 422)


def _text(value: Any, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _TEXT.fullmatch(value) is None
    ):
        raise _APIError("invalid_request", 422)
    return value.strip()


def _positive_integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _APIError("invalid_request", 422)
    return value


def _list_of_text(value: Any, field_name: str) -> list[str]:
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            raise _APIError(f"invalid_{field_name}", 422) from None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise _APIError(f"invalid_{field_name}", 422)
    return list(value)


def _member_value(member: Any, field_name: str) -> Any:
    if isinstance(member, Mapping):
        return member.get(field_name)
    return getattr(member, field_name, None)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _canonical_json(value: Any, *, ensure_ascii: bool = True) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
            sort_keys=True,
        )
    except TypeError, ValueError:
        raise _APIError("invalid_json", 422) from None


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


__all__ = ["apply_draft", "canonical_request_digest", "resolve_context"]
