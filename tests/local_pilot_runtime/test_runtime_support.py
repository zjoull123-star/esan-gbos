from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from services.local_pilot_runtime.runtime_support import (
    RuntimeSupportError,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
)


def _secret(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _config(path: Path, secret_dir: Path) -> Path:
    value = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "postgres": {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": "gbos_agent_app",
            "password_file": str(secret_dir / "postgres_password"),
            "connect_timeout_seconds": 3,
        },
        "auth": {
            "agent_api_bearer_file": str(secret_dir / "agent_api_bearer"),
            "context_api_bearer_file": str(secret_dir / "context_api_bearer"),
            "context_client_bearer_file": str(secret_dir / "context_client_bearer"),
            "context_auth_ref": "auth-agent-runtime",
        },
        "context_endpoint": {
            "base_url": "http://127.0.0.1:8001",
            "unix_socket": None,
        },
        "listen": {
            "host": "127.0.0.1",
            "agent_api_port": 8002,
            "context_api_port": 8001,
        },
        "components": {
            "agent_api": {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            },
            "context_api": {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            },
            "agent_worker": {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "deterministic",
                "synthetic_e2e": True,
            },
            "model_worker": {
                "enabled": False,
                "kill_switch": True,
                "provider_mode": "deepseek",
                "synthetic_e2e": False,
            },
        },
        "worker": {
            "worker_id": "agent-worker-local-1",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_runtime_config_and_postgres_connection_use_only_0600_secret_files(
    tmp_path: Path,
) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    password = _secret(secret_dir / "postgres_password", "db-secret")
    for name in (
        "agent_api_bearer",
        "context_api_bearer",
        "context_client_bearer",
    ):
        _secret(secret_dir / name, f"{name}-secret")
    config = load_runtime_config(_config(tmp_path / "runtime.json", secret_dir))
    captured: list[dict[str, Any]] = []
    connection = object()

    def connector(**kwargs: Any) -> object:
        captured.append(kwargs)
        return connection

    assert connect_postgres(config.postgres, connector=connector) is connection
    assert captured == [
        {
            "host": "127.0.0.1",
            "port": 55432,
            "dbname": "gbos_local_pilot",
            "user": "gbos_agent_app",
            "password": "db-secret",
            "connect_timeout": 3,
        }
    ]
    secret = load_secret_file(password)
    assert secret.reveal() == "db-secret"
    assert "db-secret" not in repr(secret)
    assert "db-secret" not in str(secret)
    assert "db-secret" not in repr(config)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o666])
def test_secret_file_requires_exact_mode_0600(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "secret"
    path.write_text("secret", encoding="utf-8")
    os.chmod(path, mode)

    with pytest.raises(RuntimeSupportError, match="0600"):
        load_secret_file(path)


@pytest.mark.parametrize(
    "environment",
    [
        {"POSTGRES_PASSWORD": "secret"},
        {"DEEPSEEK_API_KEY": "secret"},
        {"GBOS_AGENT_API_TOKEN": "secret"},
        {"FRAPPE_API_SECRET": "secret"},
    ],
)
def test_plaintext_secret_environment_is_forbidden(
    environment: dict[str, str],
) -> None:
    with pytest.raises(RuntimeSupportError, match="plaintext secret"):
        reject_plaintext_secret_environment(environment)


def test_runtime_config_is_closed_and_local_only(tmp_path: Path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    path = _config(tmp_path / "runtime.json", secret_dir)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["postgres"]["host"] = "db.example.com"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(RuntimeSupportError, match="local"):
        load_runtime_config(path)

    value["postgres"]["host"] = "127.0.0.1"
    value["plaintext_password"] = "forbidden"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RuntimeSupportError, match="closed"):
        load_runtime_config(path)
