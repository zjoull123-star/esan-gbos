from __future__ import annotations

import json
import subprocess
import sys

from .conftest import NOW, ROOT, write_json
from .test_preflight import rewrite


def run_plan(manifest_path, topology_path, operation: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/plan.py"),
            "--operation",
            operation,
            "--manifest",
            str(manifest_path),
            "--topology",
            str(topology_path),
            "--schema",
            str(ROOT / "contracts/gate6/release-manifest.schema.json"),
            "--now",
            NOW,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_plan_is_staged_validation_only(release_inputs) -> None:
    manifest_path, topology_path, _, _ = release_inputs()

    result = run_plan(manifest_path, topology_path, "release")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["mutations_executed"] is False
    assert [stage["name"] for stage in plan["stages"]] == [
        "local-preflight",
        "backup-readiness-check",
        "migration-forward-safety-check",
        "staged-release",
        "task-specific-verification",
        "observation-window",
    ]
    assert all(stage["execute"] is False for stage in plan["stages"])


def test_rollback_plan_requires_safe_forward_fix_metadata(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["operation"] = "rollback"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_plan(manifest_path, topology_path, "rollback")

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["rollback"]["strategy"] == "forward-fix"
    assert plan["rollback"]["schema_policy"] == "no-destructive-reversal"
    assert plan["rollback"]["target_release_id"] == "gbos-2026.08.06.3"
    assert plan["rollback"]["forward_fix_migrations"] == ["gate6-rollback-forward-fix"]
    assert all(stage["execute"] is False for stage in plan["stages"])


def test_rollback_plan_rejects_schema_reversal(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["operation"] = "rollback"
    manifest["rollback"]["strategy"] = "reverse-migrations"
    write_json(manifest_path, manifest)

    result = run_plan(manifest_path, topology_path, "rollback")

    assert result.returncode == 2
    assert "MANIFEST_SCHEMA" in result.stderr or "UNSAFE_ROLLBACK" in result.stderr
