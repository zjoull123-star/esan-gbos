from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from services.local_pilot_runtime.webhook import (
    WhatsAppWebhookConfig,
    create_whatsapp_webhook_app,
    main,
)
from services.observer.observer.connectors.whatsapp_cloud import (
    WhatsAppCloudDeliveryAuthenticator,
    WhatsAppCloudDurableReceiver,
)
from services.observer.observer.models import RawDelivery
from services.observer.observer.runtime import LocalPilotRuntimeGuard

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
BODY = b'{"object":"whatsapp_business_account","entry":[]}'
APP_SECRET = "private-app-secret"
VERIFY_TOKEN = "private-verify-token"
PATH = "/webhooks/whatsapp"


def _signature(body: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class DurableAccept:
    def __init__(self, *, disposition: str = "accepted") -> None:
        self.disposition = disposition
        self.calls: list[tuple[RawDelivery, str, datetime, datetime]] = []

    def __call__(
        self,
        delivery: RawDelivery,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        self.calls.append((delivery, nonce, nonce_expires_at, now))
        return self.disposition


def _client(
    accept: DurableAccept,
    *,
    enabled: bool = True,
    kill_switch: bool = False,
    max_body_bytes: int = 1_024,
) -> TestClient:
    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(
            app_secret=APP_SECRET,
            max_body_bytes=max_body_bytes,
        ),
        authenticated_accept=accept,
        clock=lambda: NOW,
    )
    app = create_whatsapp_webhook_app(
        config=WhatsAppWebhookConfig(
            path=PATH,
            verify_token=VERIFY_TOKEN,
            max_body_bytes=max_body_bytes,
        ),
        receiver=receiver,
        guard=LocalPilotRuntimeGuard(
            enabled=enabled,
            kill_switch=kill_switch,
        ),
        clock=lambda: NOW,
    )
    return TestClient(app)


def test_get_challenge_requires_injected_runtime_and_returns_exact_plain_value() -> None:
    client = _client(DurableAccept())

    response = client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123456",
        },
    )

    assert response.status_code == 200
    assert response.content == b"123456"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert VERIFY_TOKEN not in repr(client.app)


def test_post_authenticates_exact_raw_body_then_acknowledges_accept_and_duplicate() -> None:
    accepted = DurableAccept()
    accepted_response = _client(accepted).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )

    duplicate = DurableAccept(disposition="duplicate")
    duplicate_response = _client(duplicate).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )

    assert accepted_response.status_code == 200
    assert accepted_response.json() == {"status": "accepted"}
    assert duplicate_response.status_code == 200
    assert duplicate_response.json() == {"status": "duplicate"}
    delivery = accepted.calls[0][0]
    assert delivery.exact_bytes == BODY
    assert delivery.delivery_id == ("whatsapp-webhook:" + hashlib.sha256(BODY).hexdigest())
    assert len(accepted.calls) == len(duplicate.calls) == 1


def test_post_rejects_oversize_or_bad_signature_before_durable_accept() -> None:
    oversized_accept = DurableAccept()
    oversized = _client(oversized_accept, max_body_bytes=len(BODY) - 1).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )
    unsigned_accept = DurableAccept()
    unsigned = _client(unsigned_accept).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )

    assert oversized.status_code == 413
    assert oversized.json() == {"error": {"code": "payload_too_large"}}
    assert unsigned.status_code == 401
    assert unsigned.json() == {"error": {"code": "authentication_failed"}}
    assert oversized_accept.calls == []
    assert unsigned_accept.calls == []


def test_default_missing_dependencies_kill_switch_and_unknown_paths_fail_closed() -> None:
    default_client = TestClient(create_whatsapp_webhook_app())
    stopped_accept = DurableAccept()
    stopped_client = _client(stopped_accept, kill_switch=True)

    missing = default_client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123456",
        },
    )
    stopped = stopped_client.post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )
    unknown = stopped_client.post("/not-allowed", content=BODY)

    assert missing.status_code == 503
    assert missing.json() == {"error": {"code": "runtime_disabled"}}
    assert stopped.status_code == 503
    assert stopped.json() == {"error": {"code": "runtime_disabled"}}
    assert unknown.status_code == 404
    assert stopped_accept.calls == []
    assert main() == 78


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _entrypoint_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    postgres_password = tmp_path / "postgres-password"
    postgres_password.write_text("not-a-real-password", encoding="utf-8")
    postgres_password.chmod(0o600)
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "alpha.example",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {"enabled": False},
        "channels": {
            name: {
                "enabled": name == "whatsapp",
                "activation_time": "2026-08-08T09:00:00Z" if name == "whatsapp" else None,
                "backfill_history": False,
                **({"credential_ref": None} if name != "media" else {"local_only": True}),
            }
            for name in ("email", "wecom", "whatsapp", "media")
        },
    }
    runtime = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos",
            "user": "gbos_observer_app",
            "password_file": str(postgres_password),
            "connect_timeout_seconds": 2,
        },
        "auth": {
            "agent_api_bearer_file": str(postgres_password),
            "context_api_bearer_file": str(postgres_password),
            "context_client_bearer_file": str(postgres_password),
            "context_auth_ref": "local",
        },
        "context_endpoint": {"base_url": "http://context-api:8001", "unix_socket": None},
        "listen": {"host": "127.0.0.1", "agent_api_port": 8002, "context_api_port": 8001},
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
            "worker_id": "observer-worker",
            "idle_delay_seconds": 1,
            "heartbeat_interval_seconds": 5,
        },
    }
    credential = tmp_path / "whatsapp.json"
    _private_json(
        credential,
        {
            "instance_id": "wa-primary",
            "team_ref": "team:sales",
            "agent_task_type": "sales",
            "app_secret": "not-a-real-app-secret",
            "verify_token": "not-a-real-token",
            "path": PATH,
            "max_body_bytes": 1_024,
        },
    )
    connectors = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "external_send": False,
        "evidence_cas_root": str(tmp_path / "cas"),
        "channels": {
            name: {
                "enabled": name == "whatsapp",
                "kill_switch": name != "whatsapp",
                "activation_time": "2026-08-08T09:00:00Z" if name == "whatsapp" else None,
                "backfill_history": False,
                "credential_file": str(credential if name == "whatsapp" else tmp_path / name),
            }
            for name in ("email", "wecom", "whatsapp", "media")
        },
    }
    return (
        _private_json(tmp_path / "manifest.json", manifest),
        _private_json(tmp_path / "runtime.json", runtime),
        _private_json(tmp_path / "connectors.json", connectors),
    )


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Storage:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.registered: list[tuple[object, ...]] = []

    def register_connector_instance(self, scope: object, key: object, **kwargs: object) -> None:
        if self.conflict:
            raise ValueError("routing metadata conflict")
        self.registered.append((scope, key, kwargs))

    def accept_authenticated_delivery(self, *args: object, **kwargs: object) -> object:
        class Accepted:
            disposition = "accepted"

        return Accepted()


def test_main_preflights_then_composes_durable_whatsapp_runtime(
    tmp_path: Path,
) -> None:
    manifest, runtime, connectors = _entrypoint_files(tmp_path)
    connection = _Connection()
    storage = _Storage()
    server_calls: list[tuple[object, ...]] = []

    result = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: connection,
        storage_factory=lambda _connection: storage,
        server_runner=lambda app, **kwargs: server_calls.append((app, kwargs)),
        clock=lambda: NOW,
    )

    assert result == 0
    assert len(storage.registered) == 1
    assert len(server_calls) == 1
    assert server_calls[0][1] == {
        "host": "0.0.0.0",
        "port": 8000,
        "network_mode": "internal_network",
    }
    assert connection.closed is True


def test_main_rejects_plaintext_secret_and_routing_conflict_without_serving(
    tmp_path: Path,
) -> None:
    manifest, runtime, connectors = _entrypoint_files(tmp_path)
    database_calls: list[object] = []
    server_calls: list[object] = []

    plaintext = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
            "WHATSAPP_APP_SECRET": "forbidden",
        },
        connector=lambda **kwargs: database_calls.append(kwargs),
        server_runner=lambda *args, **kwargs: server_calls.append((args, kwargs)),
        clock=lambda: NOW,
    )
    conflict = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: _Connection(),
        storage_factory=lambda _connection: _Storage(conflict=True),
        server_runner=lambda *args, **kwargs: server_calls.append((args, kwargs)),
        clock=lambda: NOW,
    )

    assert plaintext == 78
    assert database_calls == []
    assert conflict == 78
    assert server_calls == []
