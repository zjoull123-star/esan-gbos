from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.common import (
    bff_endpoint,
    require_doc_permission,
    require_roles,
    success,
)
from esan_gbos.domain.access_policy import PARTY_360_ROLES

READ_ROLES = PARTY_360_ROLES


def _linked(
    doctype: str,
    name: str | None,
    team: str | None,
    fields: list[str],
) -> dict[str, Any] | None:
    if not name or not team:
        return None
    value = frappe.db.get_value(
        doctype,
        name,
        [*fields, "custom_esan_team"],
        as_dict=True,
    )
    if not value or value.get("custom_esan_team") != team:
        return None
    try:
        can_read = frappe.has_permission(doctype, ptype="read", doc=name)
    except frappe.DoesNotExistError:
        return None
    except frappe.PermissionError:
        return None
    if not can_read:
        return None
    return {field: value.get(field) for field in fields}


@frappe.whitelist(methods=["GET"])
@bff_endpoint("GET")
def get_360(party: str) -> dict[str, Any]:
    """Return the fixed, permission-scoped customer 360 DTO."""
    require_roles(READ_ROLES)
    profile = frappe.get_doc("GBOS Party Profile", party)
    require_doc_permission(profile, "read")
    data = {
        "profile": {
            "name": profile.name,
            "party_name": profile.party_name,
            "team": profile.team,
            "origin": profile.origin,
            "business_status": profile.business_status,
            "review_status": profile.review_status,
            "revision": profile.revision,
            "modified": profile.modified,
        },
        "organization": _linked(
            "CRM Organization",
            profile.crm_organization,
            profile.team,
            ["name", "organization_name", "website", "territory", "industry"],
        ),
        "contact": _linked(
            "Contact",
            profile.contact,
            profile.team,
            ["name", "full_name", "email_id", "mobile_no"],
        ),
        "lead": _linked(
            "CRM Lead",
            profile.crm_lead,
            profile.team,
            ["name", "lead_name", "organization", "status", "lead_owner"],
        ),
        "deal": _linked(
            "CRM Deal",
            profile.crm_deal,
            profile.team,
            ["name", "organization", "status", "deal_owner", "expected_deal_value"],
        ),
        "product_briefs": frappe.get_list(
            "GBOS Product Brief",
            filters={"party_profile": party},
            fields=[
                "name",
                "title",
                "deal",
                "origin",
                "business_status",
                "review_status",
                "revision",
            ],
            order_by="modified desc",
            page_length=50,
        ),
        "samples": frappe.get_list(
            "GBOS Sample Project",
            filters={"party_profile": party},
            fields=[
                "name",
                "title",
                "origin",
                "business_status",
                "review_status",
                "revision",
            ],
            order_by="modified desc",
            page_length=50,
        ),
        "demands": frappe.get_list(
            "GBOS Demand Signal",
            filters={"party_profile": party},
            fields=[
                "name",
                "title",
                "origin",
                "business_status",
                "review_status",
                "revision",
            ],
            order_by="modified desc",
            page_length=50,
        ),
    }
    return success(data)
