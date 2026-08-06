from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parents[2]
EVIDENCE_ROOT = REPO_ROOT / "docs" / "evidence"
GATE3_DIR = EVIDENCE_ROOT / "gate3"
HISTORICAL_GATE01_MANIFEST = "a6a86c5dcb39d5d57b27e3cf7b444f71700bd74db362a74af6b2816186982cea"
HISTORICAL_GATE2_MANIFEST = "3c7ae8b498ab81903a13e379feba0648a9e4e6b2b41da290f65792b9e02e7428"
GATE3_COMPACT_FILES = {
    "gate3-evidence.json",
    "gate3-summary.md",
    "observer-validation.json",
    "privacy-review.json",
    "security-review.json",
}


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_historical_gate_manifests_are_unchanged() -> None:
    assert (
        hashlib.sha256((EVIDENCE_ROOT / "SHA256SUMS").read_bytes()).hexdigest()
        == HISTORICAL_GATE01_MANIFEST
    )
    assert (
        hashlib.sha256((EVIDENCE_ROOT / "gate2" / "SHA256SUMS").read_bytes()).hexdigest()
        == HISTORICAL_GATE2_MANIFEST
    )


def test_gate3_evidence_is_local_synthetic_runtime_only() -> None:
    evidence = _json(GATE3_DIR / "gate3-evidence.json")

    assert evidence["gate"] == 3
    assert evidence["scope"] == "local-synthetic-fixture-manual-import-only"
    assert evidence["status"] == "pass"
    assert evidence["evidence_level"] == "local-runtime-synthetic-only"
    assert evidence["implementation_commit"]["status"] == "final"
    assert len(evidence["implementation_commit"]["value"]) == 40
    assert evidence["environment"]["kind"] == "local_disposable"
    assert evidence["environment"]["architecture"] == "arm64"
    assert evidence["environment"]["image_digests"]["postgres_pgvector"].startswith(
        "pgvector/pgvector@sha256:"
    )

    inventory = evidence["test_inventory"]
    paths = {item["path"] for item in inventory}
    assert {
        "tests/contracts",
        "tests/observer",
        "tests/context",
        "tests/integration/test_gate3_postgres.py",
        "tests/acceptance/test_gate3_evidence.py",
        "tests",
    } <= paths
    assert all(item["command"] and item["status"] == "pass" for item in inventory)
    assert all(item["failed"] == 0 for item in inventory)


def test_gate3_control_matrix_has_reproducible_runtime_evidence() -> None:
    evidence = _json(GATE3_DIR / "gate3-evidence.json")
    required = {
        "service_identity_signature",
        "idempotency_replay_ordering",
        "site_purpose_consent",
        "object_integrity_immutability",
        "locator_replay",
        "retention_deletion_legal_hold",
        "upload_safety",
        "log_redaction",
        "tool_free_processing",
        "context_publication",
        "postgres_rls_backup_restore",
    }
    controls = evidence["control_results"]
    assert set(controls) == required
    for control in controls.values():
        assert control["status"] == "pass"
        assert control["owner"]
        assert control["test_refs"]
        assert control["evidence_refs"]
        for test_ref in control["test_refs"]:
            assert (REPO_ROOT / test_ref.split("::", 1)[0]).is_file()
        for evidence_ref in control["evidence_refs"]:
            assert (REPO_ROOT / evidence_ref).is_file()


def test_gate3_zero_external_activity_and_later_capabilities_are_explicit() -> None:
    evidence = _json(GATE3_DIR / "gate3-evidence.json")
    zero = evidence["zero_external_activity"]

    assert zero["method"]
    assert zero["allowed_internal_destinations"] == [
        "127.0.0.1:5432",
        "127.0.0.1:8092",
    ]
    for counter in (
        "external_network_calls",
        "kingdee_calls",
        "model_provider_calls",
        "production_credentials_loaded",
        "external_messages",
        "business_writes",
    ):
        assert zero[counter] == 0

    capabilities = evidence["capability_ledger"]
    assert capabilities["manual_import_local"]["status"] == "pass"
    assert capabilities["context_proposal_write"]["status"] == "pass"
    for name in (
        "external_channel_canary",
        "real_model_provider",
        "kingdee",
        "cloud_runtime",
        "production",
    ):
        assert capabilities[name]["status"] in {"not_started", "not_applicable"}


def test_gate3_security_and_privacy_reviews_keep_human_and_external_gates_open() -> None:
    security = _json(GATE3_DIR / "security-review.json")
    privacy = _json(GATE3_DIR / "privacy-review.json")

    assert security["overall_status"] == "accepted_for_local_synthetic_runtime"
    assert security["human_review"]["status"] == "pending"
    assert privacy["overall_status"] == "accepted_for_synthetic_data_only"
    assert privacy["human_review"]["status"] == "pending"

    for risk in security["risks"]:
        assert risk["severity"] in {"critical", "high", "medium", "low"}
        assert risk["status"] in {
            "runtime_verified",
            "controlled_by_disabled_capability",
            "deferred_to_later_gate",
        }
        if risk["severity"] in {"critical", "high"}:
            assert risk["status"] != "deferred_to_later_gate"

    assert privacy["real_personal_data_processed"] is False
    assert privacy["cross_border_transfer_performed"] is False


def test_gate3_go_no_go_is_narrow_and_does_not_claim_production() -> None:
    evidence = _json(GATE3_DIR / "gate3-evidence.json")
    decision = evidence["go_no_go"]

    assert decision == {
        "technical_local_go": "go",
        "external_channel_canary": "not_started",
        "gate4_local_start": "go",
        "real_model": "no_go",
        "kingdee": "no_go",
        "cloud": "no_go",
        "production": "no_go",
    }
    assert evidence["limitations"]
    assert evidence["pending_evidence"]

    summary = (GATE3_DIR / "gate3-summary.md").read_text(encoding="utf-8").lower()
    for phrase in (
        "technical local go",
        "external channel",
        "real model",
        "kingdee",
        "cloud",
        "production",
        "not_started",
        "no-go",
    ):
        assert phrase in summary


def test_gate3_checksum_manifest_covers_only_compact_gate3_files() -> None:
    entries = (GATE3_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    covered: set[str] = set()

    assert entries
    for entry in entries:
        expected, relative = entry.split("  ", 1)
        assert "/" not in relative and "../" not in relative
        path = GATE3_DIR / relative
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        covered.add(relative)
    assert covered == GATE3_COMPACT_FILES
