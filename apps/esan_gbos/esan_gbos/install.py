from __future__ import annotations

import frappe

from esan_gbos.domain.permissions import (
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
)
CRM_DOCTYPES = ("CRM Organization", "Contact", "CRM Lead", "CRM Deal")


def after_install() -> None:
    ensure_roles()
    ensure_permissions()
    ensure_crm_permissions()
    ensure_unique_indexes()
    frappe.clear_cache()


def after_migrate() -> None:
    ensure_roles()
    ensure_permissions()
    ensure_crm_permissions()
    ensure_unique_indexes()
    frappe.clear_cache()


def ensure_roles() -> None:
    for role_name in GBOS_ROLES:
        if frappe.db.exists("Role", role_name):
            continue
        frappe.get_doc(
            {
                "doctype": "Role",
                "role_name": role_name,
                "desk_access": 1,
                "is_custom": 0,
            }
        ).insert(ignore_permissions=True)


def _permission_values(doctype: str, role: str) -> dict[str, int | str]:
    can_read = role_has_doctype_permission(role, doctype, "read")
    can_write = role_has_doctype_permission(role, doctype, "write")
    can_create = role_has_doctype_permission(role, doctype, "create")
    can_delete = role_has_doctype_permission(role, doctype, "delete")
    return {
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
