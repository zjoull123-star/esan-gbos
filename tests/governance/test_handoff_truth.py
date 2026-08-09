from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
PERMISSION_MATRIX = ROOT / "docs" / "permission-matrix.md"
EXTERNAL_DEPS = ROOT / "docs" / "external-deps.md"
HANDOFF = ROOT / "docs" / "HANDOFF.md"
IDENTITY_PLAN = (
    ROOT / "docs" / "superpowers" / "plans" / "2026-08-09-gbos-user-identity-resolution.md"
)
DOCTYPE_ROOT = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "gbos" / "doctype"
CEO_ACCESS = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "ceo_access.py"
MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
ENTRYPOINTS = ROOT / "infra" / "local" / "runtime-entrypoints.json"
IMAGE_LOCK = ROOT / "infra" / "local" / "images.lock.json"

CEO_ROLES = (
    "CEO",
    "GBOS Admin",
    "Integration Admin",
    "Reviewer",
    "System Manager",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_reports_the_current_doctype_inventory_and_local_boundary() -> None:
    readme = _read(README)
    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in DOCTYPE_ROOT.glob("*/*.json")
    ]
    parent_count = sum(not document.get("istable") for document in documents)
    child_count = sum(bool(document.get("istable")) for document in documents)

    assert (parent_count, child_count) == (15, 3)
    assert f"{parent_count} 个父 DocType 与 {child_count} 个 Child DocType" in readme
    assert "13 个父 DocType 与 2 个 Child DocType" not in readme
    assert "local pilot" in readme.lower()
    assert "No-Go" in readme


def test_permission_matrix_records_the_closed_ceo_auto_elevation_bundle() -> None:
    matrix = _read(PERMISSION_MATRIX)
    ceo_source = _read(CEO_ACCESS)

    assert "CEO auto-elevation" in matrix
    assert "System User" in matrix
    assert "before_validate" in matrix
    assert "after_install" in matrix
    assert "after_migrate" in matrix
    for role in CEO_ROLES:
        assert f"`{role}`" in matrix
    ceo_section = matrix.split("CEO auto-elevation", 1)[1].split("Legend:", 1)[0]
    assert "Privacy/Audit" not in ceo_section
    assert "CEO_FULL_ACCESS_ROLES" in ceo_source
    for role in CEO_ROLES:
        assert role in _read(HANDOFF)


def test_external_dependency_truth_names_deepseek_without_claiming_a_real_call() -> None:
    dependencies = _read(EXTERNAL_DEPS)

    assert "DeepSeek gateway" in dependencies
    assert "deepseek-v4-flash" in dependencies
    assert "no real call" in dependencies.lower()
    assert "model identity" in dependencies.lower()
    assert "real provider not selected" not in dependencies.lower()
    assert "formal local pilot" in dependencies.lower()
    for capability in ("real channels", "real model", "Kingdee", "cloud", "production"):
        assert capability.lower() in dependencies.lower()


def test_handoff_binds_source_baseline_current_runtime_truth_and_historical_boundary() -> None:
    handoff = _read(HANDOFF)
    manifest = json.loads(_read(MANIFEST))
    entrypoints = json.loads(_read(ENTRYPOINTS))
    image_lock = json.loads(_read(IMAGE_LOCK))

    assert "8c40731" in handoff
    assert "feat/user-identity-resolution-20260810" in handoff
    assert "historical" in handoff.lower()
    assert "current main" in handoff.lower()
    assert "do not modify" in handoff.lower()
    assert "15 parent" in handoff
    assert "3 child" in handoff
    assert "CEO" in handoff and "System User" in handoff
    assert "DeepSeek gateway" in handoff
    assert "real call" in handoff.lower()
    assert "model identity" in handoff.lower()
    assert "No-Go" in handoff
    assert "not_composed" in handoff
    assert "local_pilot_go=false" in handoff
    assert "real channels" in handoff.lower()
    assert "Kingdee" in handoff
    assert "cloud" in handoff.lower()
    assert "production" in handoff.lower()

    assert f"production_go={str(manifest['production_go']).lower()}" in handoff
    assert f"local_pilot_go={str(manifest['local_pilot_go']).lower()}" in handoff
    assert entrypoints["composition"]["status"] in handoff

    locked_digests = {
        image["service"]: image["local_inspect_digest"]
        for image in image_lock["images"]
        if image["service"] in {"frappe-pwa", "local-runtime"}
    }
    for service, digest in locked_digests.items():
        assert digest in handoff, service


def test_owned_handoff_docs_do_not_reintroduce_stale_runtime_claims() -> None:
    owned_docs = "\n".join(
        _read(path) for path in (README, PERMISSION_MATRIX, EXTERNAL_DEPS, HANDOFF)
    )

    forbidden = (
        "13 个父 DocType 与 2 个 Child DocType",
        "real provider not selected",
        "local_pilot_go=true",
        "composition.status=go",
        "production_go=true",
        "real channels verified",
        "real model verified",
        "Kingdee live verified",
        "cloud deployment verified",
        "production deployed",
    )
    for statement in forbidden:
        assert statement.lower() not in owned_docs.lower(), statement


def test_identity_handoff_keeps_the_four_user_relations_separate_and_truthful() -> None:
    handoff = _read(HANDOFF)
    plan = _read(IDENTITY_PLAN)

    for relation in (
        "Observation.team_ref ↔ GBOS Team Member.user",
        "Connector Instance.account_user_ref",
        "Participant.identity_ref",
        "Deal owner / owner_user / assigned_to",
    ):
        assert relation in handoff
        assert relation in plan

    for document in (handoff, plan):
        assert "禁止相互推导" in document
        assert "c98f6a5" in document
        assert "Task 13" in document
        assert "未执行" in document
        assert "真实 Frappe" in document
        assert "Prometheus" in document
        assert "72 小时" in document
        assert "local_pilot_go=false" in document
