from __future__ import annotations

import json
import tomllib
from pathlib import Path

APP_ROOT = Path(__file__).parents[2] / "apps" / "esan_gbos"
PACKAGE_ROOT = APP_ROOT / "esan_gbos"
DOCTYPE_ROOT = PACKAGE_ROOT / "gbos" / "doctype"

PARENT_DOCTYPES = {
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
}
CHILD_DOCTYPES = {"GBOS Team Member", "GBOS Sourcing Candidate"}
GATE4_AUDIT_DOCTYPES = {"GBOS Review Decision"}
COMMON_FIELDS = {"origin", "business_status", "review_status", "revision"}


def _doctype_documents() -> dict[str, dict[str, object]]:
    documents = {}
    for path in DOCTYPE_ROOT.glob("*/*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        documents[str(data["name"])] = data
    return documents


def test_standard_frappe_app_skeleton_exists() -> None:
    required = (
        APP_ROOT / "pyproject.toml",
        PACKAGE_ROOT / "__init__.py",
        PACKAGE_ROOT / "hooks.py",
        PACKAGE_ROOT / "modules.txt",
        PACKAGE_ROOT / "patches.txt",
    )

    assert all(path.is_file() for path in required)


def test_custom_app_license_metadata_matches_the_shipped_agpl_text() -> None:
    project = tomllib.loads((APP_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    hooks = (PACKAGE_ROOT / "hooks.py").read_text(encoding="utf-8")
    license_text = (APP_ROOT / "license.txt").read_text(encoding="utf-8")

    assert project["project"]["license"] == "AGPL-3.0-only"
    assert project["project"]["license-files"] == ["license.txt"]
    assert 'app_license = "AGPL-3.0"' in hooks
    assert "GNU Affero General Public License v3.0" in license_text


def test_gate_one_doctypes_remain_frozen_and_gate_four_adds_only_review_audit() -> None:
    assert set(_doctype_documents()) == (PARENT_DOCTYPES | CHILD_DOCTYPES | GATE4_AUDIT_DOCTYPES)


def test_every_parent_has_governed_fields_and_is_not_submittable() -> None:
    documents = _doctype_documents()

    for name in PARENT_DOCTYPES:
        document = documents[name]
        fields = {field["fieldname"] for field in document["fields"]}
        assert fields >= COMMON_FIELDS, name
        if name != "GBOS Team":
            assert "team" in fields, name
        assert not document.get("is_submittable"), name


def test_children_are_tables_without_parent_governance_fields() -> None:
    documents = _doctype_documents()

    for name in CHILD_DOCTYPES:
        document = documents[name]
        fields = {field["fieldname"] for field in document["fields"]}
        assert document["istable"] == 1
        assert not (COMMON_FIELDS & fields)


def test_crm_extensions_are_prefixed_and_do_not_reference_crm_contact() -> None:
    fixture = json.loads(
        (PACKAGE_ROOT / "fixtures" / "custom_field.json").read_text(encoding="utf-8")
    )

    assert fixture
    assert all(row["fieldname"].startswith("custom_esan_") for row in fixture)
    assert all(row.get("options") != "CRM Contact" for row in fixture)


def test_product_brief_links_forward_to_crm_deal() -> None:
    fields = {row["fieldname"]: row for row in _doctype_documents()["GBOS Product Brief"]["fields"]}

    assert fields["deal"]["fieldtype"] == "Link"
    assert fields["deal"]["options"] == "CRM Deal"


def test_hooks_enforce_server_side_permissions_and_transaction_guards() -> None:
    hooks = (PACKAGE_ROOT / "hooks.py").read_text(encoding="utf-8")

    assert "esan_gbos.api.v1.http.normalize_bff_pre_dispatch_error" in hooks
    assert "esan_gbos.security.add_gbos_pwa_security_headers" in hooks
    assert "permission_query_conditions" in hooks
    assert "has_permission" in hooks
    assert '"GBOS Review Case": "esan_gbos.permissions.review_case_permission_query"' in hooks
    assert '"GBOS Work Item": "esan_gbos.permissions.work_item_permission_query"' in hooks
    assert '"CRM Organization": "esan_gbos.permissions.crm_organization_permission_query"' in hooks
    assert '"CRM Deal": "esan_gbos.permissions.crm_deal_permission_query"' in hooks
    assert '"CRM Lead": "esan_gbos.permissions.crm_lead_permission_query"' in hooks
    assert '"Contact": "esan_gbos.permissions.contact_permission_query"' in hooks
    assert '"Sales Order"' in hooks
    assert '"Purchase Order"' in hooks
    assert '"Sales Invoice"' in hooks
    assert '"Purchase Invoice"' in hooks
    assert '"Payment Entry"' in hooks
    assert '"Stock Entry"' in hooks
    assert '"GL Entry"' in hooks


def test_gbos_pwa_security_headers_are_scoped_and_deny_unsafe_defaults() -> None:
    source = (PACKAGE_ROOT / "security.py").read_text(encoding="utf-8")

    assert 'request_path == "/gbos" or request_path.startswith("/gbos/")' in source
    assert '"Content-Security-Policy"' in source
    assert '"Permissions-Policy"' in source
    assert "\"default-src 'self'\"" in source
    assert "\"object-src 'none'\"" in source
    assert "\"base-uri 'self'\"" in source
    assert "\"frame-ancestors 'self'\"" in source
    assert "\"script-src 'self'\"" in source
    assert "\"connect-src 'self' ws: wss:\"" in source
    assert '"camera=(), microphone=(), geolocation=()"' in source
    assert "upgrade-insecure-requests" not in source


def test_hooks_route_every_gbos_spa_path_to_the_authenticated_shell() -> None:
    hooks = (PACKAGE_ROOT / "hooks.py").read_text(encoding="utf-8")

    assert '"from_route": "/gbos/<path:app_path>"' in hooks
    assert '"to_route": "gbos"' in hooks


def test_gbos_shell_uses_manifest_assets_and_non_executable_bootstrap_json() -> None:
    controller = (PACKAGE_ROOT / "www" / "gbos.py").read_text(encoding="utf-8")
    template = (PACKAGE_ROOT / "www" / "gbos.html").read_text(encoding="utf-8")

    assert "frappe.sessions.get_csrf_token()" in controller
    assert "frappe.session.user" in controller
    assert "frappe.get_roles()" in controller
    assert 'id="gbos-bootstrap"' in template
    assert 'type="application/json"' in template
    assert "gbos_entry" in template
    assert "gbos_styles" in template
    assert "/assets/esan_gbos/frontend/registerSW.js" in template


def test_bff_dispatch_reaches_the_uniform_auth_and_method_guard() -> None:
    api_root = PACKAGE_ROOT / "api" / "v1"
    endpoint_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            api_root / "party.py",
            api_root / "sample.py",
            api_root / "sourcing.py",
            api_root / "work_item.py",
        )
    )

    dispatch_decorator = '@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])'
    assert endpoint_sources.count(dispatch_decorator) == 8
    assert endpoint_sources.count('@bff_endpoint("GET")') == 4
    assert endpoint_sources.count('@bff_endpoint("POST")') == 4
