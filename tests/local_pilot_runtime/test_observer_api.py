from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.local_pilot_runtime import observer_api
from services.local_pilot_runtime.runtime_support import SecretValue
from services.observer.observer.email_draft_material_repository import (
    PostgresEmailDraftMaterialRepository,
)
from services.observer.observer.email_material_retention import (
    EmailMaterialRetentionService,
)
from services.observer.observer.email_material_retention_repository import (
    PostgresEmailMaterialRetentionRepository,
)
from services.observer.observer.email_participant_authority import (
    EmailParticipantAuthorityResolver,
    PostgresEmailParticipantAuthorityRepository,
)
from services.observer.observer.identity_resolution_work import (
    PostgresIdentityResolutionWorkRepository,
)
from services.observer.observer.identity_tokens import HmacSha256IdentityTokenResolver
from services.observer.observer.models import TenantScope


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _QueryCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = iter(rows)
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> _QueryCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self.calls.append((statement, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return next(self.rows, None)


class _QueryConnection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.query_cursor = _QueryCursor(rows)

    def transaction(self) -> _QueryConnection:
        return self

    def cursor(self) -> _QueryCursor:
        return self.query_cursor

    def __enter__(self) -> _QueryConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _CasStore:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[TenantScope, str]] = []

    def read(self, scope: TenantScope, object_ref: str) -> bytes:
        self.calls.append((scope, object_ref))
        return self.content


def _secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    postgres_secret = secret_dir / "postgres_password"
    observer_secret = secret_dir / "observer_bearer"
    cursor_secret = secret_dir / "cursor_hmac_key"
    _secret(postgres_secret, "db-secret")
    _secret(observer_secret, "observer-token")
    _secret(cursor_secret, "c" * 32)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mode": "local_pilot",
                "site_id": "gbos.localhost",
                "production_go": False,
                "local_pilot_go": True,
                "local_pilot_status": "ready",
                "deepseek": {"enabled": False, "kill_switch": True},
            }
        ),
        encoding="utf-8",
    )

    def component(enabled: bool, kill_switch: bool) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
            "provider_mode": "disabled",
            "synthetic_e2e": False,
        }

    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "site_id": "gbos.localhost",
                "postgres": {
                    "host": "127.0.0.1",
                    "port": 55432,
                    "database": "gbos_local_pilot",
                    "user": "gbos_agent_app",
                    "password_file": str(postgres_secret),
                    "connect_timeout_seconds": 3,
                },
                "auth": {
                    "agent_api_bearer_file": str(secret_dir / "missing_agent_bearer"),
                    "context_api_bearer_file": str(secret_dir / "missing_context_bearer"),
                    "context_client_bearer_file": str(secret_dir / "missing_client_bearer"),
                    "context_auth_ref": "observer-auth-v1",
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
                    "agent_worker": component(False, True),
                    "model_worker": component(False, True),
                },
                "worker": {
                    "worker_id": "observer-api-local-1",
                    "idle_delay_seconds": 0.1,
                    "heartbeat_interval_seconds": 1.0,
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, config_path, observer_secret, cursor_secret


def _enable_email_gateway(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["email_gateway"] = {
        "kill_switch": False,
        "publication_kill_switch": False,
        "external_send": False,
        "mailboxes": [],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_observer_runtime_composes_real_health_without_model_projection() -> None:
    runtime = observer_api.build_postgres_runtime(
        connection=_Connection(),
        bearer_token=SecretValue("observer-token"),
        auth_ref="observer-auth-v1",
        cursor_secret=SecretValue("c" * 32),
        bind_host="/run/gbos/sockets/observer.sock",
        network_mode="unix_socket",
    )

    health = TestClient(runtime.app).get("/health")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "runtime_enabled": True,
        "kill_switch": False,
        "safe_reason_code": None,
        "external_send": False,
        "formal_business_commands": False,
        "network_mode": "unix_socket",
        "authenticated_internal_api": True,
    }
    assert runtime.projection_repository is None
    assert runtime.projection_publisher is None
    assert isinstance(
        runtime.identity_resolution_metrics,
        PostgresIdentityResolutionWorkRepository,
    )
    assert runtime.identity_resolution_metrics._connection is runtime.connection
    assert runtime.email_connector_config_repository._connection is runtime.connection
    with pytest.raises(RuntimeError, match="disabled"):
        runtime.outbox._publisher(object(), "event-1", "idem-1")


def test_observer_runtime_injects_reveal_and_draft_cas_services_with_separate_auth(
    tmp_path: Path,
) -> None:
    identity_resolver = HmacSha256IdentityTokenResolver(b"i" * 32)
    runtime = observer_api.build_postgres_runtime(
        connection=_Connection(),
        bearer_token=SecretValue("observer-token"),
        auth_ref="observer-auth-v1",
        cursor_secret=SecretValue("c" * 32),
        mailbox_projection_bearer_token=SecretValue("mailbox-projection-token"),
        mailbox_projection_auth_ref="gateway-mailbox-projection-v1",
        draft_material_bearer_token=SecretValue("draft-material-token"),
        draft_material_auth_ref="observer-email-draft-material-v1",
        identity_resolver=identity_resolver,
        identity_hmac_key=b"i" * 32,
        evidence_cas_root=tmp_path / "cas",
        bind_host="0.0.0.0",
        network_mode="internal_network",
    )

    paths = {route.path for route in runtime.app.routes}
    assert "/internal/v1/bff/evidence/reveal" in paths
    assert "/internal/v1/bff/email-draft-material/save" in paths
    assert "/internal/v1/bff/email-draft-material/finalize" in paths
    assert runtime.evidence_reveal is not None
    assert runtime.email_draft_material is not None
    assert isinstance(
        runtime.email_draft_material_repository,
        PostgresEmailDraftMaterialRepository,
    )
    assert isinstance(
        runtime.email_draft_material._repository,
        PostgresEmailDraftMaterialRepository,
    )
    assert runtime.email_draft_material._repository._connection is runtime.connection
    assert isinstance(runtime.email_material_retention, EmailMaterialRetentionService)
    assert isinstance(
        runtime.email_material_retention_repository,
        PostgresEmailMaterialRetentionRepository,
    )
    assert runtime.email_material_retention_repository._connection is runtime.connection
    assert runtime.email_material_retention._authoritative_registrar is None
    assert runtime.email_material_retention._cas is runtime.email_draft_material._store
    assert "/internal/v1/retention/tombstones/verify" in paths
    assert "/internal/v1/retention/email-material/register" in paths
    resolver = runtime.email_draft_material._participant_resolver
    assert isinstance(resolver, EmailParticipantAuthorityResolver)
    assert isinstance(resolver._repository, PostgresEmailParticipantAuthorityRepository)
    assert resolver._repository._connection is runtime.connection
    assert resolver._identity_resolver is identity_resolver
    assert runtime.email_mailbox_identity is not None
    assert runtime.email_mailbox_identity._identity_resolver is identity_resolver
    assert "/internal/v1/bff/email-mailbox-identity/derive" in paths
    assert runtime.email_address_match is not None
    assert "/internal/v1/email-address-match/attest" in paths
    assert "signing_key=<redacted>" in repr(runtime.email_address_match)


def test_observer_main_preflights_draft_repository_before_runtime_factory_and_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    _enable_email_gateway(manifest_path)
    secret_dir = tmp_path / "gateway-secrets"
    secret_dir.mkdir()
    projection = secret_dir / "mailbox_projection_bearer"
    draft = secret_dir / "draft_material_bearer"
    identity = secret_dir / "identity_hmac_key"
    _secret(projection, "projection-token")
    _secret(draft, "draft-token")
    identity.write_bytes(b"i" * 32)
    identity.chmod(0o600)
    connection = _Connection()
    events: list[str] = []

    monkeypatch.setattr(
        PostgresEmailDraftMaterialRepository,
        "preflight",
        lambda self: events.append("draft_preflight"),
    )
    monkeypatch.setattr(
        PostgresEmailMaterialRetentionRepository,
        "preflight",
        lambda self: events.append("retention_preflight"),
    )

    class Runtime:
        app = FastAPI()

    def build(**_kwargs: object) -> Runtime:
        events.append("factory")
        return Runtime()

    monkeypatch.setattr(observer_api, "build_postgres_runtime", build)
    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        mailbox_projection_bearer_file=projection,
        draft_material_bearer_file=draft,
        identity_hmac_key_file=identity,
        connector=lambda **_kwargs: connection,
        server_runner=lambda *_args, **_kwargs: events.append("server"),
    )

    assert result == 0
    assert events == ["draft_preflight", "retention_preflight", "factory", "server"]
    assert connection.closed is True


def test_address_match_evidence_reader_binds_delivered_publication_site_and_cas() -> None:
    content = b"From: private@example.invalid\nTo: target@example.invalid\n\nbody"
    connection = _QueryConnection(
        [
            (
                "obs:v1:partition:sha256:" + "a" * 64,
                hashlib.sha256(content).hexdigest(),
                len(content),
                "message/rfc822",
            )
        ]
    )
    store = _CasStore(content)
    reader = observer_api.PostgresEmailAddressMatchEvidenceReader(connection, store)
    scope = TenantScope(
        "alpha.example",
        "email_address_identity_confirmation",
    )

    result = reader.read_authorized(
        scope,
        "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        caller_ref="frappe-identity-command",
        purpose="email_address_identity_confirmation",
    )

    assert result == content
    statement, params = connection.query_cursor.calls[1]
    assert "email_message_publication_outbox" in statement
    assert "relay_status = 'delivered'" in statement
    assert "jsonb_array_elements_text" in statement
    assert params == (
        "alpha.example",
        "alpha.example",
        "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
    )
    assert store.calls == [
        (
            scope,
            "obs:v1:partition:sha256:" + "a" * 64,
        )
    ]

    with pytest.raises(PermissionError):
        reader.read_authorized(
            scope,
            "EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
            caller_ref="other-caller",
            purpose="email_address_identity_confirmation",
        )


def test_observer_main_starts_injected_server_and_closes_connection(tmp_path: Path) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    connection = _Connection()
    seen: list[tuple[FastAPI, dict[str, object]]] = []

    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_LISTEN_UNIX_SOCKET": "/run/gbos/sockets/observer.sock",
        },
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        connector=lambda **_: connection,
        server_runner=lambda app, **kwargs: seen.append((app, kwargs)),
    )

    assert result == 0
    assert len(seen) == 1
    assert seen[0][1] == {
        "host": "127.0.0.1",
        "port": 8003,
        "unix_socket": Path("/run/gbos/sockets/observer.sock"),
        "network_mode": "unix_socket",
    }
    assert TestClient(seen[0][0]).get("/health").json()["status"] == "ok"
    assert connection.closed is True


def test_observer_main_requires_distinct_mailbox_projection_secret_before_postgres(
    tmp_path: Path,
) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    _enable_email_gateway(manifest_path)
    connect_calls: list[dict[str, object]] = []

    missing = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        mailbox_projection_bearer_file=tmp_path / "missing-projection-token",
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *_args, **_kwargs: None,
    )

    assert missing == 78
    assert connect_calls == []


@pytest.mark.parametrize("case", ["missing", "short", "long", "symlink", "wrong_mode"])
def test_observer_main_requires_exact_identity_key_before_postgres_when_gateway_enabled(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    _enable_email_gateway(manifest_path)
    secret_dir = tmp_path / "gateway-secrets"
    secret_dir.mkdir()
    projection = secret_dir / "mailbox_projection_bearer"
    draft = secret_dir / "draft_material_bearer"
    identity = secret_dir / "identity_hmac_key"
    _secret(projection, "projection-token")
    _secret(draft, "draft-token")
    if case == "short":
        identity.write_bytes(b"x" * 31)
        identity.chmod(0o600)
    elif case == "long":
        identity.write_bytes(b"x" * 33)
        identity.chmod(0o600)
    elif case == "symlink":
        target = secret_dir / "identity-target"
        target.write_bytes(b"x" * 32)
        target.chmod(0o600)
        identity.symlink_to(target)
    elif case == "wrong_mode":
        identity.write_bytes(b"x" * 32)
        identity.chmod(0o644)
    connect_calls: list[dict[str, object]] = []

    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        mailbox_projection_bearer_file=projection,
        draft_material_bearer_file=draft,
        identity_hmac_key_file=identity,
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *_args, **_kwargs: None,
    )

    assert result == 78
    assert connect_calls == []


def test_observer_main_rejects_identity_key_replacement_during_provider_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    _enable_email_gateway(manifest_path)
    secret_dir = tmp_path / "gateway-secrets"
    secret_dir.mkdir()
    projection = secret_dir / "mailbox_projection_bearer"
    draft = secret_dir / "draft_material_bearer"
    identity = secret_dir / "identity_hmac_key"
    _secret(projection, "projection-token")
    _secret(draft, "draft-token")
    identity.write_bytes(b"x" * 32)
    identity.chmod(0o600)
    connect_calls: list[dict[str, object]] = []
    original_read = os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        payload = original_read(descriptor, size)
        if not replaced and size == 32 and payload == b"x" * 32:
            replacement = secret_dir / "identity-replacement"
            replacement.write_bytes(b"y" * 32)
            replacement.chmod(0o600)
            replacement.replace(identity)
            replaced = True
        return payload

    monkeypatch.setattr(os, "read", replacing_read)
    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        mailbox_projection_bearer_file=projection,
        draft_material_bearer_file=draft,
        identity_hmac_key_file=identity,
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *_args, **_kwargs: None,
    )

    assert replaced is True
    assert result == 78
    assert connect_calls == []


def test_observer_main_defaults_fail_closed_before_postgres_and_do_not_print_secrets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, config_path, _, cursor_secret = _files(tmp_path)
    connect_calls: list[dict[str, Any]] = []

    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true"},
        cursor_secret_file=cursor_secret,
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *_args, **_kwargs: None,
    )

    assert result == 78
    assert connect_calls == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert "observer-token" not in captured.out + captured.err


def test_observer_main_rejects_unsafe_uds_before_postgres(tmp_path: Path) -> None:
    manifest_path, config_path, observer_secret, cursor_secret = _files(tmp_path)
    connect_calls: list[dict[str, Any]] = []

    result = observer_api.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_LISTEN_UNIX_SOCKET": "/run/gbos/observer.sock",
        },
        observer_bearer_file=observer_secret,
        observer_auth_ref="observer-auth-v1",
        cursor_secret_file=cursor_secret,
        connector=lambda **kwargs: connect_calls.append(kwargs),
        server_runner=lambda *_args, **_kwargs: None,
    )

    assert result == 78
    assert connect_calls == []


def test_observer_default_app_is_import_safe_and_not_ready() -> None:
    health = TestClient(observer_api.app).get("/health")

    assert health.status_code == 200
    assert health.json()["status"] == "stopped"
    assert health.json()["runtime_enabled"] is False


def test_observer_module_reload_does_not_touch_database_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted DB or network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)

    importlib.reload(observer_api)
