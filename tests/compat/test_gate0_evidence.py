from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
EVIDENCE_DIR = REPO_ROOT / "docs" / "evidence"


def test_gate0_summary_records_local_go_and_downstream_no_go() -> None:
    summary = (EVIDENCE_DIR / "gate0-summary.md").read_text(encoding="utf-8")

    assert "Status: **pass for the local disposable Gate 0 boundary**" in summary
    assert "Gate 0 Go" in summary
    assert "Gate 6 production remain **No-Go**" in summary
    assert "sha256:b69f0001225523ec52ceb6d80fc696c34f24c560a0d15c5ebc53e803eb5286ec" in summary
    assert "sha256:a55e3dc432cabc7e4a1bbe4951d1586c97e65151b41a5d9c7e5eb0632d61f1e9" in summary
    assert "duckdb_sync" in summary
    assert "transient network hang" in summary
    assert "GitHub Pro" in summary
    assert "make the repository public" not in summary


def test_gate0_machine_evidence_has_explicit_scope_and_status() -> None:
    evidence = json.loads((EVIDENCE_DIR / "gate0-evidence.json").read_text(encoding="utf-8"))

    assert evidence["scope"] == "local-disposable-gate0"
    assert evidence["status"] == "pass"
    assert evidence["go_no_go"]["gate0"] == "go"
    assert evidence["verified"]["upstream_image"]["installed_apps"] == [
        "frappe",
        "erpnext",
        "crm",
    ]
    assert evidence["verified"]["upstream_image"]["migration_exit_codes"] == [0, 0]
    assert evidence["verified"]["final_foundation_image"]["installed_apps"] == [
        "frappe",
        "erpnext",
        "crm",
        "esan_gbos",
    ]
    assert evidence["security"]["unwaived_high"] == 0
    assert evidence["security"]["unwaived_critical"] == 0


def test_gate1_evidence_records_the_verified_local_exit_conditions() -> None:
    evidence = json.loads((EVIDENCE_DIR / "gate1-evidence.json").read_text(encoding="utf-8"))
    summary = (EVIDENCE_DIR / "gate1-summary.md").read_text(encoding="utf-8")

    assert evidence["scope"] == "local-disposable-gate1"
    assert evidence["status"] == "pass"
    assert evidence["go_no_go"]["gate1"] == "go-after-green-head-ci"
    assert evidence["immutable_runtime"]["digest"] == (
        "sha256:a55e3dc432cabc7e4a1bbe4951d1586c97e65151b41a5d9c7e5eb0632d61f1e9"
    )
    assert evidence["immutable_runtime"]["installed_apps"] == [
        "frappe",
        "erpnext",
        "crm",
        "esan_gbos",
    ]
    assert evidence["tests"]["repository_pytest"] == 200
    assert evidence["tests"]["frappe_integration"] == 21
    assert evidence["tests"]["frontend_unit"] == 56
    assert evidence["tests"]["live_role_pages"] == 5
    assert all(
        latency < evidence["query_p95_ms"]["threshold"]
        for endpoint, latency in evidence["query_p95_ms"].items()
        if endpoint != "threshold"
    )
    for doctype in ("Sales Order", "Purchase Order", "Stock Entry", "GL Entry"):
        assert evidence["fixture_counts_after_http_smoke"][doctype] == 0
    assert all(count == 0 for count in evidence["external_call_counts"].values())
    assert evidence["backup_restore"]["status"] == "pass"
    assert evidence["backup_restore"]["post_restore_counts_match"] is True

    assert "Gate 1 Go" in summary
    assert "Gates 2–6" in summary
    assert "duckdb_sync.cleanup_old_syncs" in summary
    assert "PR #1" in summary
    normalized_summary = " ".join(summary.split())
    assert (
        "Pull requests build an ephemeral `linux/amd64` image for the fresh-site "
        "smoke without pushing it." in normalized_summary
    )
    assert "No registry publication or production deployment was authorized." in normalized_summary


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
