from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
APP_ROOT = REPO_ROOT / "apps" / "esan_gbos"

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
DEFERRED_DOCTYPE_TERMS = {
    "Observation",
    "Extracted Fact",
    "Agent Run",
    "Model Invocation",
    "Approval Case",
    "Approval Step",
}
ROLE_NAMES = {
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
}
BFF_METHODS = {
    "party.get_360",
    "work_item.list",
    "sample.get_status",
    "sourcing.get_board",
    "sample.create_project",
    "sample.record_feedback",
    "sourcing.create_from_demand",
    "work_item.transition",
}
PWA_ROUTES = {
    "/gbos/ceo",
    "/gbos/sales",
    "/gbos/purchase",
    "/gbos/product",
    "/gbos/review",
    "/gbos/party/:id",
    "/gbos/sample/:id",
}


def _files_without_generated_dirs(
    root: Path,
    patterns: tuple[str, ...],
) -> list[Path]:
    return [
        path
        for pattern in patterns
        for path in root.glob(pattern)
        if path.is_file() and not {"node_modules", "dist", "coverage"}.intersection(path.parts)
    ]


def _doctype_jsons() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for path in APP_ROOT.glob("**/doctype/*/*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        name = payload.get("name")
        if isinstance(name, str):
            result[name] = payload
    return result


def _app_text() -> str:
    files = _files_without_generated_dirs(
        APP_ROOT,
        ("**/*.py", "**/*.json", "**/*.ts", "**/*.vue"),
    )
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def test_gate_one_contains_only_the_frozen_doctype_set() -> None:
    doctypes = _doctype_jsons()

    assert set(doctypes) >= PARENT_DOCTYPES | CHILD_DOCTYPES
    assert {
        name
        for name, payload in doctypes.items()
        if name.startswith("GBOS ") and not payload.get("custom")
    } == PARENT_DOCTYPES | CHILD_DOCTYPES
    assert all(not any(term in name for term in DEFERRED_DOCTYPE_TERMS) for name in doctypes)


def test_parent_and_child_doctype_shapes_are_explicit() -> None:
    doctypes = _doctype_jsons()

    for name in PARENT_DOCTYPES:
        payload = doctypes[name]
        assert not payload.get("istable"), name
        assert not payload.get("is_submittable"), name
        fields = {
            field["fieldname"]: field
            for field in payload.get("fields", [])
            if isinstance(field, dict) and "fieldname" in field
        }
        for fieldname in ("origin", "business_status", "review_status", "revision"):
            assert fieldname in fields, f"{name} is missing {fieldname}"
        if name != "GBOS Team":
            assert fields.get("team", {}).get("options") == "GBOS Team", name
        assert fields["review_status"].get("options") != fields["business_status"].get("options"), (
            name
        )

    for name in CHILD_DOCTYPES:
        assert _doctype_jsons()[name].get("istable"), name


def test_app_declares_roles_permission_hooks_and_frozen_bff_surface() -> None:
    text = _app_text()

    for role in ROLE_NAMES:
        assert role in text
    assert "permission_query_conditions" in text
    assert "has_permission" in text
    for method in BFF_METHODS:
        module, function = method.split(".")
        assert module in text and f"def {function}" in text


def test_frontend_declares_all_routes_without_generic_frappe_writers() -> None:
    frontend = APP_ROOT / "frontend"
    source_files = _files_without_generated_dirs(
        frontend,
        ("**/*.ts", "**/*.js", "**/*.vue"),
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)

    for route in PWA_ROUTES:
        assert route in source
    assert "frappe.client.insert" not in source
    assert "frappe.client.set_value" not in source


def test_service_worker_cannot_cache_business_api_responses() -> None:
    frontend = APP_ROOT / "frontend"
    service_worker_files = _files_without_generated_dirs(
        frontend,
        ("**/*service-worker*", "**/sw.*", "**/vite.config.*"),
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in service_worker_files)

    assert service_worker_files
    assert "/api/" in source
    assert "NetworkOnly" in source
    assert "CacheFirst" in source
