from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from services.local_pilot_runtime.projection_config import (
    ProjectionConfigError,
    load_projection_config,
)


def _private(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _config(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    cas = tmp_path / "cas"
    vault = tmp_path / "vault"
    cas.mkdir()
    vault.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    users = {
        "observer": "gbos_observer_app",
        "context": "gbos_context_app",
        "agent": "gbos_agent_app",
    }
    connections: dict[str, object] = {}
    for role, user in users.items():
        connections[role] = {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": user,
            "password_file": str(_private(secrets / role, f"{role}-secret")),
            "connect_timeout_seconds": 3,
        }
    value: dict[str, object] = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "controlled_egress": True,
        "evidence_cas_root": str(cas),
        "tokenizer_vault_root": str(vault),
        "connections": connections,
    }
    path = tmp_path / "projection-connections.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, value


def test_closed_projection_config_requires_three_exact_roles_and_private_files(
    tmp_path: Path,
) -> None:
    path, _ = _config(tmp_path)

    config = load_projection_config(path, expected_site_id="gbos.localhost")

    assert tuple(config.connections) == ("observer", "context", "agent")
    assert config.connections["observer"].user == "gbos_observer_app"
    assert config.connections["context"].user == "gbos_context_app"
    assert config.connections["agent"].user == "gbos_agent_app"
    assert "secret" not in repr(config)


@pytest.mark.parametrize("mutation", ("reused_role", "broad_config", "symlink_root"))
def test_projection_config_rejects_reused_role_broad_permissions_and_symlink_root(
    tmp_path: Path,
    mutation: str,
) -> None:
    path, value = _config(tmp_path)
    if mutation == "reused_role":
        connections = value["connections"]
        assert isinstance(connections, dict)
        context = connections["context"]
        assert isinstance(context, dict)
        context["user"] = "gbos_observer_app"
    elif mutation == "broad_config":
        os.chmod(path, 0o640)
    else:
        target = tmp_path / "cas"
        target.rmdir()
        linked = tmp_path / "actual-cas"
        linked.mkdir()
        target.symlink_to(linked, target_is_directory=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    if mutation != "broad_config":
        os.chmod(path, 0o600)

    with pytest.raises(ProjectionConfigError):
        load_projection_config(path, expected_site_id="gbos.localhost")


def test_projection_config_rejects_site_mismatch_and_reused_password_file(
    tmp_path: Path,
) -> None:
    path, value = _config(tmp_path)
    connections = value["connections"]
    assert isinstance(connections, dict)
    observer = connections["observer"]
    context = connections["context"]
    assert isinstance(observer, dict) and isinstance(context, dict)
    context["password_file"] = observer["password_file"]
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(ProjectionConfigError):
        load_projection_config(path, expected_site_id="other.localhost")
    with pytest.raises(ProjectionConfigError):
        load_projection_config(path, expected_site_id="gbos.localhost")
