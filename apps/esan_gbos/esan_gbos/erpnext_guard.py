from __future__ import annotations

import frappe
from frappe import _
from frappe.exceptions import PermissionError


def reject_v1_transaction_creation(doc: object, method: str | None = None) -> None:
    del method
    doctype = getattr(doc, "doctype", "ERPNext transaction")
    frappe.throw(
        _("{0} creation is disabled in GBOS V1; Kingdee remains authoritative.").format(doctype),
        exc=PermissionError,
        title=_("GBOS V1 transaction guard"),
    )


def has_v1_transaction_permission(
    doc: object,
    user: str | None = None,
    permission_type: str | None = None,
) -> bool | None:
    del doc, user
    if permission_type in {"create", "submit", "cancel", "amend", "delete"}:
        return False
    return None
