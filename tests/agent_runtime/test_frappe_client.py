from __future__ import annotations

import os
from typing import Any

import pytest

from services.agent_runtime.frappe_client import (
    FrappeClientError,
    HttpFrappeDraftClient,
    HttpxFrappeTransport,
)
from services.agent_runtime.models import IdempotencyConflict, canonical_payload_digest
from services.agent_runtime.proposals import MaterializationIntent


class _Transport:
    def __init__(self, response: dict[str, Any], *, status: int = 200) -> None:
        self.response = response
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.status, self.response


def _intent() -> MaterializationIntent:
    return MaterializationIntent(
        operation="create",
        doctype="GBOS Work Item",
        values={
            "title": "Prepare follow-up",
            "team": "TEM-0001",
            "reference_doctype": "GBOS Work Item",
            "reference_name": "WRK-0001",
            "origin": "AI",
            "origin_reference": "proposal-0001",
            "business_status": "Open",
            "review_status": "AI Draft",
        },
    )


def _digest(intent: MaterializationIntent) -> str:
    return canonical_payload_digest(
        {
            "operation": intent.operation,
            "doctype": intent.doctype,
            "values": dict(intent.values),
        }
    )


@pytest.mark.parametrize(
    "base_url",
    (
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://192.0.2.1:8000",
        "http://frappe-backend:8000",
        "http://user:secret@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
    ),
)
def test_frappe_client_rejects_every_non_strict_loopback_url(base_url: str) -> None:
    with pytest.raises(FrappeClientError, match="loopback|URL"):
        HttpFrappeDraftClient(
            base_url=base_url,
            api_key="materializer-key",
            api_secret="materializer-secret",
            auth_ref="agent-materializer-v1",
            site_id="gbos.localhost",
            processing_purpose="sales_follow_up",
            transport=_Transport({}),
        )


def test_frappe_client_accepts_exact_explicit_internal_service_host() -> None:
    client = HttpFrappeDraftClient(
        base_url="http://frappe-backend:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        allowed_internal_hosts=frozenset({"frappe-backend"}),
        transport=_Transport({}),
    )

    assert "materializer-secret" not in repr(client)


@pytest.mark.parametrize(
    ("base_url", "allowed_internal_hosts"),
    (
        ("http://other-backend:8000", frozenset({"frappe-backend"})),
        ("http://frappe-backend.evil:8000", frozenset({"frappe-backend"})),
        ("http://frappe-backend:8001", frozenset({"frappe-backend"})),
        ("http://frappe-backend.:8000", frozenset({"frappe-backend"})),
        ("http://frappe_backend:8000", frozenset({"frappe_backend"})),
        ("http://frappe-backend:8000", frozenset({"localhost"})),
        ("http://frappe-backend:8000", frozenset({"127.0.0.1"})),
        ("http://frappe-backend:8000", frozenset({"frappe-backend."})),
    ),
)
def test_frappe_client_rejects_non_exact_or_unsafe_internal_service_hosts(
    base_url: str,
    allowed_internal_hosts: frozenset[str],
) -> None:
    with pytest.raises(FrappeClientError, match="URL|host|port"):
        HttpFrappeDraftClient(
            base_url=base_url,
            api_key="materializer-key",
            api_secret="materializer-secret",
            auth_ref="agent-materializer-v1",
            site_id="gbos.localhost",
            processing_purpose="sales_follow_up",
            allowed_internal_hosts=allowed_internal_hosts,
            transport=_Transport({}),
        )


@pytest.mark.parametrize(
    "base_url",
    (
        "unix:///tmp/gbos-frappe.sock",
        "unix:///run/gbos/sockets/nested/frappe.sock",
        "unix:///run/gbos/sockets/../frappe.sock",
        "unix:///run/gbos/sockets/.frappe.sock",
        "unix:///run/gbos/sockets/frappe",
    ),
)
def test_frappe_client_rejects_unix_socket_outside_fixed_safe_directory(
    base_url: str,
) -> None:
    with pytest.raises(FrappeClientError, match="Unix|socket"):
        HttpFrappeDraftClient(
            base_url=base_url,
            api_key="materializer-key",
            api_secret="materializer-secret",
            auth_ref="agent-materializer-v1",
            site_id="gbos.localhost",
            processing_purpose="sales_follow_up",
            transport=_Transport({}),
        )


def test_frappe_client_uses_standard_token_auth_and_governed_headers() -> None:
    intent = _intent()
    digest = _digest(intent)
    transport = _Transport(
        {
            "message": {
                "site_id": "gbos.localhost",
                "doctype": "GBOS Work Item",
                "name": "WRK-AI-0001",
                "revision": 1,
                "request_id": "materialization-0001",
                "request_digest": digest,
            }
        }
    )
    client = HttpFrappeDraftClient(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        timeout_seconds=2.5,
        transport=transport,
    )

    receipt = client.apply(
        intent,
        request_id="materialization-0001",
        request_digest=digest,
    )

    assert receipt.name == "WRK-AI-0001"
    call = transport.calls[0]
    assert call["headers"] == {
        "Authorization": "token materializer-key:materializer-secret",
        "Content-Type": "application/json",
        "Host": "gbos.localhost",
        "X-GBOS-Frappe-Auth-Ref": "agent-materializer-v1",
        "X-Processing-Purpose": "sales_follow_up",
        "X-Request-ID": "materialization-0001",
        "X-Site-ID": "gbos.localhost",
    }
    assert call["timeout_seconds"] == 2.5
    assert "materializer-secret" not in repr(client)
    assert call["payload"]["payload"]["request_digest"] == digest


def test_frappe_client_uses_explicit_per_apply_purpose_for_headers_and_payload() -> None:
    intent = _intent()
    digest = _digest(intent)
    transport = _Transport(
        {
            "message": {
                "site_id": "gbos.localhost",
                "doctype": "GBOS Work Item",
                "name": "WRK-AI-0001",
                "revision": 1,
                "request_id": "materialization-0001",
                "request_digest": digest,
            }
        }
    )
    client = HttpFrappeDraftClient(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        transport=transport,
    )

    client.apply(
        intent,
        request_id="materialization-0001",
        request_digest=digest,
        processing_purpose="procurement_coordination",
    )

    call = transport.calls[0]
    assert call["headers"]["X-Processing-Purpose"] == "procurement_coordination"
    assert call["payload"]["payload"]["processing_purpose"] == "procurement_coordination"


def test_frappe_client_requires_one_resolvable_processing_purpose() -> None:
    intent = _intent()
    digest = _digest(intent)
    transport = _Transport({})
    client = HttpFrappeDraftClient(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        transport=transport,
    )

    with pytest.raises(FrappeClientError, match="processing purpose"):
        client.apply(
            intent,
            request_id="materialization-0001",
            request_digest=digest,
        )

    assert transport.calls == []


def test_frappe_client_rejects_conflicting_fixed_and_per_apply_purposes() -> None:
    intent = _intent()
    digest = _digest(intent)
    transport = _Transport({})
    client = HttpFrappeDraftClient(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        transport=transport,
    )

    with pytest.raises(FrappeClientError, match="processing purpose"):
        client.apply(
            intent,
            request_id="materialization-0001",
            request_digest=digest,
            processing_purpose="metric_reporting",
        )

    assert transport.calls == []


def test_frappe_client_rejects_receipt_site_or_revision_mismatch() -> None:
    intent = _intent()
    digest = _digest(intent)
    transport = _Transport(
        {
            "message": {
                "site_id": "other.localhost",
                "doctype": "GBOS Work Item",
                "name": "WRK-AI-0001",
                "revision": -1,
                "request_id": "materialization-0001",
                "request_digest": digest,
            }
        }
    )
    client = HttpFrappeDraftClient(
        base_url="unix:///run/gbos/sockets/frappe.sock",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        transport=transport,
    )

    with pytest.raises(FrappeClientError, match="receipt"):
        client.apply(
            intent,
            request_id="materialization-0001",
            request_digest=digest,
        )


def test_frappe_client_maps_only_idempotency_conflict_without_response_body() -> None:
    intent = _intent()
    digest = _digest(intent)
    client = HttpFrappeDraftClient(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        transport=_Transport(
            {
                "message": {
                    "error": {
                        "code": "idempotency_conflict",
                        "raw": "secret Frappe traceback",
                    }
                }
            },
            status=409,
        ),
    )

    with pytest.raises(IdempotencyConflict) as raised:
        client.apply(
            intent,
            request_id="materialization-0001",
            request_digest=digest,
        )

    assert "traceback" not in str(raised.value)


def test_httpx_transport_disables_environment_proxies_and_redirect_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    class _RedirectResponse:
        status_code = 302

        def __enter__(self) -> _RedirectResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def stream(self, *_: object, **__: object) -> _RedirectResponse:
            return _RedirectResponse()

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setattr("services.agent_runtime.frappe_client.httpx.Client", _Client)

    with pytest.raises(FrappeClientError, match="redirect"):
        HttpxFrappeTransport().request(
            url="http://127.0.0.1:8000/api/method/test",
            headers={"Authorization": "token key:secret"},
            payload={"payload": {}},
            timeout_seconds=1.0,
        )

    assert os.environ["HTTP_PROXY"] == "http://proxy.invalid:8080"
    assert seen[0]["trust_env"] is False
    assert seen[0]["follow_redirects"] is False
