from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from services.agent_runtime.frappe_client import FrappeClientError
from services.agent_runtime.frappe_context import HttpMaterializationContextResolver
from services.agent_runtime.materialization import MaterializationContextRequest


class _Transport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return 200, self.response


def _digest(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _request() -> MaterializationContextRequest:
    return MaterializationContextRequest(
        site_id="gbos.localhost",
        task_id="task-0001",
        processing_purpose="sales_follow_up",
        proposal_id="proposal-0001",
        subject_type="GBOS Work Item",
        subject_ref="WRK-0001",
        subject_revision=3,
    )


def test_context_resolver_returns_only_bound_controlled_context() -> None:
    snapshot = {
        "doctype": "GBOS Work Item",
        "name": "WRK-0001",
        "revision": 3,
        "title": "Prepare follow-up",
        "team": "TEM-0001",
    }
    transport = _Transport(
        {
            "message": {
                "site_id": "gbos.localhost",
                "request_id": "task-0001",
                "subject_type": "GBOS Work Item",
                "subject_ref": "WRK-0001",
                "subject_revision": 3,
                "team": "TEM-0001",
                "assigned_reviewer": "reviewer@example.invalid",
                "subject_snapshot": snapshot,
                "subject_payload_digest": _digest(snapshot),
            }
        }
    )
    resolver = HttpMaterializationContextResolver(
        base_url="http://127.0.0.1:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        transport=transport,
    )

    context = resolver.resolve(_request())

    assert context is not None
    assert context.team == "TEM-0001"
    assert context.assigned_reviewer == "reviewer@example.invalid"
    assert context.subject_snapshot == snapshot
    call = transport.calls[0]
    assert call["payload"]["payload"]["proposal_id"] == "proposal-0001"
    assert call["headers"]["X-Request-ID"] == "task-0001"
    assert "materializer-secret" not in repr(resolver)


def test_context_resolver_accepts_exact_explicit_internal_service_host() -> None:
    snapshot = {
        "doctype": "GBOS Work Item",
        "name": "WRK-0001",
        "revision": 3,
        "team": "TEM-0001",
    }
    transport = _Transport(
        {
            "message": {
                "site_id": "gbos.localhost",
                "request_id": "task-0001",
                "subject_type": "GBOS Work Item",
                "subject_ref": "WRK-0001",
                "subject_revision": 3,
                "team": "TEM-0001",
                "assigned_reviewer": None,
                "subject_snapshot": snapshot,
                "subject_payload_digest": _digest(snapshot),
            }
        }
    )
    resolver = HttpMaterializationContextResolver(
        base_url="http://frappe-backend:8000",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        allowed_internal_hosts=frozenset({"frappe-backend"}),
        transport=transport,
    )

    context = resolver.resolve(_request())

    assert context is not None
    assert transport.calls[0]["url"].startswith("http://frappe-backend:8000/")
    assert "materializer-secret" not in repr(resolver)


def test_context_resolver_rejects_internal_service_host_by_default() -> None:
    with pytest.raises(FrappeClientError, match="loopback|URL|host"):
        HttpMaterializationContextResolver(
            base_url="http://frappe-backend:8000",
            api_key="materializer-key",
            api_secret="materializer-secret",
            auth_ref="agent-materializer-v1",
            site_id="gbos.localhost",
            transport=_Transport({}),
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("site_id", "other.localhost"),
        ("request_id", "other-task"),
        ("subject_revision", 4),
    ],
)
def test_context_resolver_rejects_mismatched_response_binding(
    field: str,
    changed: str | int,
) -> None:
    snapshot = {
        "doctype": "GBOS Work Item",
        "name": "WRK-0001",
        "revision": 3,
        "team": "TEM-0001",
    }
    response = {
        "site_id": "gbos.localhost",
        "request_id": "task-0001",
        "subject_type": "GBOS Work Item",
        "subject_ref": "WRK-0001",
        "subject_revision": 3,
        "team": "TEM-0001",
        "assigned_reviewer": "reviewer@example.invalid",
        "subject_snapshot": snapshot,
        "subject_payload_digest": _digest(snapshot),
    }
    response[field] = changed
    resolver = HttpMaterializationContextResolver(
        base_url="unix:///run/gbos/sockets/frappe.sock",
        api_key="materializer-key",
        api_secret="materializer-secret",
        auth_ref="agent-materializer-v1",
        site_id="gbos.localhost",
        transport=_Transport({"message": response}),
    )

    with pytest.raises(FrappeClientError, match="context"):
        resolver.resolve(_request())
