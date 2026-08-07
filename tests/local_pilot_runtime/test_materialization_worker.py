from __future__ import annotations

import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from services.agent_runtime.frappe_client import HttpFrappeDraftClient
from services.agent_runtime.frappe_context import HttpMaterializationContextResolver
from services.agent_runtime.materialization import (
    MaterializationRunResult,
    MaterializationWorker,
)
from services.agent_runtime.postgres import PostgresAgentTaskRepository
from services.local_pilot_runtime import materialization_worker


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        raise AssertionError("test composition must not perform HTTP")


def _secret(path: Path, value: str, *, mode: int = 0o600) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def _files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    postgres_password = _secret(secret_dir / "postgres_password", "db-secret")
    for name in (
        "agent_api_bearer",
        "context_api_bearer",
        "context_client_bearer",
    ):
        _secret(secret_dir / name, f"{name}-secret")
    frappe_key = _secret(secret_dir / "frappe_materializer_api_key", "frappe-key")
    frappe_secret = _secret(
        secret_dir / "frappe_materializer_api_secret",
        "frappe-secret",
    )
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

    def component(enabled: bool, kill_switch: bool) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
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
            "password_file": str(postgres_password),
            "connect_timeout_seconds": 3,
        },
        "auth": {
            "agent_api_bearer_file": str(secret_dir / "agent_api_bearer"),
            "context_api_bearer_file": str(secret_dir / "context_api_bearer"),
            "context_client_bearer_file": str(secret_dir / "context_client_bearer"),
            "context_auth_ref": "auth-agent-materializer",
        },
        "context_endpoint": {
            "base_url": "http://127.0.0.1:8000",
            "unix_socket": None,
        },
        "listen": {
            "host": "127.0.0.1",
            "agent_api_port": 8002,
            "context_api_port": 8001,
        },
        "components": {
            "agent_api": component(False, True),
            "context_api": component(False, True),
            "agent_worker": component(True, False),
            "model_worker": component(False, True),
        },
        "worker": {
            "worker_id": "materialization-worker-local-1",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return manifest_path, config_path, frappe_key, frappe_secret


def _enabled_environment() -> dict[str, str]:
    return {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_MATERIALIZATION_KILL_SWITCH": "false",
    }


def test_materialization_worker_import_is_side_effect_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted DB or network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)

    module = importlib.reload(materialization_worker)

    assert callable(module.main)
    assert callable(module.run_worker)


@pytest.mark.parametrize("endpoint", ["loopback", "unix", "internal"])
def test_main_composes_real_scoped_worker_without_db_or_http_until_preflight_passes(
    tmp_path: Path,
    endpoint: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    if endpoint != "loopback":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if endpoint == "unix":
            config["context_endpoint"]["unix_socket"] = "/run/gbos/sockets/frappe.sock"
        else:
            config["context_endpoint"]["base_url"] = "http://frappe-backend:8000"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    connection = _Connection()
    transport = _Transport()
    seen: list[tuple[MaterializationWorker, str, Event, float]] = []
    fixed_now = datetime(2026, 8, 8, 10, tzinfo=UTC)

    def runner(
        worker: MaterializationWorker,
        site_id: str,
        stop_event: Event,
        idle_delay: float,
        sleep: Any,
    ) -> None:
        seen.append((worker, site_id, stop_event, idle_delay))
        stop_event.set()

    result = materialization_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=_enabled_environment(),
        connector=lambda **_: connection,
        worker_runner=runner,
        clock=lambda: fixed_now,
        frappe_transport=transport,
    )

    assert result == 0
    assert len(seen) == 1
    worker, site_id, stop_event, idle_delay = seen[0]
    assert isinstance(worker._repository, PostgresAgentTaskRepository)
    assert isinstance(worker._client, HttpFrappeDraftClient)
    assert isinstance(worker._context_resolver, HttpMaterializationContextResolver)
    assert worker._lease_duration.total_seconds() == 10
    assert site_id == "gbos.localhost"
    assert stop_event.is_set()
    assert idle_delay == 0.1
    assert transport.calls == []
    assert connection.closed is True
    assert "frappe-secret" not in repr(worker)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://frappe-backend.evil:8000",
        "http://frappe-backend:8001",
        "http://user:secret@frappe-backend:8000",
        "http://frappe-backend:8000?next=http://evil.invalid",
    ),
)
def test_internal_endpoint_confusion_exits_78_before_database_or_http(
    tmp_path: Path,
    base_url: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["context_endpoint"]["base_url"] = base_url
    config_path.write_text(json.dumps(config), encoding="utf-8")
    connector_calls: list[dict[str, Any]] = []
    transport = _Transport()

    result = materialization_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=_enabled_environment(),
        connector=lambda **kwargs: connector_calls.append(kwargs),
        worker_runner=lambda *_: None,
        frappe_transport=transport,
    )

    assert result == 78
    assert connector_calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "case",
    [
        "kill_switch",
        "missing_kill_switch",
        "missing_config",
        "non_local_manifest",
        "unsafe_secret",
        "non_local_endpoint",
        "plaintext_secret_env",
        "timeout_not_below_lease",
    ],
)
def test_preflight_failures_exit_78_before_database_or_http(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    environment = _enabled_environment()
    timeout = 3.0
    if case == "kill_switch":
        environment["GBOS_MATERIALIZATION_KILL_SWITCH"] = "true"
    elif case == "missing_kill_switch":
        environment.pop("GBOS_MATERIALIZATION_KILL_SWITCH")
    elif case == "missing_config":
        config_path = tmp_path / "missing-runtime.json"
    elif case == "non_local_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["mode"] = "production"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "unsafe_secret":
        os.chmod(frappe_secret, 0o640)
    elif case == "non_local_endpoint":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["context_endpoint"]["base_url"] = "http://192.0.2.10:8000"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    elif case == "plaintext_secret_env":
        environment["FRAPPE_API_SECRET"] = "must-not-be-read"
    elif case == "timeout_not_below_lease":
        timeout = 10.0

    connector_calls: list[dict[str, Any]] = []
    transport = _Transport()
    result = materialization_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=environment,
        connector=lambda **kwargs: connector_calls.append(kwargs),
        worker_runner=lambda *_: None,
        frappe_timeout_seconds=timeout,
        frappe_transport=transport,
    )

    assert result == 78
    assert connector_calls == []
    assert transport.calls == []


def test_run_worker_uses_injected_stop_event_and_sleep_only_while_idle() -> None:
    stop_event = Event()
    sleeps: list[float] = []

    class IdleWorker:
        def run_once(self, site_id: str) -> MaterializationRunResult:
            assert site_id == "gbos.localhost"
            return MaterializationRunResult(
                status="idle",
                materialization_id=None,
                attempt=None,
            )

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        stop_event.set()

    materialization_worker.run_worker(
        IdleWorker(),
        site_id="gbos.localhost",
        stop_event=stop_event,
        idle_delay=0.25,
        sleep=sleep,
    )

    assert sleeps == [0.25]


def test_runner_crash_propagates_without_success_and_connection_is_closed(
    tmp_path: Path,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    connection = _Connection()

    def crash(*_: object) -> None:
        raise RuntimeError("safe runner crash")

    with pytest.raises(RuntimeError, match="runner crash"):
        materialization_worker.main(
            manifest_path=manifest_path,
            runtime_config_path=config_path,
            frappe_api_key_path=frappe_key,
            frappe_api_secret_path=frappe_secret,
            environ=_enabled_environment(),
            connector=lambda **_: connection,
            worker_runner=crash,
            frappe_transport=_Transport(),
        )

    assert connection.closed is True
