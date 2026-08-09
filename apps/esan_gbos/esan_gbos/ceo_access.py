from __future__ import annotations

from typing import Any

import frappe

CEO_FULL_ACCESS_ROLES = (
    "CEO",
    "GBOS Admin",
    "Integration Admin",
    "Reviewer",
    "System Manager",
)


def _role_names(user: Any) -> set[str]:
    return {
        str(getattr(row, "role", "") or (row.get("role") if isinstance(row, dict) else ""))
        for row in (user.get("roles") or [])
    }


def ensure_ceo_full_access(user: Any, method: str | None = None) -> bool:
    """Promote every CEO User before Frappe validates its System User state."""
    del method
    assigned = _role_names(user)
    if "CEO" not in assigned:
        return False

    changed = False
    for role in CEO_FULL_ACCESS_ROLES:
        if role not in assigned:
            user.append("roles", {"role": role})
            assigned.add(role)
            changed = True
    if getattr(user, "user_type", None) != "System User":
        user.user_type = "System User"
        changed = True
    return changed


def backfill_ceo_full_access() -> int:
    """Idempotently promote CEO users that predate the User validation hook."""
    users = frappe.get_all(
        "Has Role",
        filters={"role": "CEO", "parenttype": "User", "parentfield": "roles"},
        pluck="parent",
    )
    updated = 0
    for name in sorted({str(value) for value in users if value}):
        user = frappe.get_doc("User", name)
        if ensure_ceo_full_access(user):
            user.save(ignore_permissions=True)
            updated += 1
    return updated
