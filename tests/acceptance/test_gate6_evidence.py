from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parents[2]
GATE6 = ROOT / "docs" / "evidence" / "gate6"
IMPLEMENTATION_COMMIT = "dd46e5393b714c09fb5b902d4d09f5ba9e05d3cb"
IMAGE_DIGEST = "sha256:3b103472b2057ca365ff62e71efa02932fabef4151aba67768ab001ac79dd6f8"
APP_SOURCE_SHA256 = "37cd2cfc8860fc5934b3c12c3f328fea16c73b3ad7508029f2229fc8442e9093"
COMPACT_FILES = {
    "gate6-evidence.json",
    "gate6-summary.md",
    "release-decision.json",
    "release-manifest-status.json",
    "runtime-validation.json",
    "security-review.json",
}


def _json(name: str) -> dict[str, Any]:
    value = json.loads((GATE6 / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_gate6_is_local_technical_go_and_production_no_go() -> None:
    evidence = _json("gate6-evidence.json")

    assert evidence["gate"] == 6
    assert evidence["status"] == "technical_local_go"
    assert evidence["production_status"] == "no_go"
    assert evidence["implementation_commit"] == IMPLEMENTATION_COMMIT
    assert evidence["production_mutation_authorized"] is False
    assert evidence["production_approvers"] == []

    summary = (GATE6 / "gate6-summary.md").read_text(encoding="utf-8").lower()
    for phrase in (
        "technical local go",
        "production no-go",
        "blocked_external_input",
        "kingdee",
        "singapore",
        "uat",
    ):
        assert phrase in summary


def test_gate6_external_activity_is_zero() -> None:
    evidence = _json("gate6-evidence.json")

    assert evidence["external_activity"]
    assert all(value == 0 for value in evidence["external_activity"].values())
    assert evidence["live_capabilities"] == {
        "kingdee": False,
        "cloud": False,
        "production_channels": False,
        "real_ai_models": False,
        "external_sends": False,
    }


def test_gate6_runtime_uses_exact_image_and_frozen_toolchain() -> None:
    runtime = _json("runtime-validation.json")

    assert runtime["status"] == "pass"
    assert runtime["implementation_commit"] == IMPLEMENTATION_COMMIT
    assert runtime["final_image"] == {
        "platform": "linux/arm64",
        "digest": IMAGE_DIGEST,
        "revision_label": IMPLEMENTATION_COMMIT,
        "app_source_sha256_label": APP_SOURCE_SHA256,
        "runtime_git_present": False,
        "runtime_curl_present": False,
    }
    assert runtime["toolchain"]["pnpm"] == "11.9.0"
    assert runtime["frontend"]["unit_tests_passed"] == 77
    assert runtime["frontend"]["playwright_tests_passed"] == 6
    assert runtime["http_auth_boundary"]["guest_metrics_status"] == 401


def test_gate6_fresh_install_migrations_and_restore_have_parity() -> None:
    runtime = _json("runtime-validation.json")
    restore = runtime["backup_restore"]

    assert runtime["fresh_site"]["apps"] == ["frappe", "erpnext", "crm", "esan_gbos"]
    assert runtime["fresh_site"]["migration_runs"] == 2
    assert restore["frappe"]["source_doctype_count"] == 871
    assert restore["frappe"]["restored_doctype_count"] == 871
    assert restore["frappe"]["apps_match"] is True
    assert restore["postgres"]["source_table_count"] == 19
    assert restore["postgres"]["restored_table_count"] == 19
    assert restore["postgres"]["source_migration_count"] == 9
    assert restore["postgres"]["restored_migration_count"] == 9


def test_gate6_security_evidence_keeps_human_gate_open() -> None:
    review = _json("security-review.json")

    assert review["overall_status"] == "accepted_for_local_controls_only"
    assert review["formal_security_owner_review"] == "pending_external_review"
    assert review["scanner"]["exact_image"] == IMAGE_DIGEST
    assert review["scanner"]["repository_high_or_critical"] == 0
    assert review["scanner"]["image_unwaived_high_or_critical"] == 0
    assert review["scanner"]["local_only_waiver_entries"] == 57
    assert review["scanner"]["local_only_waiver_purls"] == 103
    assert review["scanner"]["production_effect"].startswith("blocked")
    assert review["gitleaks"]["history_commits_scanned"] == 43
    assert review["gitleaks"]["leaks_found"] == 0


def test_gate6_production_manifest_was_not_fabricated() -> None:
    status = _json("release-manifest-status.json")

    assert status["status"] == "not_issued"
    assert status["reason_code"] == "blocked_external_input"
    assert status["source_commit"] == IMPLEMENTATION_COMMIT
    assert status["image_digest"] == IMAGE_DIGEST
    assert status["production_mutation_authorized"] is False
    assert status["approvals"] == []
    assert set(status["missing_entry_inputs"]) == {
        "live_kingdee_canary",
        "singapore_preproduction",
        "security_owner_approval",
        "privacy_cross_border_approval",
        "business_owner_uat",
        "production_authorization",
    }


def test_gate6_decision_is_schema_valid_and_matches_manifest_status() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "gate6" / "gate6-decision.schema.json").read_text(encoding="utf-8")
    )
    decision = _json("release-decision.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(decision)

    manifest_status = GATE6 / "release-manifest-status.json"
    assert decision["release_candidate"]["source_commit"] == IMPLEMENTATION_COMMIT
    assert decision["release_candidate"]["image_digest"] == IMAGE_DIGEST
    assert (
        decision["release_candidate"]["release_manifest_sha256"]
        == hashlib.sha256(manifest_status.read_bytes()).hexdigest()
    )
    assert decision["decision"] == {
        "technical_local": "go",
        "production": "no_go",
        "reason_code": "blocked_external_input",
    }
    assert decision["production_mutation_authorized"] is False


def test_gate6_control_references_resolve_to_committed_assets() -> None:
    evidence = _json("gate6-evidence.json")

    for test_run in evidence["test_inventory"]:
        if test_run["path"].startswith("tests/"):
            assert (ROOT / test_run["path"]).exists()
    for control in evidence["control_results"].values():
        assert control["status"] == "pass"
        for test_ref in control["test_refs"]:
            assert (ROOT / test_ref.split("::", 1)[0]).exists()
        for evidence_ref in control["evidence_refs"]:
            assert (ROOT / evidence_ref).exists()


def test_gate6_checksum_manifest_covers_compact_files() -> None:
    entries = (GATE6 / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = GATE6 / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == COMPACT_FILES
