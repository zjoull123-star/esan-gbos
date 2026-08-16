from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from services.local_pilot_runtime import wecom_app_mail_webhook as webhook
from services.local_pilot_runtime.secret_provider import MountedFileSecretProvider
from services.observer.observer.connectors.wecom_app_mail_callback import (
    WeComAppMailCallbackVerifier,
)
from services.observer.observer.runtime import LocalPilotRuntimeGuard

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
TOKEN = "SyntheticCallbackToken"
KEY = bytes(range(32))
AES_KEY = base64.b64encode(KEY).decode().rstrip("=")
CORP_ID = "synthetic-corp"
AGENT_ID = "1000001"
OCI = "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAILBOX = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
SIGNAL_RECEIPT = "ESG-01ARZ3NDEKTSV4RRFFQ69G5FAV"
EVENT_XML = (
    "<xml><ToUserName><![CDATA[synthetic-app-mail]]></ToUserName>"
    "<FromUserName><![CDATA[sys]]></FromUserName>"
    f"<CreateTime><![CDATA[{int(NOW.timestamp())}]]></CreateTime>"
    "<MsgType><![CDATA[event]]></MsgType>"
    "<Event><![CDATA[app_email_change]]></Event>"
    "<ChangeType><![CDATA[receive_email]]></ChangeType>"
    "<Amount><![CDATA[2]]></Amount></xml>"
)


def _encrypt(value: str, *, receiver: str = CORP_ID) -> str:
    content = value.encode()
    framed = b"0123456789abcdef" + struct.pack(">I", len(content)) + content + receiver.encode()
    pad = 32 - len(framed) % 32
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    encrypted = encryptor.update(framed + bytes([pad]) * pad) + encryptor.finalize()
    return base64.b64encode(encrypted).decode()


def _signature(encrypted: str, timestamp: str, nonce: str) -> str:
    joined = "".join(sorted((TOKEN, timestamp, nonce, encrypted)))
    return hashlib.sha1(joined.encode()).hexdigest()  # noqa: S324 - official contract


def _config() -> webhook.WeComAppMailWebhookConfig:
    return webhook.WeComAppMailWebhookConfig(
        site_id="alpha.example",
        observer_connector_instance_ref=OCI,
        mailbox_ref=MAILBOX,
        mailbox_config_revision=7,
        activation_not_before=datetime(2026, 8, 14, 7, tzinfo=UTC),
        corp_id=CORP_ID,
        agent_id=AGENT_ID,
        callback_path="/webhooks/wecom-app-mail",
        observer_signal_url="http://observer-api:8003/internal/v1/email-signals/accept",
        max_body_bytes=65_536,
        max_query_bytes=1_024,
    )


class _SignalClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def accept(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        return {
            "schema_version": "1.0",
            "signal_receipt_ref": SIGNAL_RECEIPT,
            "payload_digest": payload["payload_digest"],
        }


def _app(client: _SignalClient | None = None):
    signal_client = client or _SignalClient()
    verifier = WeComAppMailCallbackVerifier(
        callback_token=TOKEN,
        encoding_aes_key=AES_KEY,
        corp_id=CORP_ID,
        agent_id=AGENT_ID,
    )
    return (
        webhook.create_wecom_app_mail_webhook_app(
            config=_config(),
            verifier=verifier,
            signal_client=signal_client,
            guard=LocalPilotRuntimeGuard(enabled=True, kill_switch=False),
            clock=lambda: NOW,
        ),
        signal_client,
    )


def _event_request() -> tuple[str, str, str, str]:
    encrypted = _encrypt(EVENT_XML)
    timestamp = str(int(NOW.timestamp()))
    nonce = "event-nonce"
    body = (
        "<xml><ToUserName><![CDATA[synthetic-corp]]></ToUserName>"
        "<AgentID><![CDATA[1000001]]></AgentID>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    )
    return encrypted, timestamp, nonce, body


def test_import_default_app_is_inert_and_never_constructs_secrets_client_or_server() -> None:
    client = TestClient(webhook.app)
    get_response = client.get("/webhooks/wecom-app-mail")
    post_response = client.post("/webhooks/wecom-app-mail", content=b"<xml/>")
    touched: list[str] = []

    result = webhook.main(
        environ={},
        secret_provider_factory=lambda *_args, **_kwargs: touched.append("secret"),
        client_factory=lambda **_kwargs: touched.append("client"),
        server_runner=lambda *_args, **_kwargs: touched.append("server"),
    )

    assert get_response.status_code == post_response.status_code == 503
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (get_response, post_response)
    )
    assert result == 78
    assert touched == []


def test_get_returns_raw_challenge_without_quotes_bom_or_newline() -> None:
    encrypted = _encrypt("synthetic-url-verification")
    timestamp = str(int(NOW.timestamp()))
    nonce = "challenge-nonce"
    response = TestClient(_app()[0]).get(
        "/webhooks/wecom-app-mail",
        params={
            "msg_signature": _signature(encrypted, timestamp, nonce),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": encrypted,
        },
    )

    assert response.status_code == 200
    assert response.content == b"synthetic-url-verification"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"


def test_post_decrypts_then_calls_only_closed_signal_api_without_raw_content() -> None:
    app, client = _app()
    encrypted, timestamp, nonce, body = _event_request()
    response = TestClient(app).post(
        "/webhooks/wecom-app-mail",
        params={
            "msg_signature": _signature(encrypted, timestamp, nonce),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        headers={"content-type": "application/xml"},
    )

    assert response.status_code == 200
    assert response.content == b"success"
    assert response.headers["cache-control"] == "no-store"
    assert len(client.payloads) == 1
    payload = client.payloads[0]
    assert payload == {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "signal_kind": "callback",
        "observer_connector_instance_ref": OCI,
        "activation_watermark": {
            "mailbox_id": MAILBOX,
            "mailbox_config_revision": 7,
            "not_before": "2026-08-14T07:00:00Z",
        },
        "count_hint": 2,
        "callback_timestamp": "2026-08-14T08:00:00Z",
        "payload_digest": payload["payload_digest"],
        "nonce_digest": payload["nonce_digest"],
        "replay_key_digest": payload["replay_key_digest"],
        "idempotency_key": "email-signal:"
        + str(payload["replay_key_digest"]).removeprefix("sha256:"),
    }
    rendered = repr(payload)
    for forbidden in (EVENT_XML, body, encrypted, nonce, "mail_id", "cursor", "delivery_id"):
        assert forbidden not in rendered


@pytest.mark.parametrize("method", ["get", "post"])
def test_webhook_rejects_duplicate_unknown_and_oversized_query(method: str) -> None:
    app, client = _app()
    http = TestClient(app)
    suffix = "&echostr=x" if method == "get" else ""
    duplicate_url = (
        f"/webhooks/wecom-app-mail?msg_signature=0&msg_signature=1&timestamp=1&nonce=n{suffix}"
    )
    unknown_url = f"/webhooks/wecom-app-mail?msg_signature=0&timestamp=1&nonce=n{suffix}&unknown=x"
    oversized_url = (
        f"/webhooks/wecom-app-mail?msg_signature=0&timestamp=1&nonce={'n' * 1000}{suffix}"
    )
    call = getattr(http, method)
    kwargs = {"headers": {"content-type": "application/xml"}, "content": b"<xml/>"}
    if method == "get":
        kwargs = {}

    responses = [
        call(duplicate_url, **kwargs),
        call(unknown_url, **kwargs),
        call(oversized_url, **kwargs),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422]
    assert all(response.json() == {"error": {"code": "query_invalid"}} for response in responses)
    assert all(response.headers["cache-control"] == "no-store" for response in responses)
    assert client.payloads == []


def test_post_requires_exact_xml_type_and_streams_a_bounded_body() -> None:
    app, client = _app()
    http = TestClient(app)
    url = "/webhooks/wecom-app-mail?msg_signature=0&timestamp=1&nonce=n"

    wrong = http.post(url, content=b"<xml/>", headers={"content-type": "application/json"})
    parameterized = http.post(
        url,
        content=b"<xml/>",
        headers={"content-type": "application/xml; charset=utf-8"},
    )
    oversized = http.post(
        url,
        content=b"x" * 65_537,
        headers={"content-type": "application/xml"},
    )

    assert wrong.status_code == parameterized.status_code == 415
    assert oversized.status_code == 413
    assert wrong.json() == parameterized.json() == {"error": {"code": "content_type_invalid"}}
    assert oversized.json() == {"error": {"code": "payload_too_large"}}
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (wrong, parameterized, oversized)
    )
    assert client.payloads == []


def test_all_routes_and_callback_failures_are_no_store_stable_and_secret_safe() -> None:
    app, client = _app()
    encrypted, timestamp, nonce, body = _event_request()
    invalid_signature = TestClient(app).post(
        "/webhooks/wecom-app-mail",
        params={"msg_signature": "0" * 40, "timestamp": timestamp, "nonce": nonce},
        content=body,
        headers={"content-type": "application/xml"},
    )
    wrong_path = TestClient(app).post(
        "/webhooks/wecom-app-mail/",
        content=b"<xml/>",
        headers={"content-type": "application/xml"},
        follow_redirects=False,
    )

    assert invalid_signature.status_code == 401
    assert invalid_signature.json() == {"error": {"code": "signature_invalid"}}
    assert wrong_path.status_code == 404
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (invalid_signature, wrong_path)
    )
    rendered = invalid_signature.text + repr(client.payloads)
    for forbidden in (TOKEN, AES_KEY, EVENT_XML, body, encrypted, nonce):
        assert forbidden not in rendered


class _FailingSignalClient:
    def accept(self, _payload: dict[str, object]) -> dict[str, object]:
        raise webhook.WeComAppMailSignalClientError("observer_unavailable", status_code=503)


def test_signal_api_failure_is_stable_safe_and_never_returns_success() -> None:
    app, _client = _app()  # construct the normal app first to preserve helper type coverage
    del app
    configured, _ = _app()  # no network occurs during app construction
    del configured
    verifier = WeComAppMailCallbackVerifier(
        callback_token=TOKEN,
        encoding_aes_key=AES_KEY,
        corp_id=CORP_ID,
        agent_id=AGENT_ID,
    )
    failure_app = webhook.create_wecom_app_mail_webhook_app(
        config=_config(),
        verifier=verifier,
        signal_client=_FailingSignalClient(),
        guard=LocalPilotRuntimeGuard(enabled=True, kill_switch=False),
        clock=lambda: NOW,
    )
    encrypted, timestamp, nonce, body = _event_request()

    response = TestClient(failure_app).post(
        "/webhooks/wecom-app-mail",
        params={
            "msg_signature": _signature(encrypted, timestamp, nonce),
            "timestamp": timestamp,
            "nonce": nonce,
        },
        content=body,
        headers={"content-type": "application/xml"},
    )

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "observer_unavailable"}}
    assert response.headers["cache-control"] == "no-store"
    for forbidden in (TOKEN, AES_KEY, EVENT_XML, body, encrypted, nonce):
        assert forbidden not in response.text


def _signal_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "signal_kind": "callback",
        "observer_connector_instance_ref": OCI,
        "activation_watermark": {
            "mailbox_id": MAILBOX,
            "mailbox_config_revision": 7,
            "not_before": "2026-08-14T07:00:00Z",
        },
        "count_hint": 2,
        "callback_timestamp": "2026-08-14T08:00:00Z",
        "payload_digest": "sha256:" + "1" * 64,
        "nonce_digest": "sha256:" + "2" * 64,
        "replay_key_digest": "sha256:" + "3" * 64,
        "idempotency_key": "email-signal:" + "4" * 64,
    }


class _HttpResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        chunks: tuple[bytes, ...] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = chunks or (
            json.dumps(
                {
                    "schema_version": "1.0",
                    "signal_receipt_ref": SIGNAL_RECEIPT,
                    "payload_digest": "sha256:" + "1" * 64,
                }
            ).encode(),
        )

    def __enter__(self) -> _HttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def iter_bytes(self) -> tuple[bytes, ...]:
        return self._chunks


class _HttpClient:
    def __init__(self, response: _HttpResponse | None = None) -> None:
        self.response = response or _HttpResponse()
        self.calls: list[dict[str, object]] = []

    def stream(self, method: str, url: str, **kwargs: object) -> _HttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def test_observer_signal_client_uses_exact_url_auth_scope_and_idempotency_headers() -> None:
    http = _HttpClient()
    client = webhook.ObserverEmailSignalClient(
        http_client=http,
        bearer_token="signal-bearer-token",
        site_id="alpha.example",
    )
    payload = _signal_payload()

    receipt = client.accept(payload)

    assert receipt == {
        "schema_version": "1.0",
        "signal_receipt_ref": SIGNAL_RECEIPT,
        "payload_digest": payload["payload_digest"],
    }
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://observer-api:8003/internal/v1/email-signals/accept"
    assert call["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer signal-bearer-token",
        "X-GBOS-Local-Auth-Ref": "observer-email-signal-v1",
        "X-Site-ID": "alpha.example",
        "X-Processing-Purpose": "email_signal_accept",
        "X-Request-ID": "email-signal-request-" + "4" * 64,
        "Idempotency-Key": "email-signal:" + "4" * 64,
    }
    assert json.loads(bytes(call["content"])) == payload


@pytest.mark.parametrize(
    "response",
    [
        _HttpResponse(status_code=302),
        _HttpResponse(headers={"content-type": "text/plain"}),
        _HttpResponse(chunks=(b"{" + b"x" * 16_384, b"}")),
        _HttpResponse(
            chunks=(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "signal_receipt_ref": SIGNAL_RECEIPT,
                        "payload_digest": "sha256:" + "9" * 64,
                    }
                ).encode(),
            )
        ),
    ],
)
def test_observer_signal_client_rejects_status_type_size_and_receipt_drift(
    response: _HttpResponse,
) -> None:
    client = webhook.ObserverEmailSignalClient(
        http_client=_HttpClient(response),
        bearer_token="signal-bearer-token",
        site_id="alpha.example",
    )

    with pytest.raises(webhook.WeComAppMailSignalClientError) as caught:
        client.accept(_signal_payload())

    assert caught.value.code == "observer_response_invalid"
    assert "9" * 64 not in repr(caught.value)


def _write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _manifest_value(*, site_id: str = "alpha.example") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": site_id,
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
        "deepseek": {"enabled": False},
    }


def _runtime_value(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "enabled": True,
        "kill_switch": False,
        "external_send": False,
        "site_id": "alpha.example",
        "observer_connector_instance_ref": OCI,
        "mailbox_ref": MAILBOX,
        "mailbox_config_revision": 7,
        "activation_not_before": "2026-08-14T07:00:00Z",
        "corp_id": CORP_ID,
        "agent_id": AGENT_ID,
        "callback_path": "/webhooks/wecom-app-mail",
        "observer_signal_url": "http://observer-api:8003/internal/v1/email-signals/accept",
        "max_body_bytes": 65_536,
        "max_query_bytes": 1_024,
    }
    value.update(changes)
    return value


def _write_runtime_files(
    tmp_path: Path,
    *,
    config_value: Mapping[str, object] | None = None,
    manifest_value: Mapping[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(config_value or _runtime_value()), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_value or _manifest_value()), encoding="utf-8")
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    _write_secret(secret_dir / "wecom_app_mail_callback_token", TOKEN)
    _write_secret(secret_dir / "wecom_app_mail_callback_aes_key", AES_KEY)
    _write_secret(secret_dir / "observer_email_signal_bearer", "signal-bearer-token")
    return config, manifest, secret_dir


def _enabled_environment(**changes: str) -> dict[str, str]:
    value = {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_WECOM_APP_MAIL_CALLBACK_ENABLED": "true",
        "GBOS_WECOM_APP_MAIL_CALLBACK_KILL_SWITCH": "false",
        "GBOS_GLOBAL_KILL_SWITCH": "false",
        "GBOS_EXTERNAL_SEND_ENABLED": "false",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("environment_change", "config_change"),
    [
        ({"GBOS_LOCAL_RUNTIME_ENABLED": "false"}, {}),
        ({"GBOS_WECOM_APP_MAIL_CALLBACK_ENABLED": "false"}, {}),
        ({"GBOS_WECOM_APP_MAIL_CALLBACK_KILL_SWITCH": "true"}, {}),
        ({"GBOS_GLOBAL_KILL_SWITCH": "true"}, {}),
        ({"GBOS_EXTERNAL_SEND_ENABLED": "true"}, {}),
        ({}, {"enabled": False}),
        ({}, {"kill_switch": True}),
        ({}, {"external_send": True}),
    ],
)
def test_disabled_and_stopped_main_never_constructs_secrets_client_or_server(
    tmp_path: Path,
    environment_change: dict[str, str],
    config_change: dict[str, object],
) -> None:
    config, manifest, secret_dir = _write_runtime_files(
        tmp_path,
        config_value=_runtime_value(**config_change),
    )
    touched: list[str] = []

    result = webhook.main(
        config_path=config,
        manifest_path=manifest,
        secret_root=secret_dir,
        environ=_enabled_environment(**environment_change),
        secret_provider_factory=lambda *_args, **_kwargs: touched.append("secret"),
        client_factory=lambda **_kwargs: touched.append("client"),
        server_runner=lambda *_args, **_kwargs: touched.append("server"),
    )

    assert result == 78
    assert touched == []


@pytest.mark.parametrize(
    "config_value",
    [
        {**_runtime_value(), "unknown": True},
        _runtime_value(observer_signal_url="http://observer-api:8003/other"),
        _runtime_value(callback_path="/webhooks/other"),
        _runtime_value(site_id="other.example"),
    ],
)
def test_nonsecret_config_and_manifest_binding_fail_before_secret_access(
    tmp_path: Path,
    config_value: Mapping[str, object],
) -> None:
    config, manifest, secret_dir = _write_runtime_files(tmp_path, config_value=config_value)
    touched: list[str] = []

    result = webhook.main(
        config_path=config,
        manifest_path=manifest,
        secret_root=secret_dir,
        environ=_enabled_environment(),
        secret_provider_factory=lambda *_args, **_kwargs: touched.append("secret"),
        client_factory=lambda **_kwargs: touched.append("client"),
        server_runner=lambda *_args, **_kwargs: touched.append("server"),
    )

    assert result == 78
    assert touched == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("wecom_app_mail_callback_token", "not-valid-token"),
        ("wecom_app_mail_callback_aes_key", "!" + "A" * 42),
        ("observer_email_signal_bearer", " leading-space"),
    ],
)
def test_invalid_exact_callback_secrets_fail_before_client_or_server(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    config, manifest, secret_dir = _write_runtime_files(tmp_path)
    _write_secret(secret_dir / name, value)
    touched: list[str] = []

    result = webhook.main(
        config_path=config,
        manifest_path=manifest,
        secret_root=secret_dir,
        environ=_enabled_environment(),
        client_factory=lambda **_kwargs: touched.append("client"),
        server_runner=lambda *_args, **_kwargs: touched.append("server"),
    )

    assert result == 78
    assert touched == []


def test_enabled_main_preflights_nonsecrets_and_exact_three_secrets_before_client_server(
    tmp_path: Path,
) -> None:
    config, manifest, secret_dir = _write_runtime_files(tmp_path)
    order: list[str] = []

    class _TrackingProvider:
        def __init__(self, delegate: MountedFileSecretProvider) -> None:
            self._delegate = delegate

        def read_text(self, name: str):
            order.append(f"read:{name}")
            return self._delegate.read_text(name)

    def provider_factory(*args: Any, **kwargs: Any) -> _TrackingProvider:
        order.append("provider")
        return _TrackingProvider(MountedFileSecretProvider(*args, **kwargs))

    class _ConstructedHttpClient:
        def __init__(self, **kwargs: Any) -> None:
            order.append("client")
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def stream(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("no request is made during construction")

        def close(self) -> None:
            order.append("close")

    def server_runner(*_args: Any, **kwargs: Any) -> None:
        order.append("server")
        assert kwargs == {
            "host": "0.0.0.0",
            "port": 8005,
            "network_mode": "internal_network",
        }

    result = webhook.main(
        config_path=config,
        manifest_path=manifest,
        secret_root=secret_dir,
        environ=_enabled_environment(),
        secret_provider_factory=provider_factory,
        client_factory=lambda **kwargs: _ConstructedHttpClient(**kwargs),
        server_runner=server_runner,
        clock=lambda: NOW,
    )

    assert result == 0
    assert order == [
        "provider",
        "read:wecom_app_mail_callback_token",
        "read:wecom_app_mail_callback_aes_key",
        "read:observer_email_signal_bearer",
        "client",
        "server",
        "close",
    ]
