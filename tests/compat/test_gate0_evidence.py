from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence"


def test_gate0_summary_separates_verified_upstream_from_pending_gate1() -> None:
    summary = (EVIDENCE_DIR / "gate0-summary.md").read_text(encoding="utf-8")

    assert "Verified local Gate 0 upstream evidence" in summary
    assert "Pending Gate 1 evidence" in summary
    assert "sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec" in summary
    assert "duckdb_sync" in summary
    assert "transient network hang" in summary
    assert "GitHub Pro" in summary
    assert "make the repository public" not in summary
    assert "final four-app runtime" in summary


def test_gate0_machine_evidence_has_explicit_scope_and_status() -> None:
    evidence = json.loads((EVIDENCE_DIR / "gate0-evidence.json").read_text(encoding="utf-8"))

    assert evidence["scope"] == "local-disposable-upstream-only"
    assert evidence["status"] == "partial"
    assert evidence["verified"]["installed_apps"] == ["frappe", "erpnext", "crm"]
    assert evidence["verified"]["migration_exit_codes"] == [0, 0]
    assert evidence["pending"]["final_installed_apps"] == [
        "frappe",
        "erpnext",
        "crm",
        "esan_gbos",
    ]
    assert evidence["pending"]["gate"] == "Gate 1"


def test_gate0_checksum_manifest_matches_small_evidence_files() -> None:
    checksum_file = EVIDENCE_DIR / "SHA256SUMS"
    entries = checksum_file.read_text(encoding="utf-8").splitlines()

    assert entries
    for entry in entries:
        expected, relative = entry.split("  ", 1)
        path = EVIDENCE_DIR / relative
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def test_evidence_template_requires_reproducibility_and_limits() -> None:
    template = (EVIDENCE_DIR / "gate-evidence-template.md").read_text(encoding="utf-8")

    for heading in (
        "Environment",
        "Immutable inputs",
        "Command",
        "Result",
        "Checksums",
        "Interpretation",
        "Limitations and pending evidence",
    ):
        assert f"## {heading}" in template
