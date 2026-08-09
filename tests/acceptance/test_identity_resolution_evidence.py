from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
EVIDENCE = ROOT / "docs" / "evidence" / "identity-resolution"
COMPACT_FILES = {
    "identity-resolution-evidence.json",
    "identity-resolution-summary.md",
}


def _evidence() -> dict[str, Any]:
    value = json.loads((EVIDENCE / "identity-resolution-evidence.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_identity_resolution_evidence_binds_source_and_relation_invariants() -> None:
    evidence = _evidence()
    validation_commit = evidence["validation_reference_commit"]

    assert evidence["status"] == "offline_identity_resolution_technical_go"
    assert re.fullmatch(r"[0-9a-f]{40}", validation_commit)
    subprocess.run(
        ["git", "cat-file", "-e", f"{validation_commit}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", validation_commit, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assert evidence["identity_relations"] == {
        "team_access": "Observation.team_ref <-> GBOS Team Member.user",
        "connector_account_owner": "Connector Instance.account_user_ref",
        "communication_participant": "Participant.identity_ref",
        "business_assignment": "Deal owner / owner_user / assigned_to",
        "cross_relation_inference_allowed": False,
    }


def test_identity_resolution_evidence_records_fresh_verification_and_no_go() -> None:
    evidence = _evidence()
    verification = evidence["verification"]

    assert verification["backend"] == {
        "passed": 2486,
        "skipped": 39,
        "failed": 0,
        "warnings": 1,
    }
    assert verification["postgres_gate3"] == {
        "passed": 14,
        "skipped": 0,
        "failed": 0,
        "warnings": 1,
        "migrations_applied_twice": True,
        "temporary_environment_removed": True,
    }
    assert verification["frontend_unit"] == {
        "passed": 187,
        "failed": 0,
    }
    assert verification["frontend_playwright"] == {
        "passed": 22,
        "failed": 0,
    }
    assert verification["python_static"] == {
        "ruff_check": "pass",
        "ruff_format_files": 480,
        "mypy_service_files": 121,
        "compileall": "pass",
        "secret_scan": "pass",
    }
    assert evidence["go_no_go"] == {
        "offline_identity_resolution": "go",
        "formal_local_pilot": "no_go",
        "real_email": "no_go",
        "real_deepseek": "no_go",
        "observed_model_identity": "unknown",
        "native_frappe_site": "not_run",
        "prometheus_live_scrape": "not_run",
        "seventy_two_hour_pilot": "not_run",
        "kingdee": "no_go",
        "cloud": "no_go",
        "production": "no_go",
        "external_send": "no_go",
    }
    assert all(value == 0 for value in evidence["external_activity"].values())


def test_identity_resolution_evidence_checksum_covers_only_compact_files() -> None:
    entries = (EVIDENCE / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = EVIDENCE / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == COMPACT_FILES
