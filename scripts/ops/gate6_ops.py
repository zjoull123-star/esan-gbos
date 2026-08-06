#!/usr/bin/env python3
"""Local-only Gate 6 operations policy and recovery validators.

This utility validates metadata and synthetic files. It never connects to a
service, executes a restore, mutates a database, deletes data, or performs a
production action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_ALERT_COVERAGE = frozenset(
    {
        "health_readiness",
        "latency",
        "error_rate",
        "saturation",
        "queue_depth",
        "queue_age",
        "dead_letters",
        "mariadb",
        "postgresql",
        "backup_age",
        "backup_integrity",
        "evidence_integrity",
        "metric_freshness",
        "metric_reconciliation",
        "connector_state",
        "audit_failures",
    }
)
REQUIRED_RECOVERY_SCENARIOS = frozenset({"restore", "pitr", "regional_disaster"})
REQUIRED_KILL_SWITCHES = frozenset(
    {"live_model", "external_connectors", "outbound_send", "destructive_actions"}
)
ALLOWED_SEVERITIES = frozenset({"info", "warning", "critical"})


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON value must be an object")
    return value


def emit_report(issues: Sequence[str], **details: object) -> int:
    payload: dict[str, object] = {"status": "fail" if issues else "pass", **details}
    payload["issues"] = list(issues)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if issues else 0


def validate_policy(policy: dict[str, Any], repo_root: Path) -> tuple[list[str], int, int]:
    issues: list[str] = []
    slos = policy.get("slos")
    alerts = policy.get("alerts")
    required_runbooks = policy.get("required_runbooks")
    if not isinstance(required_runbooks, list) or not required_runbooks:
        issues.append("policy.required_runbooks must be a non-empty list")
    else:
        for runbook in required_runbooks:
            if not isinstance(runbook, str) or not (repo_root / runbook).is_file():
                issues.append(f"required runbook does not exist: {runbook}")
    if not isinstance(slos, list) or not slos:
        issues.append("policy.slos must be a non-empty list")
        slos = []
    if not isinstance(alerts, list) or not alerts:
        issues.append("policy.alerts must be a non-empty list")
        alerts = []

    covered: set[str] = set()
    for index, alert in enumerate(alerts):
        prefix = f"alerts[{index}]"
        if not isinstance(alert, dict):
            issues.append(f"{prefix} must be an object")
            continue
        alert_id = str(alert.get("id", prefix))
        coverage = alert.get("coverage")
        if isinstance(coverage, list):
            covered.update(str(item) for item in coverage)
        else:
            issues.append(f"{alert_id}: coverage must be a list")
        if alert.get("severity") not in ALLOWED_SEVERITIES:
            issues.append(f"{alert_id}: severity must be info, warning, or critical")
        if not isinstance(alert.get("owner"), str) or not alert["owner"].strip():
            issues.append(f"{alert_id}: owner is required")
        escalation = alert.get("escalation")
        if not isinstance(escalation, list) or not escalation:
            issues.append(f"{alert_id}: escalation is required")
        runbook = alert.get("runbook")
        if not isinstance(runbook, str) or not runbook:
            issues.append(f"{alert_id}: runbook is required")
        else:
            runbook_path = (repo_root / runbook).resolve()
            try:
                runbook_path.relative_to(repo_root.resolve())
            except ValueError:
                issues.append(f"{alert_id}: runbook must stay within repository")
            else:
                if not runbook_path.is_file():
                    issues.append(f"{alert_id}: runbook does not exist: {runbook}")
        maintenance = alert.get("maintenance_window")
        if not isinstance(maintenance, dict):
            issues.append(f"{alert_id}: maintenance_window is required")
        elif maintenance.get("behavior") not in {
            "never_suppress",
            "suppress_page_preserve_event",
        }:
            issues.append(f"{alert_id}: invalid maintenance_window behavior")
        if not isinstance(alert.get("condition"), dict):
            issues.append(f"{alert_id}: machine-checkable condition is required")

    missing_coverage = sorted(REQUIRED_ALERT_COVERAGE - covered)
    if missing_coverage:
        issues.append(f"missing alert coverage: {', '.join(missing_coverage)}")

    for index, slo in enumerate(slos):
        prefix = f"slos[{index}]"
        if not isinstance(slo, dict):
            issues.append(f"{prefix} must be an object")
            continue
        for field in ("id", "indicator", "objective", "window", "owner"):
            if not slo.get(field):
                issues.append(f"{prefix}: {field} is required")
        if not isinstance(slo.get("objective"), (int, float)):
            issues.append(f"{prefix}: objective must be numeric")

    return issues, len(alerts), len(slos)


def parse_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return instant.astimezone(UTC)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_synthetic_path(path: Path, synthetic_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(synthetic_root.resolve(strict=True))
    except FileNotFoundError, ValueError:
        return False
    return True


def check_backups(inventory: dict[str, Any], now: datetime) -> tuple[list[str], int]:
    issues: list[str] = []
    if inventory.get("environment") != "synthetic":
        issues.append("inventory environment must be synthetic; production access is prohibited")
    synthetic_root_value = inventory.get("synthetic_root")
    if not isinstance(synthetic_root_value, str):
        return [*issues, "synthetic_root is required"], 0
    synthetic_root = Path(synthetic_root_value)
    if not synthetic_root.is_dir():
        return [*issues, "synthetic_root must be an existing local directory"], 0
    rpo_seconds = inventory.get("rpo_seconds")
    if not isinstance(rpo_seconds, (int, float)) or rpo_seconds <= 0:
        issues.append("rpo_seconds must be positive")
        rpo_seconds = 0
    backups = inventory.get("backups")
    if not isinstance(backups, list) or not backups:
        return [*issues, "missing backup inventory entries"], 0

    valid_created_at: list[datetime] = []
    for index, backup in enumerate(backups):
        prefix = f"backups[{index}]"
        if not isinstance(backup, dict):
            issues.append(f"{prefix} must be an object")
            continue
        path_value = backup.get("path")
        if not isinstance(path_value, str):
            issues.append(f"{prefix}: path is required")
            continue
        path = Path(path_value)
        if not safe_synthetic_path(path, synthetic_root):
            issues.append(f"{prefix}: path escapes synthetic_root")
            continue
        if not path.is_file():
            issues.append(f"{prefix}: missing backup file")
            continue
        expected_checksum = backup.get("sha256")
        if not isinstance(expected_checksum, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_checksum
        ):
            issues.append(f"{prefix}: valid sha256 is required")
        elif sha256_file(path) != expected_checksum:
            issues.append(f"{prefix}: checksum mismatch")
        try:
            created_at = parse_instant(str(backup["created_at"]))
        except KeyError, ValueError:
            issues.append(f"{prefix}: valid created_at is required")
        else:
            valid_created_at.append(created_at)

    if valid_created_at and rpo_seconds:
        newest = max(valid_created_at)
        age_seconds = (now - newest).total_seconds()
        if age_seconds < 0:
            issues.append("newest backup created_at is in the future")
        elif age_seconds > rpo_seconds:
            issues.append(f"stale backup: age {int(age_seconds)}s exceeds RPO {int(rpo_seconds)}s")
    return issues, len(backups)


def validate_recovery(plan: dict[str, Any]) -> tuple[list[str], int]:
    issues: list[str] = []
    if plan.get("environment") != "synthetic":
        issues.append("recovery validation is limited to synthetic plans")
    if plan.get("destructive_actions") is not False:
        issues.append("destructive_actions must be false")
    scenarios = plan.get("scenarios")
    if not isinstance(scenarios, list):
        return [*issues, "scenarios must be a list"], 0

    names: set[str] = set()
    for index, scenario in enumerate(scenarios):
        prefix = f"scenarios[{index}]"
        if not isinstance(scenario, dict):
            issues.append(f"{prefix} must be an object")
            continue
        name = str(scenario.get("name", prefix))
        names.add(name)
        observed_rpo = scenario.get("observed_rpo_seconds")
        max_rpo = scenario.get("max_rpo_seconds")
        observed_rto = scenario.get("observed_rto_seconds")
        max_rto = scenario.get("max_rto_seconds")
        if not all(isinstance(value, (int, float)) for value in (observed_rpo, max_rpo)):
            issues.append(f"{name}: numeric RPO values are required")
        elif observed_rpo > max_rpo:
            issues.append(f"{name}: RPO breach ({observed_rpo}s > {max_rpo}s)")
        if not all(isinstance(value, (int, float)) for value in (observed_rto, max_rto)):
            issues.append(f"{name}: numeric RTO values are required")
        elif observed_rto > max_rto:
            issues.append(f"{name}: RTO breach ({observed_rto}s > {max_rto}s)")
        if scenario.get("integrity_verified") is not True:
            issues.append(f"{name}: integrity verification is required")

    missing = sorted(REQUIRED_RECOVERY_SCENARIOS - names)
    if missing:
        issues.append(f"missing recovery scenarios: {', '.join(missing)}")
    return issues, len(scenarios)


def validate_operational_gate(state: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    alerts = state.get("alerts")
    if not isinstance(alerts, list):
        issues.append("alerts must be a list")
    else:
        for alert in alerts:
            if (
                isinstance(alert, dict)
                and alert.get("severity") == "critical"
                and alert.get("status") not in {"resolved", "closed"}
            ):
                issues.append(f"unresolved critical alert: {alert.get('id', 'unknown')}")
    switches = state.get("kill_switches")
    if not isinstance(switches, dict):
        issues.append("kill_switches must be an object")
    else:
        for name in sorted(REQUIRED_KILL_SWITCHES):
            if switches.get(name) is not True:
                issues.append(f"kill switch {name} is disabled")
    return issues


def validate_authorization(artifact: dict[str, Any], operation: str) -> list[str]:
    issues: list[str] = []
    if artifact.get("environment") != "production":
        issues.append("authorization environment must be production")
    if artifact.get("operation") != operation:
        issues.append("authorization operation does not match requested operation")
    if not artifact.get("release_id"):
        issues.append("release_id is required")
    approvals = artifact.get("approvals")
    if not isinstance(approvals, list):
        return [*issues, "approvals must be a list"]
    approved = [item for item in approvals if item.get("decision") == "approved"]
    actors = {str(item.get("actor", "")) for item in approved if item.get("actor")}
    roles = {str(item.get("role", "")) for item in approved if item.get("role")}
    if len(approved) < 2:
        issues.append("two approved authorization records are required")
    if len(actors) < 2:
        issues.append("two distinct approvers are required")
    if len(roles) < 2:
        issues.append("two distinct approval roles are required")
    return issues


REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+"),
        "Authorization: Bearer [REDACTED_TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(password|secret|api[_-]?key|token)\s*[=:]\s*\S+"),
        r"\1=[REDACTED_SECRET]",
    ),
    (
        re.compile(r"(?i)\braw_(?:message|content)\s*=\s*(?:\"[^\"]*\"|'[^']*'|\S+)"),
        "raw_message=[REDACTED_CONTENT]",
    ),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED_EMAIL]",
    ),
    (
        re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)"),
        "[REDACTED_PHONE]",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def local_drill_plan(synthetic_root: Path) -> dict[str, object]:
    if not synthetic_root.is_dir():
        raise ValueError("synthetic-root must be an existing local directory")
    return {
        "schema_version": 1,
        "environment": "synthetic",
        "synthetic_root": str(synthetic_root.resolve()),
        "mode": "plan_only",
        "destructive_actions": False,
        "safeguards": [
            "no network access",
            "no database commands",
            "no delete or overwrite",
            "operator records observed RPO, RTO, and checksum evidence",
        ],
        "scenarios": [
            {
                "name": "restore",
                "runbook": "docs/runbooks/backup-restore.md",
                "max_rpo_seconds": 3600,
                "max_rto_seconds": 3600,
                "observed_rpo_seconds": None,
                "observed_rto_seconds": None,
                "integrity_verified": False,
            },
            {
                "name": "pitr",
                "runbook": "docs/runbooks/pitr.md",
                "max_rpo_seconds": 900,
                "max_rto_seconds": 3600,
                "observed_rpo_seconds": None,
                "observed_rto_seconds": None,
                "integrity_verified": False,
            },
            {
                "name": "regional_disaster",
                "runbook": "docs/runbooks/regional-disaster.md",
                "max_rpo_seconds": 3600,
                "max_rto_seconds": 14400,
                "observed_rpo_seconds": None,
                "observed_rto_seconds": None,
                "integrity_verified": False,
            },
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    policy = subparsers.add_parser("validate-policy")
    policy.add_argument("--policy", type=Path, required=True)
    policy.add_argument("--repo-root", type=Path, required=True)

    backups = subparsers.add_parser("check-backups")
    backups.add_argument("--inventory", type=Path, required=True)
    backups.add_argument("--now", required=True)

    recovery = subparsers.add_parser("validate-recovery")
    recovery.add_argument("--plan", type=Path, required=True)

    gate = subparsers.add_parser("validate-operational-gate")
    gate.add_argument("--state", type=Path, required=True)

    authorization = subparsers.add_parser("validate-authorization")
    authorization.add_argument("--artifact", type=Path, required=True)
    authorization.add_argument("--operation", choices=("release", "rollback"), required=True)

    subparsers.add_parser("redact")

    drill = subparsers.add_parser("plan-drill")
    drill.add_argument("--environment", required=True)
    drill.add_argument("--synthetic-root", type=Path, required=True)
    drill.add_argument("--output", type=Path)
    drill.add_argument("--external-approval-artifact", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-policy":
            issues, alert_count, slo_count = validate_policy(load_json(args.policy), args.repo_root)
            status = "invalid" if issues else "valid"
            return emit_report(
                issues,
                status=status,
                alert_count=alert_count,
                slo_count=slo_count,
            )
        if args.command == "check-backups":
            issues, backup_count = check_backups(load_json(args.inventory), parse_instant(args.now))
            return emit_report(issues, backup_count=backup_count)
        if args.command == "validate-recovery":
            issues, scenario_count = validate_recovery(load_json(args.plan))
            return emit_report(issues, scenario_count=scenario_count)
        if args.command == "validate-operational-gate":
            issues = validate_operational_gate(load_json(args.state))
            return emit_report(issues)
        if args.command == "validate-authorization":
            issues = validate_authorization(load_json(args.artifact), args.operation)
            return emit_report(issues, operation=args.operation)
        if args.command == "redact":
            print(redact(sys.stdin.read()))
            return 0
        if args.command == "plan-drill":
            if args.environment != "synthetic":
                print(
                    "refused: production or external environments require explicit external "
                    "approval artifacts and remain outside this local-only tool",
                    file=sys.stderr,
                )
                return 1
            plan = local_drill_plan(args.synthetic_root)
            rendered = json.dumps(plan, indent=2, sort_keys=True)
            if args.output:
                output = args.output.resolve(strict=False)
                try:
                    output.relative_to(args.synthetic_root.resolve(strict=True))
                except ValueError:
                    print("refused: output must stay within synthetic-root", file=sys.stderr)
                    return 1
                if output.exists():
                    print(
                        "refused: output already exists; overwrite is prohibited", file=sys.stderr
                    )
                    return 1
                output.write_text(f"{rendered}\n", encoding="utf-8")
            print(rendered)
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"validation error: {redact(str(exc))}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
