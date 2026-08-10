from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import services.local_pilot_runtime.retention_worker as retention_runtime
from services.local_pilot_runtime.retention_worker import main

NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)


def _manifest(path: Path, *, enabled: bool = True) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mode": "local_pilot",
                "site_id": "alpha.example",
                "retention_days": 30,
                "production_go": False,
                "local_pilot_go": enabled,
                "local_pilot_status": "ready" if enabled else "disabled",
                "deepseek": {"enabled": False, "kill_switch": True},
            }
        ),
        encoding="utf-8",
    )
    return path


def _runtime_config(path: Path) -> Path:
    secret = path.parent / "postgres-password"
    secret.write_text("private-password", encoding="utf-8")
    os.chmod(secret, 0o600)
    value = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos",
            "user": "gbos_observer_app",
            "password_file": str(secret),
            "connect_timeout_seconds": 3,
        },
        "auth": {
            "agent_api_bearer_file": "/run/secrets/agent",
            "context_api_bearer_file": "/run/secrets/context",
            "context_client_bearer_file": "/run/secrets/client",
            "context_auth_ref": "context-auth-v1",
        },
        "context_endpoint": {
            "base_url": "http://context-api:8081",
            "unix_socket": None,
        },
        "listen": {
            "host": "0.0.0.0",
            "agent_api_port": 8080,
            "context_api_port": 8081,
        },
        "components": {
            name: {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            }
            for name in ("agent_api", "context_api", "agent_worker", "model_worker")
        },
        "worker": {
            "worker_id": "retention-worker-1",
            "idle_delay_seconds": 1,
            "heartbeat_interval_seconds": 5,
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_entrypoint_is_default_off_before_config_or_connection(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    assert (
        main(
            runtime_config_path=tmp_path / "absent.json",
            environ={},
            runner=lambda **kwargs: calls.append(kwargs),
        )
        == 78
    )
    assert calls == []


def test_entrypoint_requires_enabled_manifest_before_runner(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    config = _runtime_config(tmp_path / "runtime.json")
    disabled = _manifest(tmp_path / "manifest.json", enabled=False)

    assert (
        main(
            manifest_path=disabled,
            runtime_config_path=config,
            environ={
                "GBOS_LOCAL_RUNTIME_ENABLED": "true",
                "GBOS_RETENTION_ENABLED": "true",
            },
            runner=lambda **kwargs: calls.append(kwargs),
            clock=lambda: NOW,
        )
        == 78
    )
    assert calls == []


def test_entrypoint_defaults_to_dry_run_and_uses_bounded_batch(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    config = _runtime_config(tmp_path / "runtime.json")
    manifest = _manifest(tmp_path / "manifest.json")

    assert (
        main(
            manifest_path=manifest,
            runtime_config_path=config,
            environ={
                "GBOS_LOCAL_RUNTIME_ENABLED": "true",
                "GBOS_RETENTION_ENABLED": "true",
            },
            runner=lambda **kwargs: calls.append(kwargs),
            clock=lambda: NOW,
        )
        == 0
    )

    assert len(calls) == 1
    assert calls[0]["site_id"] == "alpha.example"
    assert calls[0]["worker_id"] == "retention-worker-1"
    assert calls[0]["batch_size"] == 100
    assert calls[0]["dry_run"] is True
    assert calls[0]["now"] == NOW


def test_execute_requires_separate_explicit_dry_run_override(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    config = _runtime_config(tmp_path / "runtime.json")
    manifest = _manifest(tmp_path / "manifest.json")

    assert (
        main(
            manifest_path=manifest,
            runtime_config_path=config,
            environ={
                "GBOS_LOCAL_RUNTIME_ENABLED": "true",
                "GBOS_RETENTION_ENABLED": "true",
                "GBOS_RETENTION_DRY_RUN": "false",
                "GBOS_RETENTION_BATCH_SIZE": "7",
            },
            runner=lambda **kwargs: calls.append(kwargs),
            clock=lambda: NOW,
        )
        == 0
    )

    assert calls[0]["dry_run"] is False
    assert calls[0]["batch_size"] == 7


def test_entrypoint_fails_closed_for_ambiguous_flags_or_unbounded_batch(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path / "runtime.json")
    manifest = _manifest(tmp_path / "manifest.json")

    for environment in (
        {"GBOS_LOCAL_RUNTIME_ENABLED": "true", "GBOS_RETENTION_ENABLED": "TRUE"},
        {
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_RETENTION_ENABLED": "true",
            "GBOS_RETENTION_DRY_RUN": "0",
        },
        {
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_RETENTION_ENABLED": "true",
            "GBOS_RETENTION_BATCH_SIZE": "1001",
        },
    ):
        assert (
            main(
                manifest_path=manifest,
                runtime_config_path=config,
                environ=environment,
                runner=lambda **_kwargs: None,
            )
            == 78
        )


def test_default_composition_uses_only_runtime_observer_role_and_local_roots(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config = _runtime_config(tmp_path / "runtime.json")
    manifest = _manifest(tmp_path / "manifest.json")
    cas = tmp_path / "cas"
    vault = tmp_path / "vault"
    cas.mkdir()
    vault.mkdir()
    key = tmp_path / "vault.key"
    key.write_bytes(b"k" * 32)
    os.chmod(key, 0o600)
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        retention_runtime,
        "connect_postgres",
        lambda settings, **_kwargs: calls.append(("connect", settings.user)) or object(),
    )
    monkeypatch.setattr(retention_runtime, "close_connection", lambda _value: None)
    monkeypatch.setattr(
        retention_runtime,
        "ContentAddressedEvidenceStore",
        lambda root: calls.append(("cas", root)) or object(),
    )
    monkeypatch.setattr(
        retention_runtime.EncryptedFileMappingVault,
        "from_key_file",
        lambda **kwargs: calls.append(("vault", kwargs["root"])) or object(),
    )
    monkeypatch.setattr(retention_runtime, "PostgresRetentionStorage", lambda _value: object())

    class _Service:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self, _scope: object, **_kwargs: object) -> object:
            calls.append(("run", True))
            return object()

    monkeypatch.setattr(retention_runtime, "RetentionService", _Service)

    assert (
        main(
            manifest_path=manifest,
            runtime_config_path=config,
            mapping_vault_key_file=key,
            evidence_cas_root=cas,
            tokenizer_vault_root=vault,
            environ={
                "GBOS_LOCAL_RUNTIME_ENABLED": "true",
                "GBOS_RETENTION_ENABLED": "true",
            },
            clock=lambda: NOW,
        )
        == 0
    )
    assert calls == [
        ("connect", "gbos_observer_app"),
        ("cas", cas),
        ("vault", vault),
        ("run", True),
    ]
