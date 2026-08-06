from __future__ import annotations

from pathlib import Path
from typing import Any

import frappe

from esan_gbos.domain.frontend_assets import load_vite_assets

no_cache = 1

_PWA_ROLES = {
    "GBOS Admin",
    "CEO",
    "Sales Manager",
    "Sales User",
    "Purchase Manager",
    "Buyer",
    "Product/R&D",
    "Reviewer",
}


def _manifest_path() -> Path:
    bench_root = Path(frappe.get_app_path("esan_gbos")).parents[2]
    return bench_root / "sites" / "assets" / "esan_gbos" / "frontend" / ".vite" / "manifest.json"


def get_context() -> dict[str, Any]:
    if frappe.session.user == "Guest":
        frappe.throw("请先登录后访问 ESAN GBOS。", frappe.PermissionError)

    roles = sorted(set(frappe.get_roles()))
    if not _PWA_ROLES.intersection(roles):
        frappe.throw("当前角色无权访问 ESAN GBOS。", frappe.PermissionError)

    assets = load_vite_assets(_manifest_path())
    return {
        "no_cache": 1,
        "title": "ESAN GBOS",
        "gbos_entry": assets.entry,
        "gbos_styles": assets.styles,
        "gbos_boot": {
            "user": frappe.session.user,
            "roles": roles,
            "csrf_token": frappe.sessions.get_csrf_token(),
        },
    }
