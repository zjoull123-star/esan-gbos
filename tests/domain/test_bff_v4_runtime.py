from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from esan_gbos.api.v4.client import (
    LocalServiceClient,
    LocalServiceError,
    read_bounded_json,
)
from esan_gbos.domain.v4_dto import (
    V4DTOValidationError,
    map_communication_detail,
    map_model_usage,
    validate_connector_command,
    validate_period,
)


class RecordingTransport:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        status: int = 200,
    ) -> None:
        self.response = response or {"data": {"ok": True}}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.status, self.response


class ByteResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int) -> bytes:
        return self.payload[:size]


@pytest.fixture
def gateway_module() -> tuple[Any, SimpleNamespace]:
    fake_frappe = SimpleNamespace(
        conf={},
        local=SimpleNamespace(site="gbos.localhost"),
    )
    original_frappe = sys.modules.get("frappe")
    original_common = sys.modules.pop("esan_gbos.api.v1.common", None)
    original_gateway = sys.modules.pop("esan_gbos.api.v4.gateway", None)
    sys.modules["frappe"] = fake_frappe
    module = importlib.import_module("esan_gbos.api.v4.gateway")
    yield module, fake_frappe
    sys.modules.pop("esan_gbos.api.v4.gateway", None)
    sys.modules.pop("esan_gbos.api.v1.common", None)
    if original_gateway is not None:
        sys.modules["esan_gbos.api.v4.gateway"] = original_gateway
    if original_common is not None:
        sys.modules["esan_gbos.api.v1.common"] = original_common
    if original_frappe is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original_frappe


def _site_config(
    fake_frappe: SimpleNamespace,
    *,
    token_file: Path | None = None,
    inline_token: str | None = None,
    developer_mode: bool = False,
    base_url: str = "http://127.0.0.1:8091",
) -> None:
    fake_frappe.conf = {
        "developer_mode": developer_mode,
        "gbos_observer_url": base_url,
        "gbos_observer_auth_ref": "observer-token-v1",
    }
    if token_file is not None:
        fake_frappe.conf["gbos_observer_token_file"] = str(token_file)
    if inline_token is not None:
        fake_frappe.conf["gbos_observer_token"] = inline_token


def _token_file(path: Path, value: bytes = b"file-token\n", *, mode: int = 0o400) -> Path:
    path.write_bytes(value)
    os.chmod(path, mode)
    return path


def test_configured_client_reads_token_from_strict_secret_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token")
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(fake_frappe, token_file=token_path)

    client = gateway.configured_client("Observer")
    transport = RecordingTransport()
    client._transport = transport
    client.request(
        method="GET",
        path="/internal/v1/bff/connectors",
        site_id="gbos.localhost",
        purpose="connector_read",
        request_id="REQ-local-001",
    )

    assert transport.calls[0]["headers"]["Authorization"] == "Bearer file-token"
    assert "file-token" not in repr(client)


def test_configured_client_rejects_inline_token_outside_developer_mode(
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    _site_config(fake_frappe, inline_token="legacy-dev-token")

    with pytest.raises(gateway.BFFError, match="configuration is invalid") as raised:
        gateway.configured_client("Observer")

    assert raised.value.status == 503
    assert "legacy-dev-token" not in str(raised.value)
    assert "legacy-dev-token" not in repr(raised.value)


def test_configured_client_allows_inline_token_only_in_explicit_developer_mode(
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    _site_config(
        fake_frappe,
        inline_token="legacy-dev-token",
        developer_mode=True,
    )

    client = gateway.configured_client("Observer")
    transport = RecordingTransport()
    client._transport = transport
    client.request(
        method="GET",
        path="/internal/v1/bff/connectors",
        site_id="gbos.localhost",
        purpose="connector_read",
        request_id="REQ-local-001",
    )

    assert transport.calls[0]["headers"]["Authorization"] == "Bearer legacy-dev-token"


def test_configured_client_rejects_token_file_and_inline_token_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token")
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(
        fake_frappe,
        token_file=token_path,
        inline_token="conflicting-inline-token",
        developer_mode=True,
    )

    with pytest.raises(gateway.BFFError, match="configuration is invalid") as raised:
        gateway.configured_client("Observer")

    assert "conflicting-inline-token" not in str(raised.value)
    assert "file-token" not in repr(raised.value)


@pytest.mark.parametrize(
    ("case", "mode", "payload"),
    [
        ("group-readable", 0o640, b"file-token\n"),
        ("oversize", 0o400, b"x" * 4097),
        ("multiple-lines", 0o400, b"first\nsecond\n"),
        ("carriage-return", 0o400, b"first\rsecond"),
        ("invalid-utf8", 0o400, b"\xff"),
        ("empty", 0o400, b""),
    ],
)
def test_configured_client_rejects_unsafe_token_file_contents_or_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
    case: str,
    mode: int,
    payload: bytes,
) -> None:
    del case
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token", payload, mode=mode)
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(fake_frappe, token_file=token_path)

    with pytest.raises(gateway.BFFError, match="configuration is invalid") as raised:
        gateway.configured_client("Observer")

    assert "first" not in str(raised.value)
    assert "file-token" not in repr(raised.value)


def test_configured_client_accepts_mode_0600_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token", mode=0o600)
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(fake_frappe, token_file=token_path)

    client = gateway.configured_client("Observer")

    assert "file-token" not in repr(client)


def test_configured_client_injects_only_closed_observer_internal_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token")
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(
        fake_frappe,
        token_file=token_path,
        base_url="http://observer-api:8003",
    )

    client = gateway.configured_client("Observer")

    assert "observer-api:8003" in repr(client)
    assert "file-token" not in repr(client)


def test_configured_client_injects_only_closed_agent_internal_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "agent_token")
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    fake_frappe.conf = {
        "gbos_agent_url": "http://agent-api:8002",
        "gbos_agent_token_file": str(token_path),
        "gbos_agent_auth_ref": "agent-token-v1",
    }

    client = gateway.configured_client("Agent")

    assert "agent-api:8002" in repr(client)
    assert "file-token" not in repr(client)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://observer-api.evil:8003",
        "http://observer-api:8002",
        "http://observer-api.:8003",
        "http://user:secret@observer-api:8003",
        "http://observer-api:8003/path",
    ),
)
def test_configured_client_rejects_internal_url_confusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
    base_url: str,
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    token_path = _token_file(secret_dir / "observer_token")
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(fake_frappe, token_file=token_path, base_url=base_url)

    with pytest.raises(gateway.BFFError, match="configuration is invalid"):
        gateway.configured_client("Observer")


def test_configured_client_rejects_service_outside_closed_internal_table(
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    fake_frappe.conf = {
        "developer_mode": True,
        "gbos_evil_url": "http://observer-api:8003",
        "gbos_evil_token": "evil-token",
        "gbos_evil_auth_ref": "evil-ref",
    }

    with pytest.raises(gateway.BFFError, match="not configured") as raised:
        gateway.configured_client("Evil")

    assert "evil-token" not in repr(raised.value)


def test_configured_client_rejects_symlink_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_module: tuple[Any, SimpleNamespace],
) -> None:
    gateway, fake_frappe = gateway_module
    secret_dir = tmp_path / "run" / "secrets"
    secret_dir.mkdir(parents=True)
    target = _token_file(tmp_path / "target")
    token_path = secret_dir / "observer_token"
    token_path.symlink_to(target)
    monkeypatch.setattr(gateway, "_TOKEN_DIRECTORY", secret_dir)
    _site_config(fake_frappe, token_file=token_path)

    with pytest.raises(gateway.BFFError, match="configuration is invalid"):
        gateway.configured_client("Observer")


@pytest.mark.parametrize(
    "token_path",
    (
        "/tmp/observer_token",
        "/run/secrets/nested/observer_token",
        "/run/secrets/../observer_token",
        "/run/secrets/.hidden",
        "relative-token",
    ),
)
def test_configured_client_rejects_token_path_outside_single_safe_secret_filename(
    gateway_module: tuple[Any, SimpleNamespace],
    token_path: str,
) -> None:
    gateway, fake_frappe = gateway_module
    _site_config(fake_frappe, token_file=Path(token_path))

    with pytest.raises(gateway.BFFError, match="configuration is invalid"):
        gateway.configured_client("Observer")


def test_connector_command_is_closed_and_requires_revision_and_strong_key() -> None:
    assert validate_connector_command(
        {
            "instance_id": "wecom:sales",
            "expected_revision": 3,
            "idempotency_key": "pause-connector-001",
        }
    ) == {
        "instance_id": "wecom:sales",
        "expected_revision": 3,
        "idempotency_key": "pause-connector-001",
    }

    with pytest.raises(V4DTOValidationError, match="unexpected"):
        validate_connector_command(
            {
                "instance_id": "wecom:sales",
                "expected_revision": 3,
                "idempotency_key": "pause-connector-001",
                "token": "must-not-pass",
            }
        )
    with pytest.raises(V4DTOValidationError, match="idempotency_key"):
        validate_connector_command(
            {
                "instance_id": "wecom:sales",
                "expected_revision": 3,
                "idempotency_key": "short",
            }
        )


def test_restricted_communication_never_maps_original_text() -> None:
    value = map_communication_detail(
        {
            "observation_id": "OBS-001",
            "channel": "wecom",
            "occurred_at": "2026-08-08T01:00:00+00:00",
            "summary_zh": "客户询问交期。",
            "original_language": "zh",
            "classification": "Restricted",
            "review_status": "Pending",
            "team_ref": "TEM-001",
            "party_ref": "PTY-001",
            "evidence_count": 1,
            "evidence": [{"ref": "EVD-001", "locator": "context://EVD-001"}],
            "fact_proposals": [],
            "association_suggestions": [],
            "model": {"name": "deepseek-v4-flash", "version": "2026-08-01"},
            "raw_access_allowed": True,
            "original_text": "restricted source body",
        }
    )

    assert value["raw_access_allowed"] is False
    assert "original_text" not in value
    assert "restricted source body" not in repr(value)


def test_model_usage_maps_only_the_frozen_budget_units() -> None:
    value = map_model_usage(
        {
            "model": "deepseek-v4-flash",
            "period": "2026-08",
            "tokens": 1200,
            "token_state": "known",
            "cost": {"currency": "USD", "amount": 1.25, "state": "known"},
            "soft_limit_usd": 100.0,
            "hard_limit_usd": 150.0,
            "state": "normal",
        }
    )

    assert value["soft_limit_usd"] == 100.0
    assert value["hard_limit_usd"] == 150.0
    assert value["token_state"] == "known"
    assert set(value) == {
        "model",
        "period",
        "tokens",
        "token_state",
        "cost",
        "soft_limit_usd",
        "hard_limit_usd",
        "state",
    }


@pytest.mark.parametrize(
    "base_url",
    (
        "https://127.0.0.1:8091",
        "http://192.0.2.1:8091",
        "http://user:secret@127.0.0.1:8091",
        "http://127.0.0.1:8091/path",
    ),
)
def test_local_service_client_rejects_non_loopback_or_credentialed_urls(
    base_url: str,
) -> None:
    with pytest.raises(LocalServiceError, match="loopback|URL"):
        LocalServiceClient(
            service_name="Observer",
            base_url=base_url,
            token="local-token",
            auth_ref="observer-token-v1",
            transport=RecordingTransport(),
        )


def test_local_service_client_accepts_only_fixed_safe_unix_socket_directory() -> None:
    client = LocalServiceClient(
        service_name="Observer",
        base_url="unix:///run/gbos/sockets/observer.sock",
        token="local-token",
        auth_ref="observer-token-v1",
        transport=RecordingTransport(),
    )

    assert "local-token" not in repr(client)


def test_local_service_client_rejects_internal_service_dns_by_default() -> None:
    with pytest.raises(LocalServiceError, match="loopback|allowed|URL"):
        LocalServiceClient(
            service_name="Observer",
            base_url="http://observer-api:8003",
            token="local-token",
            auth_ref="observer-token-v1",
            transport=RecordingTransport(),
        )


def test_local_service_client_accepts_only_exact_explicit_internal_url() -> None:
    client = LocalServiceClient(
        service_name="Observer",
        base_url="http://observer-api:8003",
        token="local-token",
        auth_ref="observer-token-v1",
        allowed_internal_urls=frozenset({"http://observer-api:8003"}),
        transport=RecordingTransport(),
    )

    assert "observer-api:8003" in repr(client)
    assert "local-token" not in repr(client)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://observer-api.evil:8003",
        "http://observer-api:8002",
        "http://observer-api.:8003",
        "http://observer_api:8003",
    ),
)
def test_local_service_client_rejects_non_exact_internal_url(
    base_url: str,
) -> None:
    with pytest.raises(LocalServiceError, match="loopback|allowed|URL"):
        LocalServiceClient(
            service_name="Observer",
            base_url=base_url,
            token="local-token",
            auth_ref="observer-token-v1",
            allowed_internal_urls=frozenset({"http://observer-api:8003"}),
            transport=RecordingTransport(),
        )


@pytest.mark.parametrize(
    "base_url",
    (
        "unix:///tmp/observer.sock",
        "unix:///run/gbos/sockets/nested/observer.sock",
        "unix:///run/gbos/sockets/../observer.sock",
        "unix:///run/gbos/sockets/.hidden.sock",
        "unix:///run/gbos/sockets/observer",
        "unix:///run/gbos/sockets/observer%2esock",
    ),
)
def test_local_service_client_rejects_unix_socket_outside_fixed_safe_directory(
    base_url: str,
) -> None:
    with pytest.raises(LocalServiceError, match="Unix|socket"):
        LocalServiceClient(
            service_name="Observer",
            base_url=base_url,
            token="local-token",
            auth_ref="observer-token-v1",
            transport=RecordingTransport(),
        )


def test_local_service_client_sends_governed_scope_without_secret_in_payload() -> None:
    transport = RecordingTransport()
    client = LocalServiceClient(
        service_name="Observer",
        base_url="http://127.0.0.1:8091",
        token="local-token",
        auth_ref="observer-token-v1",
        transport=transport,
        timeout_seconds=2.5,
    )

    result = client.request(
        method="POST",
        path="/internal/v1/bff/connectors/pause",
        site_id="gbos.localhost",
        purpose="connector_control",
        request_id="REQ-local-001",
        payload={"instance_id": "wecom:sales"},
        idempotency_key="pause-connector-001",
    )

    assert result == {"data": {"ok": True}}
    call = transport.calls[0]
    assert call["headers"] == {
        "Authorization": "Bearer local-token",
        "X-GBOS-Local-Auth-Ref": "observer-token-v1",
        "X-Site-ID": "gbos.localhost",
        "X-Processing-Purpose": "connector_control",
        "X-Request-ID": "REQ-local-001",
        "Idempotency-Key": "pause-connector-001",
        "Content-Type": "application/json",
    }
    assert call["payload"] == {"instance_id": "wecom:sales"}
    assert "local-token" not in repr(call["payload"])
    assert call["timeout_seconds"] == 2.5


def test_bounded_json_rejects_oversize_and_non_object_responses() -> None:
    with pytest.raises(LocalServiceError, match="size budget"):
        read_bounded_json(ByteResponse(b"{" + b"x" * 128 + b"}"), max_response_bytes=64)
    with pytest.raises(LocalServiceError, match="JSON object"):
        read_bounded_json(ByteResponse(b"[]"), max_response_bytes=64)


def test_local_service_client_preserves_only_safe_downstream_error_code() -> None:
    client = LocalServiceClient(
        service_name="Observer",
        base_url="http://127.0.0.1:8091",
        token="local-token",
        auth_ref="observer-token-v1",
        transport=RecordingTransport(
            response={"error": {"code": "idempotency_conflict", "raw": "do not surface"}},
            status=409,
        ),
    )

    with pytest.raises(LocalServiceError) as raised:
        client.request(
            method="POST",
            path="/internal/v1/bff/connectors/pause",
            site_id="gbos.localhost",
            purpose="connector_control",
            request_id="REQ-local-001",
            payload={"instance_id": "wecom:sales"},
        )

    assert raised.value.status == 409
    assert raised.value.error_code == "idempotency_conflict"
    assert "do not surface" not in str(raised.value)


@pytest.mark.parametrize("value", ("2026-08", "2024-02"))
def test_model_period_accepts_only_a_real_calendar_month(value: str) -> None:
    assert validate_period(value) == value


@pytest.mark.parametrize("value", ("2026-8", "2026-13", "all", ""))
def test_model_period_rejects_unbounded_queries(value: str) -> None:
    with pytest.raises(V4DTOValidationError, match="period"):
        validate_period(value)
