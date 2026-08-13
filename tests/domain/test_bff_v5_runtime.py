from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return 200, {"data": {"ok": True}, "site_id": "gbos.localhost"}


@pytest.fixture
def gateway_module() -> tuple[Any, SimpleNamespace]:
    fake_frappe = SimpleNamespace(
        conf={
            "gbos_email_gateway_url": "http://email-gateway-api:8004",
            "gbos_email_gateway_token_file": "/run/secrets/email_gateway_bff_bearer",
            "gbos_email_gateway_auth_ref": "email-gateway-bff-v1",
            "gbos_observer_email_material_url": "http://observer-api:8003",
            "gbos_observer_email_material_token_file": (
                "/run/secrets/observer_email_draft_material_bearer"
            ),
            "gbos_observer_email_material_auth_ref": ("observer-email-draft-material-v1"),
        },
        local=SimpleNamespace(site="gbos.localhost"),
    )
    original = sys.modules.get("frappe")
    original_common = sys.modules.pop("esan_gbos.api.v1.common", None)
    sys.modules["frappe"] = fake_frappe
    sys.modules.pop("esan_gbos.api.v5.gateway", None)
    module = importlib.import_module("esan_gbos.api.v5.gateway")
    yield module, fake_frappe
    sys.modules.pop("esan_gbos.api.v5.gateway", None)
    sys.modules.pop("esan_gbos.api.v1.common", None)
    if original_common is not None:
        sys.modules["esan_gbos.api.v1.common"] = original_common
    if original is None:
        sys.modules.pop("frappe", None)
    else:
        sys.modules["frappe"] = original


def token_file(path: Path, *, mode: int = 0o400) -> Path:
    path.write_text("mounted-bearer\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_gateway_client_is_pinned_to_exact_internal_url_and_mounted_bearer(
    gateway_module: tuple[Any, SimpleNamespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, fake = gateway_module
    secrets = tmp_path / "run" / "secrets"
    secrets.mkdir(parents=True)
    path = token_file(secrets / "email_gateway_bff_bearer")
    monkeypatch.setattr(gateway, "_BEARER_FILE", path)
    fake.conf["gbos_email_gateway_token_file"] = str(path)

    client = gateway.configured_gateway_client()
    transport = RecordingTransport()
    client._transport = transport
    client.request(
        method="POST",
        path="/internal/v1/bff/email-admin/mailboxes/list",
        site_id="gbos.localhost",
        purpose="email_mailbox_read",
        request_id="REQ-v5-runtime",
        payload={},
    )

    call = transport.calls[0]
    assert call["url"] == (
        "http://email-gateway-api:8004/internal/v1/bff/email-admin/mailboxes/list"
    )
    assert call["headers"]["Authorization"] == "Bearer mounted-bearer"
    assert call["headers"]["X-GBOS-Local-Auth-Ref"] == "email-gateway-bff-v1"
    assert "mounted-bearer" not in repr(client)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gbos_email_gateway_url", "http://127.0.0.1:8004"),
        ("gbos_email_gateway_url", "https://email-gateway-api:8004"),
        ("gbos_email_gateway_token_file", "/tmp/email_gateway_bff_bearer"),
        ("gbos_email_gateway_token", "inline-secret"),
        ("gbos_email_gateway_auth_ref", ""),
        ("gbos_email_gateway_auth_ref", "other-valid-auth-v1"),
    ],
)
def test_gateway_client_rejects_config_drift_or_inline_secret(
    gateway_module: tuple[Any, SimpleNamespace], field: str, value: str
) -> None:
    gateway, fake = gateway_module
    fake.conf[field] = value

    with pytest.raises(gateway.BFFError, match="configuration is invalid") as raised:
        gateway.configured_gateway_client()

    assert "inline-secret" not in str(raised.value)


def test_gateway_client_rejects_group_readable_bearer(
    gateway_module: tuple[Any, SimpleNamespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, fake = gateway_module
    secrets = tmp_path / "run" / "secrets"
    secrets.mkdir(parents=True)
    path = token_file(secrets / "email_gateway_bff_bearer", mode=0o640)
    monkeypatch.setattr(gateway, "_BEARER_FILE", path)
    fake.conf["gbos_email_gateway_token_file"] = str(path)

    with pytest.raises(gateway.BFFError, match="configuration is invalid"):
        gateway.configured_gateway_client()


def test_observer_email_material_client_uses_exact_separate_mounted_bearer(
    gateway_module: tuple[Any, SimpleNamespace], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, fake = gateway_module
    secrets = tmp_path / "run" / "secrets"
    secrets.mkdir(parents=True)
    path = token_file(secrets / "observer_email_draft_material_bearer")
    monkeypatch.setattr(gateway, "_OBSERVER_MATERIAL_BEARER_FILE", path)
    fake.conf["gbos_observer_email_material_token_file"] = str(path)

    client = gateway.configured_observer_email_material_client()
    transport = RecordingTransport()
    client._transport = transport
    client.request(
        method="POST",
        path="/internal/v1/bff/email-draft-material/save",
        site_id="gbos.localhost",
        purpose="email_draft_material",
        request_id="REQ-v5-draft-material",
        payload={},
    )

    call = transport.calls[0]
    assert call["url"] == ("http://observer-api:8003/internal/v1/bff/email-draft-material/save")
    assert call["headers"]["Authorization"] == "Bearer mounted-bearer"
    assert call["headers"]["X-GBOS-Local-Auth-Ref"] == ("observer-email-draft-material-v1")
    assert "mounted-bearer" not in repr(client)


def test_observer_mailbox_identity_derive_uses_the_existing_mounted_client(
    gateway_module: tuple[Any, SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, _fake = gateway_module
    calls: list[dict[str, Any]] = []
    client = SimpleNamespace(
        request=lambda **kwargs: (
            calls.append(kwargs)
            or {
                "data": {
                    "opaque_address_ref": "extid:v1:email:" + "M" * 43,
                    "normalization_version": "email-v1",
                }
            }
        )
    )
    monkeypatch.setattr(gateway, "configured_observer_email_material_client", lambda: client)

    result = gateway.call_observer(
        path="/internal/v1/bff/email-mailbox-identity/derive",
        purpose="email_mailbox_identity",
        payload={
            "canonical_mailbox_address": "mailbox-raw-sentinel@example.invalid",
            "idempotency_key": "mailbox-create-01",
        },
        idempotency_key="mailbox-create-01",
    )

    assert result["normalization_version"] == "email-v1"
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/internal/v1/bff/email-mailbox-identity/derive"
    assert calls[0]["site_id"] == "gbos.localhost"
    assert calls[0]["purpose"] == "email_mailbox_identity"
    assert calls[0]["payload"] == {
        "canonical_mailbox_address": "mailbox-raw-sentinel@example.invalid",
        "idempotency_key": "mailbox-create-01",
    }
    assert calls[0]["idempotency_key"] == "mailbox-create-01"
