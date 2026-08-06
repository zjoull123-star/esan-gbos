from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_SCRIPT = REPO_ROOT / "scripts" / "ops" / "gate6_ops.py"
POLICY = REPO_ROOT / "infra" / "observability" / "slo-alert-policy.json"


def run_ops(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def write_json(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def iso_at(now: datetime, *, seconds_ago: int) -> str:
    return (now - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def backup_inventory(
    synthetic_root: Path,
    backup: Path,
    *,
    now: datetime,
    age_seconds: int = 60,
    checksum: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "environment": "synthetic",
        "synthetic_root": str(synthetic_root),
        "rpo_seconds": 3600,
        "backups": [
            {
                "id": "backup-001",
                "database": "mariadb",
                "path": str(backup),
                "created_at": iso_at(now, seconds_ago=age_seconds),
                "sha256": checksum or hashlib.sha256(backup.read_bytes()).hexdigest(),
            }
        ],
    }


def recovery_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "environment": "synthetic",
        "destructive_actions": False,
        "scenarios": [
            {
                "name": "restore",
                "observed_rpo_seconds": 120,
                "max_rpo_seconds": 900,
                "observed_rto_seconds": 600,
                "max_rto_seconds": 1800,
                "integrity_verified": True,
            },
            {
                "name": "pitr",
                "observed_rpo_seconds": 300,
                "max_rpo_seconds": 900,
                "observed_rto_seconds": 1200,
                "max_rto_seconds": 3600,
                "integrity_verified": True,
            },
            {
                "name": "regional_disaster",
                "observed_rpo_seconds": 600,
                "max_rpo_seconds": 1800,
                "observed_rto_seconds": 2400,
                "max_rto_seconds": 7200,
                "integrity_verified": True,
            },
        ],
    }


def authorization() -> dict[str, object]:
    return {
        "schema_version": 1,
        "environment": "production",
        "operation": "rollback",
        "release_id": "release-20260807",
        "approvals": [
            {"actor": "release-owner", "role": "release_owner", "decision": "approved"},
            {"actor": "ops-owner", "role": "incident_commander", "decision": "approved"},
        ],
    }


def test_repository_policy_and_runbooks_are_machine_validated() -> None:
    result = run_ops("validate-policy", "--policy", str(POLICY), "--repo-root", str(REPO_ROOT))

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "valid"
    assert report["alert_count"] >= 12
    assert report["slo_count"] >= 8


def test_policy_declares_complete_required_runbook_catalog() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))

    assert set(policy["required_runbooks"]) == {
        "docs/runbooks/incident-response.md",
        "docs/runbooks/credential-rotation.md",
        "docs/runbooks/compromised-connector.md",
        "docs/runbooks/model-kill-switch.md",
        "docs/runbooks/data-breach.md",
        "docs/runbooks/privacy-request.md",
        "docs/runbooks/support-access.md",
        "docs/runbooks/audit-export.md",
        "docs/runbooks/deployment-rollback.md",
        "docs/runbooks/backup-restore.md",
        "docs/runbooks/pitr.md",
        "docs/runbooks/regional-disaster.md",
    }


def test_policy_rejects_missing_runbook_owner_and_escalation(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["alerts"][0]["owner"] = ""
    policy["alerts"][0]["escalation"] = []
    policy["alerts"][0]["runbook"] = "docs/runbooks/not-present.md"
    invalid = write_json(tmp_path / "invalid-policy.json", policy)

    result = run_ops("validate-policy", "--policy", str(invalid), "--repo-root", str(REPO_ROOT))

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert any("owner" in issue for issue in report["issues"])
    assert any("escalation" in issue for issue in report["issues"])
    assert any("runbook" in issue for issue in report["issues"])


@pytest.mark.parametrize("fault", ["missing", "stale"])
def test_backup_check_rejects_missing_or_stale_backup(tmp_path: Path, fault: str) -> None:
    now = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
    backup = tmp_path / "synthetic" / "mariadb.dump"
    backup.parent.mkdir()
    backup.write_bytes(b"synthetic-backup")
    inventory = backup_inventory(
        backup.parent,
        backup,
        now=now,
        age_seconds=7200 if fault == "stale" else 60,
    )
    if fault == "missing":
        backup.unlink()
    manifest = write_json(tmp_path / "inventory.json", inventory)

    result = run_ops("check-backups", "--inventory", str(manifest), "--now", now.isoformat())

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert any(fault in issue for issue in report["issues"])


def test_backup_check_rejects_checksum_mismatch(tmp_path: Path) -> None:
    now = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
    backup = tmp_path / "synthetic" / "postgres.dump"
    backup.parent.mkdir()
    backup.write_bytes(b"synthetic-backup")
    inventory = backup_inventory(
        backup.parent,
        backup,
        now=now,
        checksum="0" * 64,
    )
    manifest = write_json(tmp_path / "inventory.json", inventory)

    result = run_ops("check-backups", "--inventory", str(manifest), "--now", now.isoformat())

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert any("checksum mismatch" in issue for issue in report["issues"])


def test_backup_check_accepts_fresh_integrity_verified_synthetic_backup(tmp_path: Path) -> None:
    now = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)
    backup = tmp_path / "synthetic" / "postgres.dump"
    backup.parent.mkdir()
    backup.write_bytes(b"synthetic-backup")
    manifest = write_json(
        tmp_path / "inventory.json",
        backup_inventory(backup.parent, backup, now=now),
    )

    result = run_ops("check-backups", "--inventory", str(manifest), "--now", now.isoformat())

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "pass"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("observed_rpo_seconds", 901, "RPO breach"),
        ("observed_rto_seconds", 1801, "RTO breach"),
        ("integrity_verified", False, "integrity"),
    ],
)
def test_recovery_validation_rejects_rpo_rto_or_integrity_breach(
    tmp_path: Path,
    field: str,
    value: int | bool,
    expected: str,
) -> None:
    plan = recovery_plan()
    plan["scenarios"][0][field] = value  # type: ignore[index]
    plan_file = write_json(tmp_path / "recovery-plan.json", plan)

    result = run_ops("validate-recovery", "--plan", str(plan_file))

    assert result.returncode == 1
    assert any(expected in issue for issue in json.loads(result.stdout)["issues"])


def test_recovery_validation_requires_restore_pitr_and_regional_dr(tmp_path: Path) -> None:
    plan = recovery_plan()
    plan["scenarios"] = plan["scenarios"][:2]  # type: ignore[index]
    plan_file = write_json(tmp_path / "recovery-plan.json", plan)

    result = run_ops("validate-recovery", "--plan", str(plan_file))

    assert result.returncode == 1
    assert any("regional_disaster" in issue for issue in json.loads(result.stdout)["issues"])


def test_operational_gate_rejects_unresolved_critical_alert(tmp_path: Path) -> None:
    state = write_json(
        tmp_path / "state.json",
        {
            "alerts": [
                {
                    "id": "audit-write-failure",
                    "severity": "critical",
                    "status": "firing",
                }
            ],
            "kill_switches": {
                "live_model": True,
                "external_connectors": True,
                "outbound_send": True,
                "destructive_actions": True,
            },
        },
    )

    result = run_ops("validate-operational-gate", "--state", str(state))

    assert result.returncode == 1
    assert any(
        "unresolved critical alert" in issue for issue in json.loads(result.stdout)["issues"]
    )


def test_operational_gate_rejects_disabled_kill_switch(tmp_path: Path) -> None:
    state = write_json(
        tmp_path / "state.json",
        {
            "alerts": [],
            "kill_switches": {
                "live_model": False,
                "external_connectors": True,
                "outbound_send": True,
                "destructive_actions": True,
            },
        },
    )

    result = run_ops("validate-operational-gate", "--state", str(state))

    assert result.returncode == 1
    assert any(
        "kill switch live_model is disabled" in issue
        for issue in json.loads(result.stdout)["issues"]
    )


def test_two_person_authorization_requires_distinct_approvers(tmp_path: Path) -> None:
    artifact = authorization()
    artifact["approvals"][1]["actor"] = "release-owner"  # type: ignore[index]
    artifact_file = write_json(tmp_path / "authorization.json", artifact)

    result = run_ops(
        "validate-authorization",
        "--artifact",
        str(artifact_file),
        "--operation",
        "rollback",
    )

    assert result.returncode == 1
    assert any("distinct" in issue for issue in json.loads(result.stdout)["issues"])


def test_two_person_authorization_accepts_distinct_roles(tmp_path: Path) -> None:
    artifact_file = write_json(tmp_path / "authorization.json", authorization())

    result = run_ops(
        "validate-authorization",
        "--artifact",
        str(artifact_file),
        "--operation",
        "rollback",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "pass"


def test_redaction_removes_secret_token_email_phone_and_raw_message_content() -> None:
    source = (
        "Authorization: Bearer top-secret-token "
        "password=hunter2 alice@example.com +86 138-0013-8000 "
        'raw_message="customer private message"'
    )

    result = run_ops("redact", input_text=source)

    assert result.returncode == 0, result.stderr
    assert "top-secret-token" not in result.stdout
    assert "hunter2" not in result.stdout
    assert "alice@example.com" not in result.stdout
    assert "138-0013-8000" not in result.stdout
    assert "customer private message" not in result.stdout
    assert "[REDACTED_" in result.stdout


def test_drill_planner_refuses_production_without_external_approval_artifacts(
    tmp_path: Path,
) -> None:
    synthetic_root = tmp_path / "synthetic"
    synthetic_root.mkdir()
    output = tmp_path / "must-not-exist.json"

    result = run_ops(
        "plan-drill",
        "--environment",
        "production",
        "--synthetic-root",
        str(synthetic_root),
        "--output",
        str(output),
    )

    assert result.returncode == 1
    assert "explicit external approval artifacts" in result.stderr
    assert not output.exists()


def test_drill_planner_emits_non_destructive_local_plan(tmp_path: Path) -> None:
    synthetic_root = tmp_path / "synthetic"
    synthetic_root.mkdir()

    result = run_ops(
        "plan-drill",
        "--environment",
        "synthetic",
        "--synthetic-root",
        str(synthetic_root),
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["environment"] == "synthetic"
    assert plan["destructive_actions"] is False
    assert {item["name"] for item in plan["scenarios"]} == {
        "restore",
        "pitr",
        "regional_disaster",
    }
    for scenario in plan["scenarios"]:
        assert scenario["max_rpo_seconds"] > 0
        assert scenario["max_rto_seconds"] > 0
        assert scenario["observed_rpo_seconds"] is None
        assert scenario["observed_rto_seconds"] is None
        assert scenario["integrity_verified"] is False
