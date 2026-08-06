from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parents[2]
EVIDENCE_ROOT = REPO_ROOT / "docs" / "evidence"
GATE2_DIR = EVIDENCE_ROOT / "gate2"
HISTORICAL_MANIFEST_DIGEST = "a6a86c5dcb39d5d57b27e3cf7b444f71700bd74db362a74af6b2816186982cea"
GATE2_COMPACT_FILES = {
    "contract-validation.json",
    "gate2-evidence.json",
    "gate2-summary.md",
    "security-review.json",
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_historical_gate0_gate1_manifest_is_unchanged() -> None:
    manifest = EVIDENCE_ROOT / "SHA256SUMS"

    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == HISTORICAL_MANIFEST_DIGEST


def test_gate2_evidence_is_design_only_and_records_reproducible_checks() -> None:
    evidence = _json(GATE2_DIR / "gate2-evidence.json")

    assert evidence["scope"] == "design-schema-mock-gate2"
    assert evidence["status"] in {"partial", "pass"}
    assert evidence["evidence_level"] == "design-and-mock-only"
    assert evidence["implementation_commit"]["value"]
    assert evidence["implementation_commit"]["status"] in {"placeholder", "final"}

    inventory = evidence["test_inventory"]
    assert isinstance(inventory, list) and inventory
    inventory_paths = {item["path"] for item in inventory}
    assert {
        "tests/governance/test_gate2_design.py",
        "tests/acceptance/test_gate2_evidence.py",
        "tests/contracts",
        "tests/fixtures/test_gate2_kingdee.py",
    } <= inventory_paths
    for item in inventory:
        assert item["command"]
        assert item["status"] in {"not_run", "pass", "fail"}

    zero_external = evidence["zero_external_activity"]
    assert zero_external["network"]["status"] in {"not_run", "pass"}
    assert zero_external["network"]["observed_calls"] == 0
    assert zero_external["credentials"]["status"] in {"not_run", "pass"}
    assert zero_external["credentials"]["loaded"] == 0
    assert zero_external["method"]
    assert zero_external["command"]

    history = evidence["historical_evidence"]
    assert history["manifest_sha256"] == HISTORICAL_MANIFEST_DIGEST
    assert history["verification_command"]
    assert history["modified"] is False

    assert evidence["known_limits"]
    assert evidence["go_no_go"]["gate2"] in {
        "conditional-go-after-finalization",
        "go-for-gate3-implementation-only",
    }
    assert evidence["go_no_go"]["runtime"] == "no-go"
    assert evidence["go_no_go"]["production"] == "no-go"


def test_real_gate2_capabilities_remain_not_started_or_not_applicable() -> None:
    evidence = _json(GATE2_DIR / "gate2-evidence.json")
    capabilities = evidence["real_capabilities"]

    expected = {
        "connector",
        "model",
        "channel",
        "kingdee",
        "cloud",
        "runtime",
        "production",
    }
    assert set(capabilities) == expected
    for capability in capabilities.values():
        assert capability["status"] in {"not_started", "not_applicable"}
        assert capability["evidence"]
        assert capability["next_gate"] in {3, 4, 5, 6}


def test_contract_validation_is_machine_readable_and_network_free() -> None:
    validation = _json(GATE2_DIR / "contract-validation.json")

    assert validation["scope"] == "gate2-contract-schema-and-synthetic-examples"
    assert validation["status"] in {"not_run", "pass", "fail"}
    assert validation["json_schema_draft"] == "2020-12"
    assert validation["network_allowed"] is False
    assert validation["credentials_allowed"] is False
    assert validation["commands"]
    assert validation["results"]
    assert all(result["status"] in {"not_run", "pass", "fail"} for result in validation["results"])
    assert validation["limitations"]


def test_security_review_requires_structured_disposition_and_human_review() -> None:
    review = _json(GATE2_DIR / "security-review.json")

    assert review["scope"] == "gate2-design-schema-mock"
    assert review["overall_status"] in {"pending_human_review", "accepted_for_gate2"}
    assert review["runtime_security_claim"] == "not_applicable"
    assert review["risks"]

    valid_severities = {"critical", "high", "medium", "low"}
    valid_statuses = {
        "controlled_by_disabled_capability",
        "design_verified",
        "deferred_to_later_gate",
        "open",
    }
    for risk in review["risks"]:
        assert risk["risk_id"]
        assert risk["severity"] in valid_severities
        assert risk["owner"]
        assert risk["status"] in valid_statuses
        assert risk["test_refs"]
        assert risk["evidence_refs"]
        assert isinstance(risk["human_review"], dict)
        assert risk["human_review"]["status"] in {"pending", "approved", "rejected"}
        assert risk["human_review"]["reviewer_role"]

        for test_ref in risk["test_refs"]:
            test_path = test_ref.split("::", 1)[0]
            assert (REPO_ROOT / test_path).is_file()
        for evidence_ref in risk["evidence_refs"]:
            assert (REPO_ROOT / evidence_ref).is_file()

    for risk in review["risks"]:
        if risk["severity"] in {"critical", "high"}:
            assert risk["status"] != "open"
            assert risk["test_refs"]
            assert risk["evidence_refs"]


def test_gate2_local_checksum_manifest_covers_only_compact_gate2_files() -> None:
    manifest = GATE2_DIR / "SHA256SUMS"
    entries = manifest.read_text(encoding="utf-8").splitlines()

    assert entries
    assert all("../" not in entry for entry in entries)
    assert all("gate0" not in entry.lower() and "gate1" not in entry.lower() for entry in entries)

    covered: set[str] = set()
    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative
        path = GATE2_DIR / relative
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)

    assert covered == GATE2_COMPACT_FILES


def test_gate2_summary_limits_go_to_gate3_implementation() -> None:
    summary = (GATE2_DIR / "gate2-summary.md").read_text(encoding="utf-8")

    assert "Gate 3 implementation" in summary
    assert "does not authorize" in summary
    for capability in (
        "connector",
        "model",
        "channel",
        "Kingdee",
        "cloud",
        "runtime",
        "production",
    ):
        assert capability.lower() in summary.lower()
