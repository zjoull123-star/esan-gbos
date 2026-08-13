from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import now_datetime

from esan_gbos.domain.review_dto import (
    REVIEW_SUBJECT_DOCTYPES,
    ReviewDTOValidationError,
    canonical_payload_hash,
    validate_evidence_references,
    validate_subject_pin,
)
from esan_gbos.gbos.doctype.base import GBOSDocument

_IMMUTABLE_SCOPE_FIELDS = (
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
)
_DECISION_FIELDS = (
    "review_status",
    "decision_note",
    "decided_by",
    "decided_at",
    "decision_record",
    "decision_payload_sha256",
)

_SUBJECT_FIELDS = {
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
    "GBOS External Identity": (
        "team",
        "identity_provider",
        "external_subject",
        "identity_type",
        "user",
        "party_profile",
        "origin",
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


def _value(source: object, fieldname: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(fieldname, default)
    return getattr(source, fieldname, default)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _json_object(value: object, fieldname: str) -> dict[str, Any]:
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ReviewDTOValidationError(f"{fieldname} must be a JSON object")
    return parsed


def _json_list(value: object, fieldname: str) -> list[str]:
    parsed = frappe.parse_json(value) if isinstance(value, str) else value
    try:
        return validate_evidence_references(parsed)
    except ReviewDTOValidationError as error:
        raise ReviewDTOValidationError(f"{fieldname}: {error}") from error


def build_subject_snapshot(doc: object) -> dict[str, Any]:
    doctype = str(_value(doc, "doctype", ""))
    if doctype not in REVIEW_SUBJECT_DOCTYPES:
        raise ReviewDTOValidationError("subject_doctype is not reviewable")
    snapshot = {
        "doctype": doctype,
        "name": str(_value(doc, "name", "")),
        "revision": int(_value(doc, "revision", 0) or 0),
    }
    for fieldname in _SUBJECT_FIELDS[doctype]:
        snapshot[fieldname] = _json_value(_value(doc, fieldname))
    return snapshot


def build_case_payload(case: object) -> dict[str, Any]:
    snapshot_value = _value(case, "subject_snapshot")
    evidence_value = _value(case, "evidence_refs")
    return {
        "title": _value(case, "title"),
        "team": _value(case, "team"),
        "assigned_reviewer": _value(case, "assigned_reviewer"),
        "subject_doctype": _value(case, "subject_doctype"),
        "subject_name": _value(case, "subject_name"),
        "subject_revision": int(_value(case, "subject_revision", 0) or 0),
        "subject_payload_sha256": _value(case, "subject_payload_sha256"),
        "subject_snapshot": _json_object(snapshot_value, "subject_snapshot"),
        "evidence_refs": _json_list(evidence_value, "evidence_refs"),
        "policy_version": _value(case, "policy_version"),
    }


class GBOSReviewCase(GBOSDocument):
    def validate(self) -> None:
        self._validate_scope()
        self._protect_command_boundary()
        self._sync_decision_state()
        super().validate()

    def _validate_scope(self) -> None:
        if self.subject_doctype not in REVIEW_SUBJECT_DOCTYPES:
            frappe.throw("Subject DocType is not reviewable", title="Invalid review subject")

        pins = (
            self.subject_revision,
            self.subject_payload_sha256,
            self.subject_snapshot,
            self.case_payload_sha256,
            self.evidence_refs,
            self.policy_version,
        )
        if not any(value not in (None, "", 0) for value in pins):
            return
        try:
            snapshot = _json_object(self.subject_snapshot, "subject_snapshot")
            validate_subject_pin(
                subject_doctype=self.subject_doctype,
                subject_name=self.subject_name,
                subject_revision=int(self.subject_revision or 0),
                subject_payload_hash=self.subject_payload_sha256,
                subject_snapshot=snapshot,
            )
            _json_list(self.evidence_refs, "evidence_refs")
            if not isinstance(self.policy_version, str) or not self.policy_version.strip():
                raise ReviewDTOValidationError("policy_version must be nonempty")
            if canonical_payload_hash(build_case_payload(self)) != self.case_payload_sha256:
                raise ReviewDTOValidationError("case_payload_sha256 does not match case scope")
        except ReviewDTOValidationError as error:
            frappe.throw(str(error), title="Invalid review pin")

    def _protect_command_boundary(self) -> None:
        before = self.get_doc_before_save()
        if not before:
            return
        for fieldname in _IMMUTABLE_SCOPE_FIELDS:
            if self.get(fieldname) != before.get(fieldname):
                raise frappe.PermissionError
        if before.business_status != "Pending":
            raise frappe.PermissionError
        if not self.flags.gbos_review_command:
            for fieldname in _DECISION_FIELDS:
                if self.get(fieldname) != before.get(fieldname):
                    raise frappe.PermissionError
        if self.business_status != before.business_status and not self.flags.gbos_review_command:
            raise frappe.PermissionError

    def _sync_decision_state(self) -> None:
        before = self.get_doc_before_save()
        if not before or self.business_status == before.business_status:
            return
        if self.business_status in {"Approved", "Rejected", "Superseded"}:
            self.review_status = self.business_status
            self.set("decided_at", self.get("decided_at") or now_datetime())

    def on_update(self) -> None:
        before = self.get_doc_before_save()
        if (
            not before
            or self.business_status == before.business_status
            or self.subject_doctype != "GBOS External Identity"
        ):
            return
        if not self.flags.gbos_review_command:
            raise frappe.PermissionError
        if str(self.origin_reference or "").startswith("identity-human:v1:") and not getattr(
            self.flags, "gbos_human_identity_approval", False
        ):
            raise frappe.PermissionError

        from esan_gbos.gbos.doctype.gbos_external_identity.gbos_external_identity import (
            review_state_for_decision,
        )

        try:
            review_status, business_status = review_state_for_decision(self.business_status)
        except ValueError:
            raise frappe.PermissionError from None
        subject = frappe.get_doc(self.subject_doctype, self.subject_name, for_update=True)
        if (
            int(subject.get("revision") or 0) != int(self.subject_revision or 0)
            or subject.get("review_status") != "Pending"
        ):
            frappe.throw("external identity review pin is stale", title="Revision conflict")
        subject.flags.gbos_identity_review_decision = True
        subject.review_status = review_status
        subject.business_status = business_status
        subject.last_request_id = self.last_request_id
        subject.save(ignore_permissions=True)

    def on_trash(self) -> None:
        raise frappe.PermissionError
