from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import (
    BFFError,
    bff_endpoint,
    request_id,
    require_doc_permission,
    require_roles,
    success,
)
from esan_gbos.domain.access_policy import SAMPLE_READ_ROLES
from esan_gbos.domain.dto import DTOValidationError, validate_payload
from esan_gbos.domain.revision import RevisionConflict, next_revision
from esan_gbos.domain.state_machine import InvalidTransition, validate_transition

READ_ROLES = SAMPLE_READ_ROLES
WRITE_ROLES = {"GBOS Admin", "Sales Manager", "Sales User", "Product/R&D"}


def _validate_link_scope(
    *,
    team: str,
    party_profile: str | None,
    product_brief: str | None,
    deal: str | None,
) -> None:
    if party_profile:
        profile_team = frappe.db.get_value("GBOS Party Profile", party_profile, "team")
        if profile_team != team:
            raise BFFError("scope_mismatch", "Party Profile is outside the selected team")
    if product_brief:
        brief = frappe.db.get_value(
            "GBOS Product Brief",
            product_brief,
            ["team", "party_profile", "deal"],
            as_dict=True,
        )
        if not brief or brief.team != team:
            raise BFFError("scope_mismatch", "Product Brief is outside the selected team")
        if party_profile and brief.party_profile and brief.party_profile != party_profile:
            raise BFFError("scope_mismatch", "Product Brief belongs to another party")
        if deal and brief.deal != deal:
            raise BFFError("scope_mismatch", "Product Brief does not belong to the Deal")
    elif deal:
        raise BFFError("invalid_dto", "deal requires product_brief")


@frappe.whitelist(methods=["GET"])
@bff_endpoint("GET")
def get_status(project: str) -> dict[str, Any]:
    require_roles(READ_ROLES)
    doc = frappe.get_doc("GBOS Sample Project", project)
    require_doc_permission(doc, "read")
    data = {
        "project": {
            "name": doc.name,
            "title": doc.title,
            "team": doc.team,
            "party_profile": doc.party_profile,
            "product_brief": doc.product_brief,
            "origin": doc.origin,
            "business_status": doc.business_status,
            "review_status": doc.review_status,
            "revision": doc.revision,
            "modified": doc.modified,
        },
        "iterations": frappe.get_list(
            "GBOS Sample Iteration",
            filters={"sample_project": project},
            fields=[
                "name",
                "iteration_number",
                "summary",
                "origin",
                "business_status",
                "review_status",
                "revision",
            ],
            order_by="iteration_number desc",
            page_length=100,
        ),
        "shipments": frappe.get_list(
            "GBOS Sample Shipment",
            filters={"sample_project": project},
            fields=[
                "name",
                "carrier",
                "tracking_number",
                "shipped_on",
                "delivered_on",
                "origin",
                "business_status",
                "revision",
            ],
            order_by="modified desc",
            page_length=100,
        ),
        "feedback": frappe.get_list(
            "GBOS Sample Feedback",
            filters={"sample_project": project},
            fields=[
                "name",
                "summary",
                "rating",
                "received_on",
                "origin",
                "review_status",
                "revision",
            ],
            order_by="modified desc",
            page_length=100,
        ),
    }
    return success(data)


@frappe.whitelist(methods=["POST"])
@bff_endpoint("POST")
def create_project(
    team: str,
    title: str,
    expected_revision: int,
    idempotency_key: str,
    party_profile: str | None = None,
    product_brief: str | None = None,
    deal: str | None = None,
    origin: str = "Manual",
) -> dict[str, Any]:
    require_roles(WRITE_ROLES)
    raw = {
        "team": team,
        "title": title,
        "expected_revision": int(expected_revision),
        "idempotency_key": idempotency_key,
        "origin": origin,
    }
    for key, value in (
        ("party_profile", party_profile),
        ("product_brief", product_brief),
        ("deal", deal),
    ):
        if value is not None:
            raw[key] = value
    try:
        payload = validate_payload("sample.create_project", raw)
    except DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error
    if payload["expected_revision"] != 0:
        raise BFFError(
            "revision_conflict",
            "create requires expected_revision 0",
            status=409,
        )

    def execute() -> dict[str, Any]:
        _validate_link_scope(
            team=team,
            party_profile=party_profile,
            product_brief=product_brief,
            deal=deal,
        )
        doc = frappe.get_doc(
            {
                "doctype": "GBOS Sample Project",
                "team": team,
                "title": title,
                "party_profile": party_profile,
                "product_brief": product_brief,
                "origin": origin,
                "business_status": "Draft",
                "review_status": "Pending",
                "last_request_id": request_id(),
            }
        )
        require_doc_permission(doc, "create")
        doc.insert()
        return {
            "doctype": doc.doctype,
            "name": doc.name,
            "business_status": doc.business_status,
            "review_status": doc.review_status,
            "revision": doc.revision,
        }

    result, replayed, original_request_id = run_idempotent(
        "sample.create_project",
        idempotency_key,
        payload,
        execute,
    )
    return success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )


@frappe.whitelist(methods=["POST"])
@bff_endpoint("POST")
def record_feedback(
    project: str,
    summary: str,
    expected_revision: int,
    idempotency_key: str,
    rating: float | None = None,
    received_on: str | None = None,
) -> dict[str, Any]:
    require_roles(WRITE_ROLES)
    raw: dict[str, Any] = {
        "project": project,
        "summary": summary,
        "expected_revision": int(expected_revision),
        "idempotency_key": idempotency_key,
    }
    if rating is not None:
        raw["rating"] = float(rating)
    if received_on is not None:
        raw["received_on"] = received_on
    try:
        payload = validate_payload("sample.record_feedback", raw)
    except DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    def execute() -> dict[str, Any]:
        sample = frappe.get_doc("GBOS Sample Project", project)
        require_doc_permission(sample, "write")
        try:
            next_revision(
                expected=payload["expected_revision"],
                current=int(sample.revision),
            )
            validate_transition("sample", sample.business_status, "Feedback")
        except RevisionConflict as error:
            raise BFFError("revision_conflict", str(error), status=409) from error
        except InvalidTransition as error:
            raise BFFError("invalid_transition", str(error), status=409) from error

        feedback = frappe.get_doc(
            {
                "doctype": "GBOS Sample Feedback",
                "team": sample.team,
                "sample_project": sample.name,
                "summary": summary,
                "rating": rating,
                "received_on": received_on,
                "origin": "Manual",
                "business_status": "Received",
                "review_status": "Pending",
                "last_request_id": request_id(),
            }
        )
        require_doc_permission(feedback, "create")
        feedback.insert()
        sample.business_status = "Feedback"
        sample.last_request_id = request_id()
        sample.save()
        return {
            "doctype": feedback.doctype,
            "name": feedback.name,
            "project": sample.name,
            "project_status": sample.business_status,
            "project_revision": sample.revision,
            "revision": feedback.revision,
        }

    result, replayed, original_request_id = run_idempotent(
        "sample.record_feedback",
        idempotency_key,
        payload,
        execute,
    )
    return success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )
