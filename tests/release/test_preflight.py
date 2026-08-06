from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .conftest import NOW, ROOT, write_json


def run_preflight(manifest_path: Path, topology_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release/preflight.py"),
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


def rewrite(
    manifest_path: Path,
    topology_path: Path,
    manifest: dict[str, Any],
    topology: dict[str, Any],
) -> None:
    write_json(topology_path, topology)
    manifest["topology"]["sha256"] = hashlib.sha256(topology_path.read_bytes()).hexdigest()
    write_json(manifest_path, manifest)


def test_valid_inputs_pass_fail_closed_preflight(release_inputs) -> None:
    manifest_path, topology_path, _, _ = release_inputs()

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "preflight passed: gbos-2026.08.07.1"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda manifest, topology: topology["domain"].clear(), "TOPOLOGY_DOMAIN_REQUIRED"),
        (lambda manifest, topology: topology["tls"].clear(), "TOPOLOGY_TLS_REQUIRED"),
        (
            lambda manifest, topology: topology["secrets"].update(required_refs=[]),
            "TOPOLOGY_SECRETS_REQUIRED",
        ),
        (
            lambda manifest, topology: topology["components"]["app"].update(secret_refs=[]),
            "COMPONENT_SECRET_REFERENCE_INVALID",
        ),
        (lambda manifest, topology: topology["backup"].clear(), "BACKUP_TARGET_REQUIRED"),
        (
            lambda manifest, topology: topology["components"]["mariadb"].update(
                public_ports=[3306]
            ),
            "PUBLIC_DATA_PORT",
        ),
        (
            lambda manifest, topology: topology["components"]["postgres_pgvector"].update(
                public_ports=[5432]
            ),
            "PUBLIC_DATA_PORT",
        ),
        (
            lambda manifest, topology: topology["components"]["queue_cache"].update(
                public_ports=[6379]
            ),
            "PUBLIC_DATA_PORT",
        ),
    ],
)
def test_topology_negative_gates(
    release_inputs,
    mutate: Callable[[dict[str, Any], dict[str, Any]], None],
    code: str,
) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    mutate(manifest, topology)
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert code in result.stderr


def test_floating_topology_image_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    topology["components"]["app"]["image"] = "registry.example.invalid/gbos/app:latest"
    manifest["images"]["app"] = topology["components"]["app"]["image"]
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "FLOATING_IMAGE" in result.stderr


def test_missing_privacy_approval_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    del manifest["approvals"]["privacy"]
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "MANIFEST_SCHEMA" in result.stderr


def test_unapproved_release_identity_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["environment"]["release_identity"] = "unapproved-release-identity"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "RELEASE_IDENTITY_UNAPPROVED" in result.stderr


@pytest.mark.parametrize(
    "approval_key",
    ["production", "rollback"],
)
def test_one_person_authorization_is_rejected(release_inputs, approval_key: str) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    if approval_key == "rollback":
        manifest["operation"] = "rollback"
    manifest["approvals"][approval_key] = manifest["approvals"][approval_key][:1]
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "TWO_PERSON_AUTHORIZATION_REQUIRED" in result.stderr


@pytest.mark.parametrize(
    "capability",
    [
        "connectors_enabled",
        "live_models_enabled",
        "kingdee_enabled",
        "external_sends_enabled",
        "destructive_operations_enabled",
    ],
)
def test_kill_switch_controlled_capability_must_remain_disabled(
    release_inputs, capability: str
) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    topology["capabilities"][capability] = True
    manifest["flags"][capability] = True
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "CAPABILITY_MUST_BE_DISABLED" in result.stderr


def test_malformed_approval_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["approvals"]["privacy"]["approved_at"] = "not-a-timestamp"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "MANIFEST_SCHEMA" in result.stderr


def test_stale_approval_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["approvals"]["production"][0]["approved_at"] = "2026-07-01T00:00:00Z"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "APPROVAL_STALE" in result.stderr


def test_stale_rollback_authorization_is_rejected_even_in_release_plan(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["approvals"]["rollback"][0]["approved_at"] = "2026-07-01T00:00:00Z"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "APPROVAL_STALE" in result.stderr


def test_manifest_and_topology_mismatch_is_rejected(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    manifest["environment"]["identity"] = "different-production-environment"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "MANIFEST_TOPOLOGY_MISMATCH" in result.stderr


def test_raw_secret_value_is_rejected_without_echoing_it(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    secret = "do-not-print-this-secret"
    topology["secrets"]["password"] = secret
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "RAW_SECRET_MATERIAL_FORBIDDEN" in result.stderr
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_unknown_topology_fields_fail_closed(release_inputs) -> None:
    manifest_path, topology_path, manifest, topology = release_inputs()
    topology["components"]["app"]["runtime_override"] = "unreviewed"
    rewrite(manifest_path, topology_path, manifest, topology)

    result = run_preflight(manifest_path, topology_path)

    assert result.returncode == 2
    assert "TOPOLOGY_UNKNOWN_FIELD" in result.stderr
