from __future__ import annotations

import frappe

from esan_gbos.ceo_access import backfill_ceo_full_access
from esan_gbos.domain.permissions import (
    IDENTITY_RESOLVER_ROLE,
    INTERNAL_MATERIALIZER_ROLE,
    role_has_crm_doctype_permission,
    role_has_doctype_permission,
)

GBOS_ROLES = (
    "GBOS Admin",
    "Integration Admin",
    "Privacy/Audit",
    "CEO",
    "Sales Manager",
    "Sales User",
    "Purchase Manager",
    "Buyer",
    "Product/R&D",
    "Reviewer",
    "Finance Readonly",
    "Agent TrustedMaterializer",
    "Observer Identity Resolver",
)

PARENT_DOCTYPES = (
    "GBOS Team",
    "GBOS Party Profile",
    "GBOS External Identity",
    "GBOS External Crosswalk",
    "GBOS Product Brief",
    "GBOS Sample Project",
    "GBOS Sample Iteration",
    "GBOS Sample Shipment",
    "GBOS Sample Feedback",
    "GBOS Demand Signal",
    "GBOS Sourcing Event",
    "GBOS Work Item",
    "GBOS Review Case",
    "GBOS Informal Observation",
)
CRM_DOCTYPES = ("CRM Organization", "Contact", "CRM Lead", "CRM Deal")
INTERNAL_MATERIALIZATION_AUDIT_DOCTYPES = ("Integration Request",)


def after_install() -> None:
    ensure_roles()
    backfill_ceo_full_access()
    ensure_permissions()
    ensure_internal_materialization_audit_permissions()
    ensure_crm_permissions()
    ensure_unique_indexes()
    frappe.clear_cache()


def after_migrate() -> None:
    ensure_roles()
    backfill_ceo_full_access()
    ensure_permissions()
    ensure_internal_materialization_audit_permissions()
    ensure_crm_permissions()
    ensure_unique_indexes()
    frappe.clear_cache()


def ensure_roles() -> None:
    for role_name in GBOS_ROLES:
        desk_access = int(role_name not in {INTERNAL_MATERIALIZER_ROLE, IDENTITY_RESOLVER_ROLE})
        if frappe.db.exists("Role", role_name):
            if int(frappe.db.get_value("Role", role_name, "desk_access") or 0) != desk_access:
                frappe.db.set_value("Role", role_name, "desk_access", desk_access)
            continue
        frappe.get_doc(
            {
                "doctype": "Role",
                "role_name": role_name,
                "desk_access": desk_access,
                "is_custom": 0,
            }
        ).insert(ignore_permissions=True)


def _permission_values(doctype: str, role: str) -> dict[str, int | str]:
    can_read = role_has_doctype_permission(role, doctype, "read")
    can_write = role_has_doctype_permission(role, doctype, "write")
    can_create = role_has_doctype_permission(role, doctype, "create")
    can_delete = role_has_doctype_permission(role, doctype, "delete")
    internal_service = role in {INTERNAL_MATERIALIZER_ROLE, IDENTITY_RESOLVER_ROLE}
    return {
        "read": int(can_read),
        "write": int(can_write),
        "create": int(can_create),
        "delete": int(can_delete),
        "report": int(can_read and not internal_service),
        "export": int(can_read and role in {"GBOS Admin", "CEO"}),
        "print": int(can_read and not internal_service),
        "email": 0,
        "share": 0,
    }


def ensure_permissions() -> None:
    for doctype in PARENT_DOCTYPES:
        for role in GBOS_ROLES:
            filters = {
                "parent": doctype,
                "role": role,
                "permlevel": 0,
                "if_owner": 0,
            }
            values = _permission_values(doctype, role)
            name = frappe.db.get_value("Custom DocPerm", filters, "name")
            if name:
                frappe.db.set_value("Custom DocPerm", name, values)
                continue
            frappe.get_doc(
                {
                    "doctype": "Custom DocPerm",
                    "parent": doctype,
                    "parenttype": "DocType",
                    "parentfield": "permissions",
                    "role": role,
                    "permlevel": 0,
                    **values,
                }
            ).insert(ignore_permissions=True)


def ensure_internal_materialization_audit_permissions() -> None:
    values = {
        "read": 1,
        "write": 1,
        "create": 1,
        "delete": 0,
        "report": 0,
        "export": 0,
        "print": 0,
        "email": 0,
        "share": 0,
    }
    for doctype in INTERNAL_MATERIALIZATION_AUDIT_DOCTYPES:
        filters = {
            "parent": doctype,
            "role": INTERNAL_MATERIALIZER_ROLE,
            "permlevel": 0,
            "if_owner": 0,
        }
        name = frappe.db.get_value("Custom DocPerm", filters, "name")
        if name:
            frappe.db.set_value("Custom DocPerm", name, values)
            continue
        frappe.get_doc(
            {
                "doctype": "Custom DocPerm",
                "parent": doctype,
                "parenttype": "DocType",
                "parentfield": "permissions",
                "role": INTERNAL_MATERIALIZER_ROLE,
                "permlevel": 0,
                **values,
            }
        ).insert(ignore_permissions=True)


def ensure_crm_permissions() -> None:
    crm_roles = (
        "GBOS Admin",
        "CEO",
        "Sales Manager",
        "Sales User",
        "Product/R&D",
    )
    for doctype in CRM_DOCTYPES:
        for role in crm_roles:
            can_read = role_has_crm_doctype_permission(role, doctype, "read")
            can_write = role_has_crm_doctype_permission(role, doctype, "write")
            can_create = role_has_crm_doctype_permission(role, doctype, "create")
            can_delete = role_has_crm_doctype_permission(role, doctype, "delete")
            values = {
                "read": int(can_read),
                "write": int(can_write),
                "create": int(can_create),
                "delete": int(can_delete),
                "report": int(can_read),
                "export": int(can_read and role in {"GBOS Admin", "CEO"}),
                "print": int(can_read),
                "email": 0,
                "share": 0,
            }
            filters = {
                "parent": doctype,
                "role": role,
                "permlevel": 0,
                "if_owner": 0,
            }
            name = frappe.db.get_value("Custom DocPerm", filters, "name")
            if name:
                frappe.db.set_value("Custom DocPerm", name, values)
                continue
            frappe.get_doc(
                {
                    "doctype": "Custom DocPerm",
                    "parent": doctype,
                    "parenttype": "DocType",
                    "parentfield": "permissions",
                    "role": role,
                    "permlevel": 0,
                    **values,
                }
            ).insert(ignore_permissions=True)


def ensure_unique_indexes() -> None:
    frappe.db.add_unique(
        "GBOS External Crosswalk",
        ["external_system", "account_set", "object_type", "external_id"],
        constraint_name="uniq_gbos_external_crosswalk",
    )
    frappe.db.add_unique(
        "GBOS External Identity",
        ["identity_provider", "external_subject"],
        constraint_name="uniq_gbos_external_identity",
    )
