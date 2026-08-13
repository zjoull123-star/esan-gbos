from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.email_gateway.api import build_email_publication_api
from services.email_gateway.models import IntakeResult
from services.email_gateway.phase1_read import Phase1Mailbox
from services.local_pilot_runtime import email_gateway_api
from services.local_pilot_runtime.email_gateway_api import main
from services.local_pilot_runtime.secret_provider import MountedFileSecretProvider, SecretSpec


def _wire() -> dict[str, Any]:
    digest = "sha256:" + "a" * 64
    return {
        "publication_id": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "site_id": "alpha.example",
        "mailbox_id": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 1,
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "received_at": "2026-08-13T09:00:00Z",
        "evidence_refs": ["EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        "participants": [{"address_role": "from", "identity_ref": "extid:v1:email:" + "A" * 43}],
        "subject_digest": digest,
        "header_digests": {"message_id": digest},
        "publication_revision": 1,
        "idempotency_key": "idem:v1:" + "b" * 64,
    }


def _payload_digest(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class _Intake:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def accept(self, scope: object, publication: object) -> IntakeResult:
        self.calls.append((scope, publication))
        from services.email_gateway.models import PublicationReceipt

        pub = publication
        receipt = PublicationReceipt(
            receipt_ref="EGR-RECEIPT",
            publication_ref=pub.publication_ref,
            site_id=pub.site_id,
            mailbox_ref=pub.mailbox_ref,
            observer_delivery_ref=pub.observer_delivery_ref,
            message_ref="MSG-MESSAGE",
            inbox_item_ref="INB-INBOX",
            payload_digest=pub.payload_digest,
            received_at=pub.received_at,
        )
        return IntakeResult(receipt, object(), object())  # type: ignore[arg-type]


def test_accept_boundary_uses_task1_wire_out_of_band_purpose_digest_and_stable_receipt() -> None:
    intake = _Intake()
    app = build_email_publication_api(
        intake=intake,
        bearer_token="publication-secret",
        auth_ref="observer-email-publication-v1",
    )
    payload = _wire()
    headers = {
        "Authorization": "Bearer publication-secret",
        "X-GBOS-Local-Auth-Ref": "observer-email-publication-v1",
        "X-Site-ID": "alpha.example",
        "X-Processing-Purpose": "observation_processing",
        "X-Payload-Digest": _payload_digest(payload),
        "X-Request-ID": "publication-request-1",
    }

    first = TestClient(app).post(
        "/internal/v1/email-publications/accept", json=payload, headers=headers
    )
    second = TestClient(app).post(
        "/internal/v1/email-publications/accept", json=payload, headers=headers
    )

    assert first.status_code == 200
    assert (
        second.json()
        == first.json()
        == {
            "schema_version": "1.0",
            "receipt_ref": "EGR-RECEIPT",
            "publication_id": payload["publication_id"],
            "payload_digest": headers["X-Payload-Digest"],
        }
    )
    assert first.headers["cache-control"] == "no-store"


def test_accept_rejects_auth_site_purpose_or_digest_before_intake() -> None:
    intake = _Intake()
    app = build_email_publication_api(
        intake=intake,
        bearer_token="publication-secret",
        auth_ref="observer-email-publication-v1",
    )
    payload = _wire()
    base = {
        "Authorization": "Bearer publication-secret",
        "X-GBOS-Local-Auth-Ref": "observer-email-publication-v1",
        "X-Site-ID": "alpha.example",
        "X-Processing-Purpose": "observation_processing",
        "X-Payload-Digest": _payload_digest(payload),
        "X-Request-ID": "publication-request-1",
    }
    for name, value in (
        ("Authorization", "Bearer wrong"),
        ("X-Site-ID", "other.example"),
        ("X-Processing-Purpose", "sales_follow_up"),
        ("X-Payload-Digest", "sha256:" + "0" * 64),
    ):
        response = TestClient(app).post(
            "/internal/v1/email-publications/accept",
            json=payload,
            headers={**base, name: value},
        )
        assert response.status_code in {400, 401, 403}
    assert intake.calls == []


def test_main_defaults_killed_before_database_or_server(tmp_path: Path) -> None:
    calls: list[str] = []

    assert (
        main(
            manifest_path=tmp_path / "missing-manifest.json",
            config_path=tmp_path / "missing-config.json",
            environ={},
            connector=lambda **_: calls.append("db"),
            server_runner=lambda *_args, **_kwargs: calls.append("server"),
        )
        == 78
    )
    assert calls == []


def _private(path: Path, value: str | dict[str, object]) -> Path:
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _enabled_runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "alpha.example",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {},
        "email_gateway": {
            "kill_switch": False,
            "publication_kill_switch": False,
            "external_send": False,
            "mailboxes": [],
        },
    }
    config = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "external_send": False,
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_gateway_app",
            "password_file": "/run/secrets/postgres_email_gateway_password",
            "connect_timeout_seconds": 5,
        },
        "endpoints": {
            "email_gateway_api": "http://email-gateway-api:8004",
            "observer_config_api": "http://observer-api:8003",
        },
        "auth": {
            "email_gateway_data_key_file": "/run/secrets/email_gateway_data_key",
            "email_publication_bearer_file": "/run/secrets/email_publication_bearer",
            "email_publication_auth_ref": "observer-email-publication-v1",
            "email_gateway_bff_bearer_file": "/run/secrets/email_gateway_bff_bearer",
            "email_gateway_bff_auth_ref": "email-gateway-bff-v1",
            "mailbox_projection_bearer_file": "/run/secrets/mailbox_projection_bearer",
            "mailbox_projection_auth_ref": "gateway-mailbox-projection-v1",
            "observer_email_draft_material_bearer_file": (
                "/run/secrets/observer_email_draft_material_bearer"
            ),
            "observer_email_draft_material_auth_ref": ("observer-email-draft-material-v1"),
        },
        "listen": {"host": "0.0.0.0", "port": 8004},
        "components": {
            "email_gateway_api": {"enabled": True, "kill_switch": False},
            "email_gateway_worker": {"enabled": False, "kill_switch": True},
            "email_publication_worker": {"enabled": False, "kill_switch": True},
            "mailbox_config_projection_worker": {"enabled": False, "kill_switch": True},
        },
        "worker": {
            "worker_id": "local-pilot-email-gateway-api",
            "idle_delay_seconds": 1.0,
            "heartbeat_interval_seconds": 5.0,
        },
        "mailboxes": [],
    }
    root = tmp_path / "secrets"
    root.mkdir()
    _private(root / "postgres_email_gateway_password", "postgres-password")
    _private(root / "email_publication_bearer", "publication-secret")
    _private(root / "mailbox_projection_bearer", "mailbox-projection-secret")
    _private(root / "email_gateway_data_key", "ab" * 32)
    return (
        _private(tmp_path / "manifest.json", manifest),
        _private(tmp_path / "config.json", config),
        root,
    )


def _provider(root: Path) -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        root,
        (
            SecretSpec(
                "postgres_email_gateway_password",
                "postgres_email_gateway_password",
                "text",
                16,
                128,
            ),
            SecretSpec("email_publication_bearer", "email_publication_bearer", "text", 16, 4096),
            SecretSpec("email_gateway_bff_bearer", "email_gateway_bff_bearer", "text", 16, 4096),
            SecretSpec("mailbox_projection_bearer", "mailbox_projection_bearer", "text", 16, 4096),
            SecretSpec("email_gateway_data_key", "email_gateway_data_key", "text", 64, 64),
        ),
    )


@pytest.mark.parametrize("unsafe_mode", [None, 0o644])
def test_main_rejects_missing_or_unsafe_bff_secret_before_database_or_server(
    tmp_path: Path, unsafe_mode: int | None
) -> None:
    manifest, config, secret_root = _enabled_runtime(tmp_path)
    if unsafe_mode is not None:
        bff = _private(secret_root / "email_gateway_bff_bearer", "bff-secret-value-1")
        os.chmod(bff, unsafe_mode)
    calls: list[str] = []

    result = main(
        manifest_path=manifest,
        config_path=config,
        emergency_stop_path=tmp_path / "no-emergency-stop",
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_EMAIL_GATEWAY_KILL_SWITCH": "false",
            "GBOS_EXTERNAL_SEND_ENABLED": "false",
        },
        connector=lambda **_: calls.append("db"),
        server_runner=lambda *_args, **_kwargs: calls.append("server"),
        secret_provider=_provider(secret_root),
        internal_network=True,
    )

    assert result == 78
    assert calls == []


def test_main_supplies_distinct_publication_and_bff_credentials_to_application_factory(
    tmp_path: Path,
) -> None:
    manifest, config, secret_root = _enabled_runtime(tmp_path)
    _private(secret_root / "email_gateway_bff_bearer", "bff-secret-value-1")
    captured: dict[str, object] = {}
    connection = type("Connection", (), {"close": lambda self: None})()

    def factory(**kwargs: object) -> FastAPI:
        captured.update(kwargs)
        return FastAPI()

    result = main(
        manifest_path=manifest,
        config_path=config,
        emergency_stop_path=tmp_path / "no-emergency-stop",
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_EMAIL_GATEWAY_KILL_SWITCH": "false",
            "GBOS_EXTERNAL_SEND_ENABLED": "false",
        },
        connector=lambda **_: connection,
        application_factory=factory,
        server_runner=lambda *_args, **_kwargs: None,
        secret_provider=_provider(secret_root),
        internal_network=True,
    )

    assert result == 0
    assert captured["publication_bearer_token"] == "publication-secret"
    assert captured["publication_auth_ref"] == "observer-email-publication-v1"
    assert captured["bff_bearer_token"] == "bff-secret-value-1"
    assert captured["bff_auth_ref"] == "email-gateway-bff-v1"
    assert "mailbox_registry" in captured
    assert "read_repository" in captured
    assert "workflow_repository" in captured
    assert "inbox_operations" in captured
    assert "conversation_service" in captured
    assert "draft_service" in captured
    assert "evidence_authority" in captured
    assert "evidence_client" in captured
    assert isinstance(
        captured["connector_health_reader"],
        email_gateway_api.ObserverConnectorHealthReader,
    )


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        body, self._body = self._body, b""
        return body


class _Opener:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[tuple[object, float]] = []

    def open(self, request: object, *, timeout: float) -> _Response:
        self.calls.append((request, timeout))
        return _Response(self.payload)


def test_connector_health_client_uses_exact_fail_closed_observer_boundary() -> None:
    opener = _Opener(
        {
            "site_id": "alpha.example",
            "data": {"connectors": []},
            "meta": {"request_id": "health-request-1", "schema_version": "1.0"},
        }
    )
    client = email_gateway_api.ObserverConnectorHealthClient(
        bearer_token="mailbox-projection-secret",
        auth_ref="gateway-mailbox-projection-v1",
        opener=opener,
    )

    assert client.fetch(site_id="alpha.example", request_id="health-request-1") == ()
    request, timeout = opener.calls[0]
    assert request.full_url == ("http://observer-api:8003/internal/v1/email-connectors/health")
    assert request.get_method() == "POST"
    assert request.data == b"{}"
    assert request.get_header("Authorization") == "Bearer mailbox-projection-secret"
    assert request.get_header("X-gbos-local-auth-ref") == "gateway-mailbox-projection-v1"
    assert request.get_header("X-site-id") == "alpha.example"
    assert request.get_header("X-processing-purpose") == "email_connector_health_read"
    assert 0 < timeout <= 5


class _HealthClient:
    def __init__(self, connector_ref: str, *, status: str = "enabled") -> None:
        self.connector_ref = connector_ref
        self.status = status
        self.calls: list[dict[str, object]] = []

    def fetch(self, **kwargs: object) -> tuple[dict[str, object], ...]:
        self.calls.append(kwargs)
        return (
            {
                "observer_connector_instance_ref": self.connector_ref,
                "status": self.status,
                "freshness": "fresh",
                "backlog": 0,
                "last_success_at": "2026-08-13T09:00:00Z",
                "safe_error_code": None,
            },
        )


def test_connector_health_reader_correlates_only_by_projected_connector_identity() -> None:
    client = _HealthClient("OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV")
    reader = email_gateway_api.ObserverConnectorHealthReader(client)  # type: ignore[arg-type]
    mailbox = Phase1Mailbox(
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        display_label="Gulf Sales",
        provider_kind="imap_smtp",
        business_mode="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01",
        account_owner_user_ref="owner-01",
        inbound_enabled=True,
        outbound_enabled=False,
        status="active",
        config_revision=1,
    )

    health = reader.read("alpha.example", (mailbox,))

    assert health[0].mailbox_ref == mailbox.mailbox_ref
    assert health[0].mailbox_label == "Gulf Sales"
    assert health[0].status == "healthy"
    assert client.calls[0]["site_id"] == "alpha.example"

    mismatch = email_gateway_api.ObserverConnectorHealthReader(
        _HealthClient(mailbox.mailbox_ref)  # type: ignore[arg-type]
    )
    with pytest.raises(HTTPException) as error:
        mismatch.read("alpha.example", (mailbox,))

    assert error.value.status_code == 503


@pytest.mark.parametrize(
    ("observer_status", "gateway_status"),
    [("error", "degraded"), ("disabled", "unknown")],
)
def test_connector_health_reader_maps_closed_observer_runtime_states(
    observer_status: str, gateway_status: str
) -> None:
    connector_ref = "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    reader = email_gateway_api.ObserverConnectorHealthReader(
        _HealthClient(connector_ref, status=observer_status)  # type: ignore[arg-type]
    )
    mailbox = Phase1Mailbox(
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_connector_instance_ref=connector_ref,
        display_label="Gulf Sales",
        provider_kind="imap_smtp",
        business_mode="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01",
        account_owner_user_ref="owner-01",
        inbound_enabled=True,
        outbound_enabled=False,
        status="active",
        config_revision=1,
    )

    assert reader.read("alpha.example", (mailbox,))[0].status == gateway_status


class _UnavailableHealthClient:
    def fetch(self, **_kwargs: object) -> tuple[object, ...]:
        raise email_gateway_api.RuntimeSupportError("observer unavailable")


def test_connector_health_reader_converts_transport_failure_to_503() -> None:
    connector_ref = "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    reader = email_gateway_api.ObserverConnectorHealthReader(
        _UnavailableHealthClient()  # type: ignore[arg-type]
    )
    mailbox = Phase1Mailbox(
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_connector_instance_ref=connector_ref,
        display_label="Gulf Sales",
        provider_kind="imap_smtp",
        business_mode="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01",
        account_owner_user_ref="owner-01",
        inbound_enabled=True,
        outbound_enabled=False,
        status="active",
        config_revision=1,
    )

    with pytest.raises(HTTPException) as error:
        reader.read("alpha.example", (mailbox,))

    assert error.value.status_code == 503
