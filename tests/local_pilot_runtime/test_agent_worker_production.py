from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from services.agent_runtime.agents import DeterministicLocalProvider
from services.agent_runtime.invocations import PostgresModelInvocationRepository
from services.agent_runtime.worker import AgentWorker
from services.local_pilot_runtime import agent_worker
from services.local_pilot_runtime.trusted_phrase_lexicon import (
    TrustedPhraseLexiconError,
    load_trusted_phrase_resolver,
)
from services.model_gateway.provider import DeepSeekAgentProvider
from services.model_gateway.runtime import PostgresMonthlyUsageLedger
from services.model_gateway.tokenization import EncryptedFileMappingVault


class _Cursor:
    def __init__(self) -> None:
        self._query = ""

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, _params: object = None) -> None:
        self._query = query

    def fetchone(self) -> tuple[object, ...] | None:
        if "count(*)" in self._query:
            return (0,)
        if "COALESCE(sum(charge.amount_usd), 0)" in self._query:
            return (Decimal("0"),)
        return None


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.cursor_instance = _Cursor()

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class _NoNetworkTransport(httpx.BaseTransport):
    def handle_request(self, _request: httpx.Request) -> httpx.Response:
        raise AssertionError("production composition must not perform HTTP")


def _private_file(path: Path, value: str | bytes) -> Path:
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _runtime_files(tmp_path: Path) -> tuple[Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    for name, value in (
        ("postgres_password", "db-secret\n"),
        ("agent_api_bearer", "agent-token\n"),
        ("context_api_bearer", "context-token\n"),
        ("context_client_bearer", "context-client-token\n"),
    ):
        _private_file(secret_dir / name, value)
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
            "enabled": True,
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "keychain_ref": "keychain://com.esan.gbos.local-pilot/deepseek",
            "kill_switch": False,
            "thinking_default": "disabled",
            "max_input_tokens": 32768,
            "max_output_tokens": 4096,
            "soft_limit_usd": 50,
            "hard_limit_usd": 100,
        },
    }
    manifest_path = _private_file(tmp_path / "manifest.json", json.dumps(manifest))

    def component(
        enabled: bool,
        kill_switch: bool,
        provider_mode: str = "disabled",
    ) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
            "provider_mode": provider_mode,
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
            "agent_api": component(False, True),
            "context_api": component(False, True),
            "agent_worker": component(True, False, "deepseek"),
            "model_worker": component(False, True),
        },
        "worker": {
            "worker_id": "agent-worker-local-1",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    config_path = _private_file(tmp_path / "runtime.json", json.dumps(config))
    return manifest_path, config_path


def _production_files(tmp_path: Path) -> dict[str, Path]:
    now = datetime.now(UTC)
    lexicon = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "resolver_version": "manual-attestation-v1",
        "approved_by": "local-data-steward",
        "approved_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "names_complete": True,
        "organizations_complete": True,
        "names": ["Private Person Phrase"],
        "organizations": ["Private Organization Phrase"],
    }
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    return {
        "deepseek": _private_file(tmp_path / "deepseek", "deepseek-secret\n"),
        "tokenizer": _private_file(tmp_path / "tokenizer", b"t" * 32),
        "mapping": _private_file(tmp_path / "mapping", b"m" * 32),
        "lexicon": _private_file(tmp_path / "lexicon", json.dumps(lexicon)),
        "vault": vault,
    }


def test_main_composes_exact_production_deepseek_provider_without_injection(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    paths = _production_files(tmp_path)
    monkeypatch.setattr(
        agent_worker, "DEFAULT_DEEPSEEK_API_KEY_FILE", paths["deepseek"], raising=False
    )
    monkeypatch.setattr(
        agent_worker, "DEFAULT_TOKENIZER_HMAC_KEY_FILE", paths["tokenizer"], raising=False
    )
    monkeypatch.setattr(
        agent_worker, "DEFAULT_MAPPING_VAULT_KEY_FILE", paths["mapping"], raising=False
    )
    monkeypatch.setattr(
        agent_worker, "DEFAULT_TRUSTED_PHRASE_LEXICON_FILE", paths["lexicon"], raising=False
    )
    monkeypatch.setattr(agent_worker, "DEFAULT_TOKENIZER_VAULT_ROOT", paths["vault"], raising=False)
    connection = _Connection()
    seen: list[AgentWorker] = []

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_MODEL_KILL_SWITCH": "false",
            "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
        },
        connector=lambda **_: connection,
        worker_runner=lambda worker, _stop, _delay: seen.append(worker),
    )

    assert result == 0
    assert len(seen) == 1
    provider = cast(DeepSeekAgentProvider, cast(Any, seen[0]._executor)._provider)
    assert isinstance(provider, DeepSeekAgentProvider)
    assert provider.provider_version == "deepseek-chat-adapter-v1"
    assert provider.tool_version == "no-tools-v1"
    gateway = cast(Any, provider._gateway)
    tokenizer = cast(Any, provider._tokenizer)
    assert gateway._network_enabled is True
    assert gateway._client.base_url == "https://api.deepseek.com"
    assert isinstance(tokenizer._vault, EncryptedFileMappingVault)
    assert isinstance(gateway._usage_ledger, PostgresMonthlyUsageLedger)
    assert isinstance(
        gateway._audit_recorder.__self__,
        PostgresModelInvocationRepository,
    )
    rendered = repr(provider) + repr(provider._phrase_resolver)
    for forbidden in (
        "deepseek-secret",
        "Private Person Phrase",
        "Private Organization Phrase",
    ):
        assert forbidden not in rendered
    assert connection.closed is True


def test_invalid_transport_factory_fails_before_postgres(tmp_path: Path) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    paths = _production_files(tmp_path)
    connections: list[dict[str, object]] = []

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_MODEL_KILL_SWITCH": "false",
            "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
        },
        connector=lambda **kwargs: connections.append(kwargs),
        worker_runner=lambda *_: None,
        secret_paths=agent_worker.AgentSecretPaths(
            deepseek_api_key=paths["deepseek"],
            tokenizer_hmac_key=paths["tokenizer"],
            mapping_vault_key=paths["mapping"],
        ),
        trusted_phrase_lexicon_path=paths["lexicon"],
        tokenizer_vault_root=paths["vault"],
        transport_factory=object(),  # type: ignore[arg-type]
    )

    assert result == 78
    assert connections == []


@pytest.mark.parametrize("secret_name", ["deepseek", "tokenizer", "mapping", "lexicon"])
@pytest.mark.parametrize("unsafe_kind", ["broad", "symlink"])
def test_private_production_files_fail_before_postgres_or_http(
    tmp_path: Path,
    secret_name: str,
    unsafe_kind: str,
) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    paths = _production_files(tmp_path)
    target = paths[secret_name]
    if unsafe_kind == "broad":
        os.chmod(target, 0o640)
    else:
        link = tmp_path / f"{secret_name}-link"
        link.symlink_to(target)
        paths[secret_name] = link
    connections: list[object] = []
    transports: list[object] = []

    def transport_factory() -> httpx.BaseTransport:
        transports.append(object())
        return _NoNetworkTransport()

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_MODEL_KILL_SWITCH": "false",
            "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
        },
        connector=lambda **_: connections.append(object()),
        worker_runner=lambda *_: None,
        secret_paths=agent_worker.AgentSecretPaths(
            deepseek_api_key=paths["deepseek"],
            tokenizer_hmac_key=paths["tokenizer"],
            mapping_vault_key=paths["mapping"],
        ),
        trusted_phrase_lexicon_path=paths["lexicon"],
        tokenizer_vault_root=paths["vault"],
        transport_factory=transport_factory,
    )

    assert result == 78
    assert connections == []
    assert transports == []


@pytest.mark.parametrize(
    "unsafe_boundary",
    [
        "manifest_endpoint",
        "context_endpoint",
        "model_kill_switch",
        "controlled_egress",
        "vault_root",
    ],
)
def test_closed_boundaries_fail_before_postgres_or_http(
    tmp_path: Path,
    unsafe_boundary: str,
) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    paths = _production_files(tmp_path)
    environment = {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_MODEL_KILL_SWITCH": "false",
        "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
    }
    if unsafe_boundary == "manifest_endpoint":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["deepseek"]["base_url"] = "https://example.invalid"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif unsafe_boundary == "context_endpoint":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["context_endpoint"]["base_url"] = "http://context-api.evil:8001"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    elif unsafe_boundary == "model_kill_switch":
        environment["GBOS_MODEL_KILL_SWITCH"] = "true"
    elif unsafe_boundary == "controlled_egress":
        environment["GBOS_DEEPSEEK_EGRESS_ENABLED"] = "false"
    else:
        root = paths["vault"]
        link = tmp_path / "vault-link"
        link.symlink_to(root, target_is_directory=True)
        paths["vault"] = link
    connections: list[object] = []
    transports: list[object] = []

    def transport_factory() -> httpx.BaseTransport:
        transports.append(object())
        return _NoNetworkTransport()

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ=environment,
        connector=lambda **_: connections.append(object()),
        worker_runner=lambda *_: None,
        secret_paths=agent_worker.AgentSecretPaths(
            deepseek_api_key=paths["deepseek"],
            tokenizer_hmac_key=paths["tokenizer"],
            mapping_vault_key=paths["mapping"],
        ),
        trusted_phrase_lexicon_path=paths["lexicon"],
        tokenizer_vault_root=paths["vault"],
        transport_factory=transport_factory,
    )

    assert result == 78
    assert connections == []
    assert transports == []


def test_injected_deepseek_provider_seam_remains_available(tmp_path: Path) -> None:
    manifest_path, config_path = _runtime_files(tmp_path)
    connection = _Connection()
    providers: list[DeterministicLocalProvider] = []

    def factory(_manifest: object, _config: object) -> DeterministicLocalProvider:
        provider = DeterministicLocalProvider()
        providers.append(provider)
        return provider

    result = agent_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_MODEL_KILL_SWITCH": "false",
            "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
        },
        connector=lambda **_: connection,
        worker_runner=lambda *_: None,
        deepseek_provider_factory=factory,
    )

    assert result == 0
    assert len(providers) == 1
    assert connection.closed is True


def test_agent_phrase_resolver_rechecks_attestation_without_exposing_context(
    tmp_path: Path,
) -> None:
    paths = _production_files(tmp_path)
    value = json.loads(paths["lexicon"].read_text(encoding="utf-8"))
    valid_at = datetime.fromisoformat(value["approved_at"])
    expires_at = datetime.fromisoformat(value["expires_at"])
    moments = [valid_at]
    resolver = load_trusted_phrase_resolver(
        paths["lexicon"],
        expected_site_id="gbos.localhost",
        clock=lambda: moments[0],
    )
    request = SimpleNamespace(
        site_id="gbos.localhost",
        raw_context="Private raw context must stay local",
    )

    assert resolver.agent_phrases(cast(Any, request)) == (
        "Private Person Phrase",
        "Private Organization Phrase",
    )
    moments[0] = expires_at

    with pytest.raises(TrustedPhraseLexiconError) as captured:
        resolver.agent_phrases(cast(Any, request))

    rendered = str(captured.value) + repr(captured.value) + repr(resolver)
    for forbidden in (
        "Private Person Phrase",
        "Private Organization Phrase",
        "Private raw context must stay local",
    ):
        assert forbidden not in rendered
