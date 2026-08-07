from __future__ import annotations

from typing import Any

import pytest

from services.agent_runtime.frappe_client import FrappeClientError, HttpFrappeDraftClient
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
        base_url="unix:///tmp/gbos-frappe.sock",
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
