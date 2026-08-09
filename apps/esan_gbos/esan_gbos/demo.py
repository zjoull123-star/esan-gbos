from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import frappe

_INSERT_ORDER = (
    "User",
    "GBOS Team",
    "CRM Organization",
    "Contact",
    "CRM Lead",
    "CRM Deal",
    "GBOS Party Profile",
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
_BLOCKED_DOCTYPES = frozenset(
    {
        "Sales Order",
        "Purchase Order",
        "Sales Invoice",
        "Purchase Invoice",
        "Stock Entry",
        "GL Entry",
    }
)
_TRUE_VALUES = frozenset({"1", "true", "yes"})


def _fixture_directory() -> Path:
    return Path(frappe.get_app_path("esan_gbos")).resolve().parents[2] / "fixtures" / "gate1"


def _load_fixture_payload(
    fixture_directory: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = fixture_directory / "manifest.json"
    payload_path = fixture_directory / "frappe_payload.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload_bytes = payload_path.read_bytes()
    if hashlib.sha256(payload_bytes).hexdigest() != manifest["frappe_payload_sha256"]:
        raise frappe.ValidationError("Gate 1 fixture payload checksum mismatch")
    payload = json.loads(payload_bytes)
    if (
        manifest.get("dataset") != "gate1"
        or manifest.get("synthetic") is not True
        or manifest.get("demo") is not True
        or not isinstance(payload, list)
    ):
        raise frappe.ValidationError("Gate 1 fixture manifest is not synthetic")
    observed = Counter(str(item.get("doctype")) for item in payload)
    if dict(observed) != manifest["record_counts"]:
        raise frappe.ValidationError("Gate 1 fixture record counts do not match")
    unknown = set(observed) - set(_INSERT_ORDER)
    if unknown or set(observed) & _BLOCKED_DOCTYPES:
        raise frappe.ValidationError(
            f"Gate 1 fixture contains unsupported DocTypes: {sorted(unknown)}"
        )
    return manifest, payload


def _assert_existing_record_is_synthetic(
    doctype: str,
    name: str,
) -> None:
    if doctype.startswith("GBOS "):
        if frappe.db.get_value(doctype, name, "origin") != "Fixture":
            raise frappe.ValidationError(f"Refusing to reuse non-fixture {doctype} {name}")
        return
    if doctype == "CRM Organization":
        if (
            frappe.db.get_value(
                doctype,
                name,
                "custom_esan_origin",
            )
            != "Fixture"
        ):
            raise frappe.ValidationError(f"Refusing to reuse non-fixture {doctype} {name}")
        return
    if doctype == "User":
        if not name.endswith("@example.invalid"):
            raise frappe.ValidationError(f"Refusing to reuse non-synthetic User {name}")
        return
    if not name.startswith("CRM-"):
        raise frappe.ValidationError(f"Refusing to reuse non-synthetic {doctype} {name}")


def _insert_fixture(
    payload: dict[str, Any],
    *,
    demo_password: str,
) -> tuple[bool, tuple[str, str] | None]:
    values = dict(payload)
    doctype = str(values["doctype"])
    name = str(values.pop("name"))
    deferred_party: tuple[str, str] | None = None
    if doctype == "CRM Deal":
        party_profile = values.pop("custom_esan_party_profile", None)
        if party_profile:
            deferred_party = (name, str(party_profile))
    if frappe.db.exists(doctype, name):
        _assert_existing_record_is_synthetic(doctype, name)
        return False, deferred_party

    doc = frappe.get_doc(values)
    if doctype.startswith("GBOS "):
        doc.flags.gbos_fixture_seed = True
    if doctype == "User" and demo_password:
        doc.new_password = demo_password
    doc.insert(ignore_permissions=True, set_name=name)
    return True, deferred_party


def seed(
    confirm_synthetic: bool = False,
    fixture_directory: str | None = None,
) -> dict[str, Any]:
    """Load the deterministic Gate 1 demo dataset from the final image.

    This bench-only helper is deliberately not whitelisted as an HTTP method.
    It never deletes or truncates data and refuses to run in production mode.
    """

    if confirm_synthetic is not True:
        raise frappe.ValidationError("Pass confirm_synthetic=true to load Gate 1 demo fixtures")
    if os.environ.get("GBOS_PRODUCTION_ENABLED", "").strip().lower() in _TRUE_VALUES:
        raise frappe.ValidationError("Synthetic Gate 1 fixtures are disabled in production")
    demo_password = os.environ.get("GBOS_DEMO_PASSWORD", "")
    if demo_password and len(demo_password) < 12:
        raise frappe.ValidationError("GBOS_DEMO_PASSWORD must contain at least 12 characters")

    directory = Path(fixture_directory).resolve() if fixture_directory else _fixture_directory()
    manifest, payload = _load_fixture_payload(directory)
    by_doctype: dict[str, list[dict[str, Any]]] = {doctype: [] for doctype in _INSERT_ORDER}
    for item in payload:
        by_doctype[str(item["doctype"])].append(item)

    inserted: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    deferred_deal_parties: list[tuple[str, str]] = []
    try:
        for doctype in _INSERT_ORDER:
            for item in by_doctype[doctype]:
                was_inserted, deferred = _insert_fixture(
                    item,
                    demo_password=demo_password,
                )
                (inserted if was_inserted else skipped)[doctype] += 1
                if deferred:
                    deferred_deal_parties.append(deferred)
        for deal, party_profile in deferred_deal_parties:
            current = frappe.db.get_value(
                "CRM Deal",
                deal,
                "custom_esan_party_profile",
            )
            if current not in (None, "", party_profile):
                raise frappe.ValidationError(f"Refusing to overwrite CRM Deal fixture link {deal}")
            if current != party_profile:
                frappe.db.set_value(
                    "CRM Deal",
                    deal,
                    "custom_esan_party_profile",
                    party_profile,
                    update_modified=False,
                )
        frappe.db.set_default("setup_complete", 1)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        raise

    return {
        "dataset": manifest["dataset"],
        "synthetic": True,
        "site": frappe.local.site,
        "inserted": dict(inserted),
        "skipped": dict(skipped),
    }
