#!/usr/bin/env python3
"""Emit an inert staged release or rollback plan after local validation."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from preflight import ValidationFailure, parse_datetime, validate_files

RELEASE_STAGES = (
    "local-preflight",
    "backup-readiness-check",
    "migration-forward-safety-check",
    "staged-release",
    "task-specific-verification",
    "observation-window",
)
ROLLBACK_STAGES = (
    "local-preflight",
    "backup-readiness-check",
    "forward-fix-safety-check",
    "staged-traffic-withdrawal",
    "forward-fix-rollback",
    "task-specific-verification",
    "observation-window",
)


def build_plan(manifest: dict[str, Any], operation: str) -> dict[str, Any]:
    if manifest["operation"] != operation:
        raise ValidationFailure(
            "OPERATION_MISMATCH", "requested operation does not match immutable manifest"
        )
    stages = RELEASE_STAGES if operation == "release" else ROLLBACK_STAGES
    plan: dict[str, Any] = {
        "release_id": manifest["release_id"],
        "operation": operation,
        "environment_identity": manifest["environment"]["identity"],
        "mode": "dry-run",
        "mutations_executed": False,
        "stages": [
            {
                "ordinal": ordinal,
                "name": name,
                "execute": False,
                "authorization": "two-person-production"
                if operation == "release"
                else "two-person-rollback",
            }
            for ordinal, name in enumerate(stages, start=1)
        ],
    }
    if operation == "rollback":
        rollback = manifest["rollback"]
        if (
            rollback["strategy"] != "forward-fix"
            or rollback["schema_policy"] != "no-destructive-reversal"
            or any(
                migration["direction"] != "forward" or migration["destructive"] is not False
                for migration in rollback["forward_fix_migrations"]
            )
        ):
            raise ValidationFailure(
                "UNSAFE_ROLLBACK", "rollback must use non-destructive forward-fix migrations"
            )
        plan["rollback"] = {
            "target_release_id": rollback["target_release_id"],
            "target_source_commit": rollback["target_source_commit"],
            "strategy": rollback["strategy"],
            "schema_policy": rollback["schema_policy"],
            "forward_fix_migrations": [
                migration["id"] for migration in rollback["forward_fix_migrations"]
            ],
        }
    return plan


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Print a dry-run Gate 6 release or rollback plan; execute nothing."
    )
    argument_parser.add_argument("--operation", required=True, choices=("release", "rollback"))
    argument_parser.add_argument("--manifest", required=True, type=Path)
    argument_parser.add_argument("--topology", required=True, type=Path)
    argument_parser.add_argument("--schema", required=True, type=Path)
    argument_parser.add_argument("--now")
    return argument_parser


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        now = (
            parse_datetime(arguments.now, "NOW_INVALID") if arguments.now else datetime.now(tz=UTC)
        )
        validated = validate_files(
            arguments.manifest,
            arguments.topology,
            arguments.schema,
            now,
        )
        plan = build_plan(validated.manifest, arguments.operation)
    except ValidationFailure as failure:
        print(f"plan rejected: {failure.code}: {failure.message}", file=sys.stderr)
        return 2
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
