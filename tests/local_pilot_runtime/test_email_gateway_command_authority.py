from __future__ import annotations

import json
from typing import Any

import pytest

from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import CommandPublication
from services.local_pilot_runtime.email_gateway_command_authority import (
    FRAPPE_EMAIL_COMMAND_AUTHORITY_URL,
    FrappeEmailCommandAuthorityClient,
)
from services.local_pilot_runtime.runtime_support import RuntimeSupportError
from tests.email_gateway.fakes.provider import closed_command


class _Response:
    headers = {"Content-Type": "application/json"}

    def __init__(self, value: object, *, status: int = 200) -> None:
        self.body = json.dumps(value).encode()
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[object, float]] = []

    def open(self, request: object, *, timeout: float) -> _Response:
        self.calls.append((request, timeout))
        return self.response


def _publication(command: dict[str, Any]) -> CommandPublication:
    return CommandPublication(
        publication_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt=2,
        generation=3,
        fence_token="FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + command["payload_sha256"],
    )


def test_client_posts_exact_fenced_payload_and_authority_headers() -> None:
    command = closed_command()
    authority = {"audience": "email-command-executor"}
    opener = _Opener(_Response({"message": {"email_send_authority": authority}}))
    client = FrappeEmailCommandAuthorityClient(
        api_key="authority-key-value",
        api_secret="authority-secret-value",
        auth_ref="email-gateway-authority-v1",
        opener=opener,
    )

    result = client.resolve(
        TenantScope(command["site_id"], command["processing_purpose"]),
        _publication(command),
        command,
    )

    assert result == authority
    request, timeout = opener.calls[0]
    assert request.full_url == FRAPPE_EMAIL_COMMAND_AUTHORITY_URL  # type: ignore[attr-defined]
    assert timeout <= 10
    request_body = json.loads(request.data)  # type: ignore[attr-defined]
    assert set(request_body) == {"payload"}
    assert request_body["payload"] == {
        "site_id": command["site_id"],
        "processing_purpose": "email_gateway_authority",
        "request_id": command["request_id"],
        "auth_ref": "email-gateway-authority-v1",
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "attempt": 2,
        "generation": 3,
        "fence_token": "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "command_ref": command["command_id"],
        "payload_digest": "sha256:" + command["payload_sha256"],
    }
    assert request.get_header("Authorization") == (  # type: ignore[attr-defined]
        "token authority-key-value:authority-secret-value"
    )
    assert request.get_header("Host") == command["site_id"]  # type: ignore[attr-defined]
    assert request.get_header("X-gbos-frappe-auth-ref") == (  # type: ignore[attr-defined]
        "email-gateway-authority-v1"
    )
    assert request.get_header("X-processing-purpose") == (  # type: ignore[attr-defined]
        "email_gateway_authority"
    )
    assert "authority-secret-value" not in repr(client)


@pytest.mark.parametrize(
    "body",
    [
        {"email_send_authority": {}},
        {"message": {"email_send_authority": {}, "extra": True}},
        {"message": {"email_send_authority": {}}, "extra": True},
        {"message": {"email_send_authority": []}},
    ],
)
def test_client_rejects_frappe_envelope_shape_drift(body: object) -> None:
    command = closed_command()
    client = FrappeEmailCommandAuthorityClient(
        api_key="authority-key-value",
        api_secret="authority-secret-value",
        auth_ref="email-gateway-authority-v1",
        opener=_Opener(_Response(body)),
    )

    with pytest.raises(RuntimeSupportError, match="authority response rejected"):
        client.resolve(
            TenantScope(command["site_id"], command["processing_purpose"]),
            _publication(command),
            command,
        )


def test_client_rejects_unbounded_response_without_leaking_credentials() -> None:
    command = closed_command()
    response = _Response({"message": {"email_send_authority": {}}})
    response.body = b"x" * 65_538
    client = FrappeEmailCommandAuthorityClient(
        api_key="authority-key-value",
        api_secret="authority-secret-value",
        auth_ref="email-gateway-authority-v1",
        opener=_Opener(response),
    )

    with pytest.raises(RuntimeSupportError) as caught:
        client.resolve(
            TenantScope(command["site_id"], command["processing_purpose"]),
            _publication(command),
            command,
        )

    assert "authority-key-value" not in str(caught.value)
    assert "authority-secret-value" not in str(caught.value)


def test_client_fails_closed_on_frappe_error_status_without_parsing_error_detail() -> None:
    command = closed_command()
    client = FrappeEmailCommandAuthorityClient(
        api_key="authority-key-value",
        api_secret="authority-secret-value",
        auth_ref="email-gateway-authority-v1",
        opener=_Opener(
            _Response(
                {"message": {"error": {"code": "raw-user@example.invalid"}}},
                status=409,
            )
        ),
    )

    with pytest.raises(RuntimeSupportError) as caught:
        client.resolve(
            TenantScope(command["site_id"], command["processing_purpose"]),
            _publication(command),
            command,
        )

    assert str(caught.value) == "Frappe email command authority unavailable"
    assert "@" not in str(caught.value)
