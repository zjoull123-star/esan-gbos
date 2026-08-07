from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
CLOSURE = ROOT / "docs" / "evidence" / "gate4-closure"
HISTORICAL_MANIFEST = ROOT / "docs" / "evidence" / "gate4" / "SHA256SUMS"
HISTORICAL_MANIFEST_SHA256 = "2df12cda3e442bbe68880e555583affe7e4f483096fd369e2c37bf34ef843b64"
COMPACT_FILES = {"gate4-closure.json", "gate4-closure-summary.md"}
CONTROL_KEYS = {
    "action_guard_fail_closed",
    "durable_idempotent_queue",
    "exact_context_decision_trace",
    "human_review_command_boundary",
    "responsive_accessible_review_pwa",
    "site_isolation_and_role_separation",
    "synthetic_ceo_prototype",
}
INVENTORY_COMMANDS = {
    "repository_pytest": ("uv run --frozen pytest --ignore=tests/acceptance/test_gate4_closure.py"),
    "gate4_postgres": "scripts/dev/test-gate4-integration",
    "python_static": (
        "uv run --frozen ruff check . && "
        "uv run --frozen ruff format --check . && "
        "uv run --frozen mypy apps/esan_gbos/esan_gbos/domain "
        "services/observer/contract_check.py services/observer/observer "
        "services/context/context_service services/action_guard "
        "services/agent_runtime services/metrics services/kingdee_adapter "
        "fixtures/gate1/generate.py fixtures/kingdee/gate1/mock.py "
        "fixtures/kingdee/gate2/adapter.py"
    ),
    "frontend_unit_build": (
        "corepack pnpm --dir apps/esan_gbos/frontend install --frozen-lockfile && "
        "corepack pnpm --dir apps/esan_gbos/frontend run lint && "
        "corepack pnpm --dir apps/esan_gbos/frontend run typecheck && "
        "corepack pnpm --dir apps/esan_gbos/frontend run test:unit && "
        "corepack pnpm --dir apps/esan_gbos/frontend run build"
    ),
    "frontend_e2e": "corepack pnpm --dir apps/esan_gbos/frontend run test:e2e",
    "secret_scan": "scripts/dev/secret-scan",
    "closure_acceptance": ("uv run --frozen pytest tests/acceptance/test_gate4_closure.py -q"),
    "closure_checksum": ("(cd docs/evidence/gate4-closure && shasum -a 256 -c SHA256SUMS)"),
}


def _evidence() -> dict[str, Any]:
    value = json.loads((CLOSURE / "gate4-closure.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_gate4_closure_is_bound_to_the_four_profile_local_prototype() -> None:
    evidence = _evidence()

    assert evidence["gate"] == 4
    assert evidence["closure_id"] == "gate4-synthetic-ceo-agent"
    assert evidence["status"] == "technical_local_go"
    assert re.fullmatch(r"[0-9a-f]{40}", evidence["implementation_commit"])
    assert evidence["agent_profiles"] == ["sales", "purchase", "product", "ceo"]
    assert evidence["durable_queue_contract_types"] == [
        "sales",
        "purchase",
        "product_sample",
        "ceo",
    ]
    assert evidence["profile_queue_naming"] == {
        "product_profile_kind": "product",
        "agent_task_contract_type": "product_sample",
    }
    assert evidence["ceo_prototype"] == {
        "processing_purpose": "metric_reporting",
        "action_type": "internal.ai_draft.propose",
        "source_mode": "synthetic_agent_context",
        "synthetic": True,
        "is_official_metric": False,
        "is_official_forecast": False,
        "requires_human_review": True,
    }


def test_gate4_closure_preserves_governance_and_no_go_boundaries() -> None:
    evidence = _evidence()

    assert evidence["required_boundaries"] == {
        "action_guard": True,
        "evidence_fact_decision_lineage": True,
        "human_review": True,
        "durable_runtime": True,
    }
    assert evidence["go_no_go"] == {
        "gate4_technical_local": "go",
        "real_model": "no_go",
        "kingdee_live": "blocked_external_input",
        "cloud": "no_go",
        "production": "no_go",
    }
    assert evidence["external_activity"] == {
        "network_calls": 0,
        "model_api_calls": 0,
        "tool_calls": 0,
        "external_messages": 0,
        "kingdee_calls": 0,
        "kingdee_mutations": 0,
        "formal_business_writes": 0,
        "cloud_deployments": 0,
        "production_credentials_loaded": 0,
    }
    assert all(value == 0 for value in evidence["external_activity"].values())
    assert evidence["human_review_semantics"] == {
        "requires_human_review_is_metadata_only": True,
        "review_case_created": False,
        "approved_command_issued": False,
    }


def test_gate4_closure_is_source_and_history_bound() -> None:
    evidence = _evidence()

    implementation_commit = evidence["implementation_commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", implementation_commit)
    subprocess.run(
        ["git", "cat-file", "-e", f"{implementation_commit}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", implementation_commit, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    committed_source = subprocess.run(
        [
            "git",
            "show",
            f"{implementation_commit}:services/agent_runtime/agents.py",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    recorded_source_sha256 = evidence["implementation_source_sha256"]
    assert recorded_source_sha256 == hashlib.sha256(committed_source).hexdigest()
    assert evidence["runtime_boundaries"] == {
        "durable_worker_to_orchestrator_dispatcher_present": False,
        "product_profile_to_product_sample_queue_mapping_present": False,
    }
    assert evidence["historical_evidence"] == {
        "path": "docs/evidence/gate4",
        "sha256sum_manifest_sha256": HISTORICAL_MANIFEST_SHA256,
        "modified": False,
    }
    assert hashlib.sha256(HISTORICAL_MANIFEST.read_bytes()).hexdigest() == (
        HISTORICAL_MANIFEST_SHA256
    )


def test_gate4_closure_controls_reference_existing_assets() -> None:
    evidence = _evidence()

    assert set(evidence["control_results"]) == CONTROL_KEYS
    for control in evidence["control_results"].values():
        assert control["status"] == "pass"
        assert control["test_refs"]
        assert control["evidence_refs"]
        for test_ref in control["test_refs"]:
            assert (ROOT / test_ref.split("::", 1)[0]).is_file()
        for evidence_ref in control["evidence_refs"]:
            assert (ROOT / evidence_ref).is_file()


def test_gate4_closure_inventory_has_exact_commands_and_zero_failures() -> None:
    evidence = _evidence()
    inventory = {item["id"]: item for item in evidence["test_inventory"]}

    assert set(inventory) == set(INVENTORY_COMMANDS)
    for inventory_id, command in INVENTORY_COMMANDS.items():
        item = inventory[inventory_id]
        assert item["command"] == command
        assert item["status"] == "pass"
        assert item["failed"] == 0
        assert isinstance(item["passed"], int) and item["passed"] >= 0
        assert isinstance(item["skipped"], int) and item["skipped"] >= 0


def test_gate4_closure_checksum_manifest_covers_only_compact_files() -> None:
    entries = (CLOSURE / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = CLOSURE / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == COMPACT_FILES


def test_ceo_route_remains_the_gate5_metrics_cockpit_only() -> None:
    frontend_source = ROOT / "apps" / "esan_gbos" / "frontend" / "src"
    workspace = (frontend_source / "views" / "WorkspaceView.vue").read_text(encoding="utf-8")
    all_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(frontend_source.rglob("*"))
        if path.is_file()
    )

    assert 'workspace === "ceo"' in workspace
    assert "client.getMetricDashboard()" in workspace
    assert "<MetricCockpit" in workspace
    assert 'gate: "Gate 5"' in workspace
    assert "getCeoAgent" not in all_source
    assert "CEO-Agent" not in all_source


def test_gate4_closure_summary_states_the_narrow_nonproduction_boundary() -> None:
    summary = (CLOSURE / "gate4-closure-summary.md").read_text(encoding="utf-8").casefold()

    for phrase in (
        "missing synthetic ceo prototype",
        "gate 5 governed cockpit",
        "metrics api",
        "requires_human_review",
        "metadata",
        "review case",
        "approvedcommand",
        "no-go",
    ):
        assert phrase in summary
