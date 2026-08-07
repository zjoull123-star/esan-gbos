from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from threading import Event
from typing import Any

from services.agent_runtime.worker import AgentWorker, ThreadedHeartbeatRunner
from services.local_pilot_runtime import agent_worker, model_worker


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _files(
    tmp_path: Path,
    *,
    agent_mode: str = "deterministic",
    model_enabled: bool = False,
) -> tuple[Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name, value in (
        ("postgres_password", "db-secret"),
        ("agent_api_bearer", "agent-token"),
        ("context_api_bearer", "context-token"),
        ("context_client_bearer", "context-client-token"),
    ):
        _secret(secret_dir / name, value)
    deepseek_enabled = agent_mode == "deepseek" or model_enabled
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "capabilities": {
            "kingdee": False,
            "cloud_server": False,
            "cloud_business_storage": False,
            "external_send": False,
            "formal_business_commands": False,
        },
        "deepseek": {
            "enabled": deepseek_enabled,
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "keychain_ref": (
                "keychain://com.esan.gbos.local-pilot/deepseek" if deepseek_enabled else None
            ),
            "kill_switch": not deepseek_enabled,
            "thinking_default": "disabled",
            "max_input_tokens": 32768,
            "max_output_tokens": 4096,
            "soft_limit_usd": 50,
            "hard_limit_usd": 100,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def component(
        enabled: bool,
        kill_switch: bool,
        provider_mode: str = "disabled",
        synthetic_e2e: bool = False,
    ) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
            "provider_mode": provider_mode,
            "synthetic_e2e": synthetic_e2e,
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
            "agent_api": component(False, True),
            "context_api": component(False, True),
            "agent_worker": component(
                True,
                False,
                agent_mode,
                agent_mode == "deterministic",
            ),
            "model_worker": component(
                model_enabled,
                not model_enabled,
                "deepseek",
            ),
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


def test_agent_worker_main_composes_deterministic_worker_stop_event_and_heartbeat(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path)
    connection = _Connection()
    seen: list[tuple[AgentWorker, Event, float]] = []

    def runner(worker: AgentWorker, stop_event: Event, idle_delay: float) -> None:
        seen.append((worker, stop_event, idle_delay))
        stop_event.set()

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        connector=lambda **_: connection,
        worker_runner=runner,
    )

    assert result == 0
    assert len(seen) == 1
    worker, stop_event, idle_delay = seen[0]
    assert isinstance(worker, AgentWorker)
    assert isinstance(worker._heartbeat_runner, ThreadedHeartbeatRunner)
    assert stop_event.is_set()
    assert idle_delay == 0.1
    assert connection.closed is True


def test_agent_worker_deepseek_mode_without_factory_fails_before_postgres(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path, agent_mode="deepseek")
    calls: list[dict[str, Any]] = []

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        connector=lambda **kwargs: calls.append(kwargs),
        worker_runner=lambda *_: None,
    )

    assert result == 78
    assert calls == []


def test_model_worker_requires_injected_controlled_runner(tmp_path: Path) -> None:
    manifest_path, config_path = _files(tmp_path, model_enabled=True)
    environment = {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_MODEL_KILL_SWITCH": "false",
    }
    assert (
        model_worker.main(
            manifest_path=manifest_path,
            runtime_config_path=config_path,
            environ=environment,
        )
        == 78
    )
    seen: list[tuple[dict[str, object], object, Event]] = []

    result = model_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ=environment,
        deepseek_runner=lambda manifest, config, stop: seen.append((manifest, config, stop)),
    )

    assert result == 0
    assert len(seen) == 1
    assert seen[0][0]["site_id"] == "gbos.localhost"
    assert seen[0][2].is_set() is False


def test_model_worker_rejects_non_exact_deepseek_manifest_before_runner(
    tmp_path: Path,
) -> None:
    manifest_path, config_path = _files(tmp_path, model_enabled=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["deepseek"]["model"] = "deepseek-chat"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls: list[object] = []

    result = model_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_MODEL_KILL_SWITCH": "false",
        },
        deepseek_runner=lambda *args: calls.append(args),
    )

    assert result == 78
    assert calls == []


def test_all_runtime_modules_are_import_safe_without_db_or_network(
    monkeypatch: Any,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted DB or network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)
    for name in (
        "services.local_pilot_runtime.runtime_support",
        "services.local_pilot_runtime.agent_api",
        "services.local_pilot_runtime.context_api",
        "services.local_pilot_runtime.agent_worker",
        "services.local_pilot_runtime.model_worker",
    ):
        importlib.reload(importlib.import_module(name))
