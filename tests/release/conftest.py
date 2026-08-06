from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
NOW = "2026-08-07T08:00:00Z"
DIGESTS = {
    name: f"registry.example.invalid/gbos/{name}@sha256:{str(index) * 64}"
    for index, name in enumerate(
        ("app", "mariadb", "postgres-pgvector", "redis", "object-storage", "ingress", "monitoring"),
        start=1,
    )
}


def topology_document() -> dict[str, Any]:
    return {
        "api_version": "gbos.esan/v1",
        "kind": "ProductionTopology",
        "topology_id": "gbos-prod-single-tenant-v1",
        "version": 1,
        "enabled": False,
        "environment": {
            "name": "production",
            "identity": "gbos-production-primary",
            "tenant_mode": "single",
            "tenant_id": "tenant-placeholder",
        },
        "domain": {"primary": "gbos.example.invalid"},
        "tls": {"secret_ref": "secret://prod/ingress/tls-certificate"},
        "secrets": {
            "provider": "external-kms",
            "kms_key_ref": "kms://prod/gbos/config",
            "required_refs": [
                "secret://prod/app/runtime",
                "secret://prod/mariadb/application",
                "secret://prod/postgres/metrics",
                "secret://prod/redis/auth",
                "secret://prod/object-storage/auth",
            ],
        },
        "backup": {
            "target_ref": "object://prod-backups/gbos-primary",
            "identity": "gbos-prod-backup-writer",
            "kms_key_ref": "kms://prod/gbos/backups",
        },
        "release_policy": {
            "approved_release_identities": ["gbos-release-automation"],
            "minimum_production_approvals": 2,
            "minimum_rollback_approvals": 2,
            "approval_max_age_hours": 72,
        },
        "capabilities": {
            "connectors_enabled": False,
            "live_models_enabled": False,
            "kingdee_enabled": False,
            "external_sends_enabled": False,
            "destructive_operations_enabled": False,
        },
        "components": {
            "app": {
                "image": DIGESTS["app"],
                "identity": "gbos-prod-app",
                "network": "application",
                "public_ports": [],
                "secret_refs": ["secret://prod/app/runtime"],
            },
            "mariadb": {
                "image": DIGESTS["mariadb"],
                "identity": "gbos-prod-mariadb",
                "network": "data",
                "public_ports": [],
                "secret_refs": ["secret://prod/mariadb/application"],
            },
            "postgres_pgvector": {
                "image": DIGESTS["postgres-pgvector"],
                "identity": "gbos-prod-postgres",
                "network": "data",
                "public_ports": [],
                "secret_refs": ["secret://prod/postgres/metrics"],
            },
            "queue_cache": {
                "image": DIGESTS["redis"],
                "identity": "gbos-prod-queue-cache",
                "network": "data",
                "public_ports": [],
                "secret_refs": ["secret://prod/redis/auth"],
            },
            "object_storage": {
                "image": DIGESTS["object-storage"],
                "identity": "gbos-prod-object-storage",
                "network": "storage",
                "public_ports": [],
                "secret_refs": ["secret://prod/object-storage/auth"],
            },
            "ingress_waf": {
                "image": DIGESTS["ingress"],
                "identity": "gbos-prod-ingress",
                "network": "edge",
                "public_ports": [443],
                "secret_refs": ["secret://prod/ingress/tls-certificate"],
            },
            "monitoring": {
                "image": DIGESTS["monitoring"],
                "identity": "gbos-prod-monitoring-reader",
                "network": "monitoring",
                "public_ports": [],
                "secret_refs": [],
            },
        },
    }


def approval(identity: str, role: str, release_id: str) -> dict[str, str]:
    return {
        "identity": identity,
        "role": role,
        "approved_at": "2026-08-07T07:00:00Z",
        "scope": "production",
        "release_id": release_id,
        "decision": "approved",
    }


def manifest_document(topology: dict[str, Any], topology_sha256: str) -> dict[str, Any]:
    release_id = "gbos-2026.08.07.1"
    return {
        "schema_version": 1,
        "release_id": release_id,
        "operation": "release",
        "issued_at": "2026-08-07T06:00:00Z",
        "expires_at": "2026-08-08T06:00:00Z",
        "source": {
            "repository": "esan/gbos",
            "commit": "a" * 40,
            "dirty": False,
        },
        "lockfiles": [
            {"path": "uv.lock", "sha256": "b" * 64},
            {"path": "apps/esan_gbos/frontend/pnpm-lock.yaml", "sha256": "c" * 64},
        ],
        "images": {name: component["image"] for name, component in topology["components"].items()},
        "migrations": [
            {
                "id": "gate6-001-forward",
                "sha256": "d" * 64,
                "direction": "forward",
                "destructive": False,
            }
        ],
        "artifacts": {
            "sbom": {"path": "artifacts/sbom.spdx.json", "sha256": "e" * 64},
            "checksums": {"path": "artifacts/SHA256SUMS", "sha256": "f" * 64},
        },
        "flags": copy.deepcopy(topology["capabilities"]),
        "topology": {
            "id": topology["topology_id"],
            "sha256": topology_sha256,
            "primary_domain": topology["domain"]["primary"],
            "tls_secret_ref": topology["tls"]["secret_ref"],
            "backup_target_ref": topology["backup"]["target_ref"],
        },
        "rollback": {
            "target_release_id": "gbos-2026.08.06.3",
            "target_source_commit": "1" * 40,
            "strategy": "forward-fix",
            "schema_policy": "no-destructive-reversal",
            "forward_fix_migrations": [
                {
                    "id": "gate6-rollback-forward-fix",
                    "sha256": "2" * 64,
                    "direction": "forward",
                    "destructive": False,
                }
            ],
        },
        "approvals": {
            "privacy": approval("privacy-owner", "privacy-approver", release_id),
            "production": [
                approval("release-owner-a", "release-approver", release_id),
                approval("release-owner-b", "release-approver", release_id),
            ],
            "rollback": [
                approval("rollback-owner-a", "rollback-approver", release_id),
                approval("rollback-owner-b", "rollback-approver", release_id),
            ],
        },
        "environment": {
            "name": topology["environment"]["name"],
            "identity": topology["environment"]["identity"],
            "release_identity": "gbos-release-automation",
            "tenant_id": topology["environment"]["tenant_id"],
        },
    }


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def release_inputs(
    tmp_path: Path,
) -> Callable[[], tuple[Path, Path, dict[str, Any], dict[str, Any]]]:
    def build() -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
        topology = topology_document()
        topology_path = tmp_path / "topology.json"
        write_json(topology_path, topology)
        topology_sha256 = hashlib.sha256(topology_path.read_bytes()).hexdigest()
        manifest = manifest_document(topology, topology_sha256)
        manifest_path = tmp_path / "release-manifest.json"
        write_json(manifest_path, manifest)
        return manifest_path, topology_path, manifest, topology

    return build
