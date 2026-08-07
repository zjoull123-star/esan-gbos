from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.local_pilot_runtime import agent_api, context_api
from services.local_pilot_runtime.runtime_support import SecretValue


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _files(tmp_path: Path, *, agent_kill: bool = False) -> tuple[Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name, value in (
        ("postgres_password", "db-secret"),
        ("agent_api_bearer", "agent-token"),
        ("context_api_bearer", "context-token"),
        ("context_client_bearer", "context-client-token"),
    ):
        _secret(secret_dir / name, value)
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {"enabled": False, "kill_switch": True},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def component(enabled: bool, kill: bool) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill,
            "provider_mode": "disabled",
            "synthetic_e2e": False,
        }

    config = {
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
            "agent_api": component(True, agent_kill),
            "context_api": component(True, False),
            "agent_worker": {
                **component(False, True),
                "provider_mode": "deterministic",
                "synthetic_e2e": True,
            },
            "model_worker": {
                **component(False, True),
                "provider_mode": "deepseek",
            },
        },
        "worker": {
            "worker_id": "agent-worker-local-1",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return manifest_path, config_path


def test_agent_api_postgres_composition_is_ready_and_authorizer_is_secret_safe() -> None:
    connection = _Connection()
    authorizer = agent_api.ConstantTimeBearerSiteAuthorizer(
        bearer_token=SecretValue("agent-token"),
        site_id="gbos.localhost",
    )
    app = agent_api.build_postgres_app(
        connection=connection,
        site_id="gbos.localhost",
        bearer_token=SecretValue("agent-token"),
    )

    assert TestClient(app).get("/health").json()["ready"] is True
    assert (
        authorizer.authorize(
            authorization="Bearer agent-token",
            requested_site_id="gbos.localhost",
        )
        == "gbos.localhost"
    )
    for authorization, site_id in (
        ("Bearer wrong", "gbos.localhost"),
        ("Bearer agent-token", "other.localhost"),
    ):
        try:
            authorizer.authorize(
                authorization=authorization,
                requested_site_id=site_id,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("authorizer accepted a mismatched identity")
    assert "agent-token" not in repr(authorizer)


def test_agent_api_main_runs_injected_server_and_closes_connection(tmp_path: Path) -> None:
    manifest_path, config_path = _files(tmp_path)
    connection = _Connection()
    seen: list[tuple[FastAPI, dict[str, object]]] = []

    result = agent_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        connector=lambda **_: connection,
        server_runner=lambda app, **kwargs: seen.append((app, kwargs)),
    )

    assert result == 0
    assert len(seen) == 1
    assert seen[0][1] == {
        "host": "127.0.0.1",
        "port": 8002,
        "unix_socket": None,
        "network_mode": "loopback",
    }
    assert TestClient(seen[0][0]).get("/health").json()["ready"] is True
    assert connection.closed is True


def test_agent_api_missing_dependency_or_kill_switch_returns_78_without_connecting(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path, agent_kill=True)
    calls: list[dict[str, Any]] = []

    result = agent_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        connector=lambda **kwargs: calls.append(kwargs),
        server_runner=lambda *_: None,
    )

    assert result == 78
    assert calls == []
    assert TestClient(agent_api.build_app()).get("/health").json()["ready"] is False


def test_context_api_main_composes_postgres_decision_storage_and_per_request_auth(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path)
    connection = _Connection()
    seen: list[tuple[FastAPI, dict[str, object]]] = []

    result = context_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        connector=lambda **_: connection,
        server_runner=lambda app, **kwargs: seen.append((app, kwargs)),
    )

    assert result == 0
    assert len(seen) == 1
    assert seen[0][1] == {
        "host": "127.0.0.1",
        "port": 8001,
        "unix_socket": None,
        "network_mode": "loopback",
    }
    health = TestClient(seen[0][0]).get("/health")
    assert health.json()["ready"] is True
    assert connection.closed is True

    disabled = TestClient(context_api.build_app())
    assert disabled.get("/health").json()["ready"] is False
    response = disabled.post("/internal/v1/agent-context", json={})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("entrypoint", (agent_api, context_api))
def test_api_main_rejects_invalid_uds_before_postgres(
    entrypoint: Any,
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path)
    connect_calls: list[dict[str, Any]] = []
    server_calls: list[object] = []

    result = entrypoint.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_LISTEN_UNIX_SOCKET": "/tmp/gbos.sock",
        },
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *args, **kwargs: server_calls.append((args, kwargs)),
    )

    assert result == 78
    assert connect_calls == []
    assert server_calls == []


@pytest.mark.parametrize(
    ("entrypoint", "port"),
    ((agent_api, 8002), (context_api, 8001)),
)
def test_api_main_uses_valid_uds_instead_of_tcp(
    entrypoint: Any,
    port: int,
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path)
    connection = _Connection()
    seen: list[tuple[FastAPI, dict[str, object]]] = []

    result = entrypoint.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_LISTEN_UNIX_SOCKET": "/run/gbos/sockets/api.sock",
        },
        connector=lambda **_: connection,
        server_runner=lambda app, **kwargs: seen.append((app, kwargs)),
    )

    assert result == 0
    assert seen == [
        (
            seen[0][0],
            {
                "host": "127.0.0.1",
                "port": port,
                "unix_socket": Path("/run/gbos/sockets/api.sock"),
                "network_mode": "unix_socket",
            },
        )
    ]
    assert connection.closed is True
