from __future__ import annotations

import ast
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
    "GBOS Informal Observation",
}
CHILD_DOCTYPES = {
    "GBOS Team Member",
    "GBOS Sourcing Candidate",
    "GBOS Informal Evidence Ref",
}
GATE4_AUDIT_DOCTYPES = {"GBOS Review Decision"}
COMMON_FIELDS = {"origin", "business_status", "review_status", "revision"}


def _doctype_documents() -> dict[str, dict[str, object]]:
    documents = {}
    for path in DOCTYPE_ROOT.glob("*/*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        documents[str(data["name"])] = data
    return documents


def _literal_assignment(path: Path, name: str) -> object:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a literal assignment in {path}")


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
        required = (
            COMMON_FIELDS - {"business_status"}
            if name == "GBOS Informal Observation"
            else COMMON_FIELDS
        )
        assert fields >= required, name
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


def test_hooks_and_migrations_auto_elevate_every_ceo_user() -> None:
    hooks = (PACKAGE_ROOT / "hooks.py").read_text(encoding="utf-8")
    install = (PACKAGE_ROOT / "install.py").read_text(encoding="utf-8")

    assert 'doc_events["User"]' in hooks
    assert "esan_gbos.ceo_access.ensure_ceo_full_access" in hooks
    assert install.count("backfill_ceo_full_access()") == 2


def test_internal_materializer_role_install_fixture_and_hooks_are_consistent() -> None:
    hooks_path = PACKAGE_ROOT / "hooks.py"
    install_path = PACKAGE_ROOT / "install.py"
    fixture = json.loads((PACKAGE_ROOT / "fixtures" / "role.json").read_text(encoding="utf-8"))
    hook_roles = set(_literal_assignment(hooks_path, "GBOS_ROLES"))
    install_roles = set(_literal_assignment(install_path, "GBOS_ROLES"))
    fixture_roles = {row["role_name"]: row for row in fixture}

    assert hook_roles == install_roles == set(fixture_roles)
    assert fixture_roles["Agent TrustedMaterializer"]["desk_access"] == 0
    assert fixture_roles["Observer Identity Resolver"]["desk_access"] == 0
    assert "GBOS Informal Observation" in set(_literal_assignment(install_path, "PARENT_DOCTYPES"))
    assert _literal_assignment(
        install_path,
        "INTERNAL_MATERIALIZATION_AUDIT_DOCTYPES",
    ) == ("Integration Request",)
    hooks_source = hooks_path.read_text(encoding="utf-8")
    assert 'has_permission["Integration Request"]' in hooks_source
    assert "esan_gbos.permissions.has_internal_materialization_permission" in hooks_source
    assert 'permission_query_conditions["Integration Request"]' in hooks_source
    assert "esan_gbos.permissions.integration_request_permission_query" in hooks_source
    install_source = install_path.read_text(encoding="utf-8")
    assert install_source.count("ensure_internal_materialization_audit_permissions()") == 3


def test_observer_identity_resolver_is_install_managed_and_has_no_desk_or_docperm_rights() -> None:
    install_path = PACKAGE_ROOT / "install.py"
    install_source = install_path.read_text(encoding="utf-8")
    install_roles = set(_literal_assignment(install_path, "GBOS_ROLES"))

    assert "Observer Identity Resolver" in install_roles
    assert "IDENTITY_RESOLVER_ROLE" in install_source
    assert "role_name not in {INTERNAL_MATERIALIZER_ROLE, IDENTITY_RESOLVER_ROLE}" in install_source
    assert "role in {INTERNAL_MATERIALIZER_ROLE, IDENTITY_RESOLVER_ROLE}" in install_source


def test_internal_materializer_doctype_permissions_are_exactly_the_coarse_minimum() -> None:
    expected = {
        "GBOS Team": {"read"},
        "GBOS Demand Signal": {"read"},
        "GBOS Party Profile": {"read"},
        "GBOS Product Brief": {"read"},
        "GBOS Sample Feedback": {"read"},
        "GBOS Sample Iteration": {"read"},
        "GBOS Sample Project": {"read"},
        "GBOS Sample Shipment": {"read"},
        "GBOS Sourcing Event": {"read"},
        "GBOS Work Item": {"read", "write", "create"},
        "GBOS Review Case": {"write", "create"},
        "GBOS Informal Observation": {"create"},
    }
    documents = _doctype_documents()

    for doctype, allowed in expected.items():
        service_rows = [
            row
            for row in documents[doctype]["permissions"]
            if row["role"] == "Agent TrustedMaterializer"
        ]
        assert len(service_rows) == 1, doctype
        granted = {
            permission_type
            for permission_type, enabled in service_rows[0].items()
            if permission_type != "role" and enabled == 1
        }
        assert granted == allowed, doctype


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
    shell_roles = set(_literal_assignment(PACKAGE_ROOT / "www" / "gbos.py", "_PWA_ROLES"))

    assert "frappe.sessions.get_csrf_token()" in controller
    assert "frappe.session.user" in controller
    assert "frappe.get_roles()" in controller
    assert "Integration Admin" in shell_roles
    assert "Finance Readonly" not in shell_roles
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


def test_v4_dotted_endpoints_match_the_ten_frozen_operations() -> None:
    api_root = PACKAGE_ROOT / "api" / "v4"
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            api_root / "integration.py",
            api_root / "communication.py",
            api_root / "model.py",
            api_root / "ai_draft.py",
        )
    }

    assert sources["integration.py"].count("@frappe.whitelist") == 4
    assert sources["integration.py"].count('@bff_endpoint("POST")') == 3
    assert sources["integration.py"].count('@bff_endpoint("GET")') == 1
    assert sources["communication.py"].count('@bff_endpoint("GET")') == 2
    assert sources["model.py"].count('@bff_endpoint("GET")') == 1
    assert sources["ai_draft.py"].count('@bff_endpoint("GET")') == 2
    assert sources["ai_draft.py"].count('@bff_endpoint("POST")') == 1


def test_v4_informal_observation_contains_no_raw_or_official_metric_surface() -> None:
    document = _doctype_documents()["GBOS Informal Observation"]
    fields = {field["fieldname"]: field for field in document["fields"]}

    assert {
        "subject",
        "summary_zh",
        "team",
        "evidence_refs",
        "model_name",
        "model_version",
        "is_official_metric",
        "origin",
        "origin_reference",
        "review_status",
        "revision",
        "last_request_id",
    } <= set(fields)
    assert fields["is_official_metric"]["default"] == "0"
    assert fields["is_official_metric"]["read_only"] == 1
    assert not {
        "raw",
        "original_text",
        "prompt",
        "response",
        "business_status",
        "metric_value",
    } & set(fields)


def test_all_versioned_bff_responses_are_no_store_and_v4_csrf_is_normalized() -> None:
    source = (PACKAGE_ROOT / "api" / "v1" / "http.py").read_text(encoding="utf-8")

    assert '"/api/method/esan_gbos.api.v4."' in source
    assert 'response.headers["Cache-Control"] = "no-store"' in source
