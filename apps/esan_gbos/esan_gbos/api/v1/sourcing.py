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
from esan_gbos.domain.access_policy import SOURCING_READ_ROLES
from esan_gbos.domain.dto import DTOValidationError, validate_payload
from esan_gbos.domain.revision import RevisionConflict, next_revision
from esan_gbos.domain.state_machine import InvalidTransition, validate_transition

READ_ROLES = SOURCING_READ_ROLES
WRITE_ROLES = {"GBOS Admin", "Purchase Manager", "Buyer"}


@frappe.whitelist(methods=["GET"])
@bff_endpoint("GET")
def get_board(team: str | None = None) -> dict[str, Any]:
    require_roles(READ_ROLES)
    filters = {"team": team} if team else {}
    events = frappe.get_list(
        "GBOS Sourcing Event",
        filters=filters,
        fields=[
            "name",
            "title",
            "team",
            "demand_signal",
            "selected_supplier",
            "owner_user",
            "origin",
            "business_status",
            "review_status",
            "revision",
            "modified",
        ],
        order_by="modified desc",
        page_length=100,
    )
    lanes: dict[str, list[dict[str, Any]]] = {
        status: [] for status in ("Draft", "Invited", "Collecting", "Evaluating", "Selected")
    }
    for row in events:
        status = str(row["business_status"])
        if status in lanes:
            lanes[status].append(row)
    return success({"lanes": lanes, "total": len(events)})


@frappe.whitelist(methods=["POST"])
@bff_endpoint("POST")
def create_from_demand(
    demand: str,
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(WRITE_ROLES)
    raw = {
        "demand": demand,
        "expected_revision": int(expected_revision),
        "idempotency_key": idempotency_key,
    }
    try:
        payload = validate_payload("sourcing.create_from_demand", raw)
    except DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error

    def execute() -> dict[str, Any]:
        demand_doc = frappe.get_doc("GBOS Demand Signal", demand)
        require_doc_permission(demand_doc, "read")
        try:
            next_revision(
                expected=payload["expected_revision"],
                current=int(demand_doc.revision),
            )
            validate_transition(
                "demand",
                demand_doc.business_status,
                "Sourcing",
            )
        except RevisionConflict as error:
            raise BFFError("revision_conflict", str(error), status=409) from error
        except InvalidTransition as error:
            raise BFFError("invalid_transition", str(error), status=409) from error

        event = frappe.get_doc(
            {
                "doctype": "GBOS Sourcing Event",
                "title": f"Sourcing: {demand_doc.title}",
                "team": demand_doc.team,
                "demand_signal": demand_doc.name,
                "owner_user": frappe.session.user,
                "origin": "Manual",
                "business_status": "Draft",
                "review_status": "Pending",
                "last_request_id": request_id(),
            }
        )
        require_doc_permission(event, "create")
        event.insert()
        demand_doc.business_status = "Sourcing"
        demand_doc.last_request_id = request_id()
        # Procurement users receive only a governed demand summary. The command
        # owns this one status transition; it does not grant general demand edits.
        demand_doc.save(ignore_permissions=True)
        return {
            "doctype": event.doctype,
            "name": event.name,
            "business_status": event.business_status,
            "revision": event.revision,
            "demand": demand_doc.name,
            "demand_status": demand_doc.business_status,
            "demand_revision": demand_doc.revision,
        }

    result, replayed, original_request_id = run_idempotent(
        "sourcing.create_from_demand",
        idempotency_key,
        payload,
        execute,
    )
    return success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )
