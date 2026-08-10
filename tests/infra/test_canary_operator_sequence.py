from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
START = ROOT / "scripts" / "local-pilot" / "start"
RUNBOOK = ROOT / "docs" / "local-pilot" / "RUNBOOK.md"

REQUIRED_LIVE_CHECKS = {
    "email_body_peek_no_backfill",
    "user_mapping_reviewed",
    "party_mapping_reviewed",
    "user_second_message_auto_resolved",
    "party_second_message_auto_resolved",
    "model_identity_exact",
    "model_input_tokenized",
    "ai_draft_review_visible",
    "budget_limits_verified",
    "retention_verified",
    "emergency_stop_verified",
    "fault_drills_verified",
    "zero_prohibited_actions",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_start_calls_the_repository_bound_canary_preflight_contract() -> None:
    source = _read(START)
    invocation = source.split('"${SCRIPT_DIR}/canary-preflight"', maxsplit=1)[1].split(
        "GBOS_CANARY_CONTROL=", maxsplit=1
    )[0]

    assert "--manifest" in invocation
    assert "--run-control" in invocation
    assert "--secret-dir" in invocation
    assert "--repo-root" not in invocation


def test_runbook_uses_the_private_compose_verifier_and_complete_evidence_ledger() -> None:
    runbook = _read(RUNBOOK)
    sequence = runbook.split("严格按以下顺序执行", maxsplit=1)[1].split("## Keychain", maxsplit=1)[
        0
    ]
    commands = sequence.split("```sh", maxsplit=1)[1].split("```", maxsplit=1)[0]

    assert "--repo-root" not in commands
    assert "scripts/local-pilot/canary_verifier_runtime" in commands
    assert "scripts/local-pilot/verify-canary-chain" not in commands
    assert commands.count("scripts/local-pilot/canary-evidence sample") >= 2
    assert "status-before.json" in commands
    assert "status-after.json" in commands
    for kind in REQUIRED_LIVE_CHECKS:
        assert f"--kind {kind}" in commands
    assert commands.index("status-before.json") < commands.index("--kind model_identity_exact")
    assert commands.index("--kind model_identity_exact") < commands.index("status-after.json")
    assert commands.index("status-after.json") < commands.index(
        "scripts/local-pilot/canary-evidence finalize"
    )
