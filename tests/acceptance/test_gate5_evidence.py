from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
GATE5 = ROOT / "docs" / "evidence" / "gate5"
COMPACT_FILES = {
    "gate5-evidence.json",
    "gate5-summary.md",
    "runtime-validation.json",
    "security-review.json",
}


def _json(name: str) -> dict[str, Any]:
    value = json.loads((GATE5 / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_gate5_evidence_is_local_synthetic_and_read_only() -> None:
    evidence = _json("gate5-evidence.json")

    assert evidence["gate"] == 5
    assert evidence["status"] == "technical_local_go"
    assert evidence["evidence_level"] == "local-runtime-synthetic-only"
    assert len(evidence["implementation_commit"]["value"]) == 40
    assert evidence["capability_boundary"]["kingdee_writer_tools"] == 0
    assert evidence["capability_boundary"]["live_kingdee_enabled"] is False
    assert evidence["capability_boundary"]["arbitrary_query_allowed"] is False


def test_gate5_controls_reference_real_tests_and_evidence() -> None:
    evidence = _json("gate5-evidence.json")

    for control in evidence["control_results"].values():
        assert control["status"] == "pass"
        for test_ref in control["test_refs"]:
            assert (ROOT / test_ref.split("::", 1)[0]).exists()
        for evidence_ref in control["evidence_refs"]:
            assert (ROOT / evidence_ref).is_file()


def test_gate5_external_entry_gates_are_not_promoted_by_synthetic_evidence() -> None:
    evidence = _json("gate5-evidence.json")

    assert all(
        value == 0 for key, value in evidence["zero_external_activity"].items() if key != "method"
    )
    assert evidence["entry_gate_status"] == {
        "local_technical_readiness": "go",
        "live_kingdee_canary": "blocked_external_input",
        "singapore_preproduction": "blocked_external_input",
        "security_owner_review": "pending_external_review",
        "privacy_and_cross_border_review": "blocked_external_input",
        "business_owner_uat": "blocked_external_input",
    }
    assert evidence["go_no_go"]["production"] == "no_go"

    summary = (GATE5 / "gate5-summary.md").read_text(encoding="utf-8").lower()
    for phrase in (
        "synthetic",
        "blocked_external_input",
        "security owner",
        "production",
        "no-go",
    ):
        assert phrase in summary


def test_gate5_runtime_uses_exact_hardened_image_and_forced_rls() -> None:
    runtime = _json("runtime-validation.json")

    assert runtime["status"] == "pass"
    assert runtime["final_image"]["digest"].startswith("sha256:")
    assert runtime["final_image"]["revision_label"] == runtime["implementation_commit"]
    assert runtime["final_image"]["runtime_git_present"] is False
    assert runtime["final_image"]["runtime_curl_present"] is False
    assert runtime["postgres"]["forced_rls_tables_in_metrics_schema"] == 4
    assert all(
        role["superuser"] is False and role["bypass_rls"] is False
        for role in runtime["postgres"]["roles"].values()
    )
    assert runtime["metrics_api"]["source_modes"] == ["synthetic"]
    assert runtime["browser"]["console_errors"] == 0
    assert runtime["browser"]["horizontal_overflow"] is False


def test_gate5_security_review_keeps_production_and_human_gates_open() -> None:
    review = _json("security-review.json")

    assert review["overall_status"] == "accepted_for_local_gate6_implementation"
    assert review["human_review"]["status"] == "pending"
    assert review["scanner"]["unwaived_high_or_critical"] == 0
    assert review["scanner"]["local_only_waiver_entries"] == 57
    assert review["scanner"]["production_effect"].startswith("blocked")
    assert review["risks"]
    assert any(risk["status"] == "production_blocker" for risk in review["risks"])


def test_gate5_checksum_manifest_covers_compact_files() -> None:
    entries = (GATE5 / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = GATE5 / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == COMPACT_FILES
