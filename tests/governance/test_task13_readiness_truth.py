from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
EVIDENCE_DIR = ROOT / "docs" / "evidence" / "task13-readiness"
EVIDENCE = EVIDENCE_DIR / "task13-readiness-evidence.json"
SUMMARY = EVIDENCE_DIR / "task13-readiness-summary.md"
CHECKSUMS = EVIDENCE_DIR / "SHA256SUMS"
ROADMAP = ROOT / "docs" / "superpowers" / "plans" / ("2026-08-09-gbos-user-identity-resolution.md")
LOCAL_PLAN = ROOT / "docs" / "local-pilot" / "IMPLEMENTATION_PLAN.md"
HANDOFF = ROOT / "docs" / "HANDOFF.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_task13_readiness_historical_snapshot_is_closed_and_honest() -> None:
    payload = json.loads(_read(EVIDENCE))

    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "pre_canary_ready_external_inputs_blocked"
    assert payload["historical_evidence_modified"] is False
    assert payload["stability"] == {
        "continuous_runtime_required": False,
        "seventy_two_hour_run": "deferred_by_user",
    }
    # This directory is an immutable historical snapshot. A governed image
    # rebuild updates the current image lock and writes a new evidence package;
    # it must not make an older, checksum-bound snapshot follow mutable state.
    for image in payload["images"].values():
        assert len(image["source_revision"]) == 40
        assert image["image_id"].startswith("sha256:")
        assert len(image["image_id"]) == len("sha256:") + 64
    assert payload["formal_state"] == {
        "production_go": False,
        "local_pilot_go": False,
        "composition_status": "composed",
        "external_send": False,
    }
    assert payload["go_no_go"]["credential_free_readiness"] == "go"
    assert payload["go_no_go"]["real_email_deepseek_canary"] == "no_go"
    assert payload["go_no_go"]["observed_model_identity"] == "unknown"
    assert payload["external_activity"] == {
        "real_email_connections": 0,
        "real_model_api_calls": 0,
        "external_messages": 0,
        "kingdee_calls": 0,
        "cloud_deployments": 0,
        "production_writes": 0,
    }
    assert payload["verification"]["backend"] == {
        "passed": 2557,
        "skipped": 41,
        "failed": 0,
        "warnings": 1,
    }
    assert payload["verification"]["frontend"]["unit_passed"] == 188
    assert payload["verification"]["frontend"]["harness_playwright_passed"] == 22
    assert payload["verification"]["fresh_frappe"]["native_tests_passed"] == 12
    assert payload["verification"]["retention"]["dry_run"] == "pass_no_deletion"
    assert payload["verification"]["emergency_stop"]["containment_verified"] is True
    assert payload["verification"]["offline_fault_drills"]["verdict"] == "pass"
    assert payload["missing_external_inputs"] == [
        "email_credential",
        "deepseek_api_key",
        "identity_hmac_key",
        "trusted_phrase_lexicon",
        "frappe_identity_resolver_api_key",
        "frappe_identity_resolver_api_secret",
    ]

    commit = payload["validation_reference_commit"]
    assert len(commit) == 40
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0


def test_task13_readiness_documents_remove_72_hour_gate_and_stale_runtime_claims() -> None:
    roadmap = _read(ROADMAP)
    local_plan = _read(LOCAL_PLAN)
    handoff = _read(HANDOFF)
    summary = _read(SUMMARY)

    for text in (roadmap, local_plan, handoff, summary):
        assert "72 小时连续运行不再作为本阶段退出条件" in text
        assert "real_email_deepseek_canary=no_go" in text
    assert "Task 13 | **Credential-free implementation complete; external canary deferred**" in (
        roadmap
    )
    assert "The actual UI is a Frappe PWA and its local composition is not present" not in (
        local_plan
    )
    assert "task13-readiness-summary.md" in handoff
    assert "真实 Email + DeepSeek" in summary


def test_task13_readiness_checksums_cover_only_the_machine_evidence_and_summary() -> None:
    entries: dict[str, str] = {}
    for line in _read(CHECKSUMS).splitlines():
        digest, name = line.split("  ", maxsplit=1)
        entries[name] = digest

    assert set(entries) == {EVIDENCE.name, SUMMARY.name}
    for name, expected in entries.items():
        assert hashlib.sha256((EVIDENCE_DIR / name).read_bytes()).hexdigest() == expected
