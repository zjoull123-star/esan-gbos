from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_runs_gate6_release_privacy_operations_and_decision_controls() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "gate6-controls:" in workflow
    for expected in (
        "tests/contracts/test_gate6_decision_contract.py",
        "tests/release",
        "tests/ops",
        "tests/governance/test_gate6_privacy_contracts.py",
        "tests/governance/test_gate6_privacy_procedures.py",
        "scripts/ops/gate6_ops.py validate-policy",
        "contracts/gate6/release-manifest.schema.json",
        "infra/prod/single-tenant-v1.json",
        "scripts/release/preflight",
        "scripts/release/plan",
        "gate-log-gate6-controls",
    ):
        assert expected in workflow


def test_ci_keeps_gate6_tools_inert_and_uploads_only_sanitized_control_logs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate6_job = workflow.split("  gate6-controls:\n", 1)[1]

    assert "--offline" in gate6_job
    assert "bash -n" in gate6_job
    assert "release-manifest.example" not in gate6_job
    assert "deploy" not in gate6_job.lower()
    assert "production credentials" not in gate6_job.lower()
    assert "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f" in gate6_job


def test_gate6_prepares_locked_python_and_dependencies_before_offline_commands() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate6_job = workflow.split("  gate6-controls:\n", 1)[1]

    install_index = gate6_job.index("uv python install 3.14.2")
    sync_index = gate6_job.index("uv sync --frozen")
    first_offline_index = gate6_job.index("uv run --offline")

    assert install_index < sync_index < first_offline_index


def test_frappe_vue_playwright_fetches_full_history_for_gate4_closure() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    frappe_job = workflow.split("  frappe-vue-playwright:\n", 1)[1].split("\n  gitleaks:\n", 1)[0]
    checkout_step = frappe_job.split(
        "      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", 1
    )[1].split("\n      - ", 1)[0]

    assert "fetch-depth: 0" in checkout_step
