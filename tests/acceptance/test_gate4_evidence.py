from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
GATE4 = ROOT / "docs" / "evidence" / "gate4"
COMPACT_FILES = {
    "gate4-evidence.json",
    "gate4-summary.md",
    "runtime-validation.json",
    "security-review.json",
}


def _json(name: str) -> dict[str, Any]:
    value = json.loads((GATE4 / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_gate4_evidence_is_narrow_local_and_synthetic() -> None:
    evidence = _json("gate4-evidence.json")

    assert evidence["gate"] == 4
    assert evidence["status"] == "pass"
    assert evidence["evidence_level"] == "local-runtime-synthetic-only"
    assert len(evidence["implementation_commit"]["value"]) == 40
    assert evidence["capability_boundary"]["human_confirmation_required"] is True
    assert evidence["capability_boundary"]["direct_business_writer"] is False
    assert evidence["capability_boundary"]["external_send"] is False
    assert evidence["capability_boundary"]["kingdee_write"] is False


def test_gate4_controls_reference_real_tests_and_evidence() -> None:
    evidence = _json("gate4-evidence.json")

    for control in evidence["control_results"].values():
        assert control["status"] == "pass"
        for test_ref in control["test_refs"]:
            assert (ROOT / test_ref.split("::", 1)[0]).exists()
        for evidence_ref in control["evidence_refs"]:
            assert (ROOT / evidence_ref).is_file()


def test_gate4_external_and_release_boundaries_remain_closed() -> None:
    evidence = _json("gate4-evidence.json")

    assert all(
        value == 0 for key, value in evidence["zero_external_activity"].items() if key != "method"
    )
    assert evidence["capability_ledger"]["kingdee_read"] == "deferred_to_gate5"
    assert evidence["capability_ledger"]["kingdee_write"] == "not_available"
    assert evidence["go_no_go"] == {
        "gate4_technical_local": "go",
        "gate5_local_start": "go",
        "real_model": "no_go",
        "kingdee_live": "blocked_external_input",
        "cloud": "no_go",
        "production": "no_go",
    }

    summary = (GATE4 / "gate4-summary.md").read_text(encoding="utf-8").lower()
    for phrase in ("kingdee", "cloud", "production", "blocked_external_input", "no-go"):
        assert phrase in summary


def test_gate4_runtime_uses_exact_image_and_non_privileged_roles() -> None:
    runtime = _json("runtime-validation.json")

    assert runtime["status"] == "pass"
    assert runtime["final_image"]["digest"].startswith("sha256:")
    assert runtime["final_image"]["revision_label"] == runtime["implementation_commit"]
    assert runtime["postgres"]["forced_rls_tables_in_agent_and_context_schemas"] >= 3
    assert all(
        role["superuser"] is False and role["bypass_rls"] is False
        for role in runtime["postgres"]["roles"].values()
    )
    assert runtime["frappe_runtime"]["integration_tests"]["failed"] == 0
    assert runtime["frontend"]["sensitive_api_offline_cache"] is False


def test_gate4_security_review_keeps_human_gate_open() -> None:
    review = _json("security-review.json")

    assert review["overall_status"] == "accepted_for_local_synthetic_gate5_work"
    assert review["human_review"]["status"] == "pending"
    assert review["risks"]
    for risk in review["risks"]:
        assert risk["status"] in {"runtime_verified", "controlled_by_disabled_capability"}
        if risk["severity"] in {"critical", "high"}:
            assert risk["status"] != "open"


def test_gate4_checksum_manifest_covers_compact_files() -> None:
    entries = (GATE4 / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = GATE4 / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == COMPACT_FILES
