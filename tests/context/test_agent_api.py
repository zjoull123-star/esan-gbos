from __future__ import annotations

from fastapi.testclient import TestClient

from services.context.context_service.agent_runtime_api import (
    create_agent_context_runtime_app,
)

from .test_agent_view import _Storage


def _body() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "auth_ref": "auth-agent-runtime",
        "request_id": "request-1",
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "subject_type": "CRM Contact",
        "subject_ref": "contact-1",
        "decision_ref": "decision-1",
        "fact_version_refs": [{"fact_id": "verified-fact-1", "fact_version": 2}],
        "evidence_refs": ["evidence-1"],
    }


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token",
        "X-Auth-Ref": "auth-agent-runtime",
        "X-Site-ID": "gbos.localhost",
        "X-Processing-Purpose": "sales_follow_up",
        "X-Request-ID": "request-1",
        "Content-Type": "application/json",
    }


def _client(*, max_body_bytes: int = 65_536) -> TestClient:
    return TestClient(
        create_agent_context_runtime_app(
            storage=_Storage(),
            local_token="secret-token",
            local_auth_ref="auth-agent-runtime",
            max_body_bytes=max_body_bytes,
        )
    )


def test_agent_context_http_is_503_when_identity_or_storage_is_unconfigured() -> None:
    client = TestClient(create_agent_context_runtime_app())

    response = client.post("/internal/v1/agent-context", json=_body(), headers=_headers())

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_agent_context_http_binds_every_governed_header_to_closed_body() -> None:
    response = _client().post(
        "/internal/v1/agent-context",
        json=_body(),
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["request_id"] == "request-1"
    assert response.json()["context"]["decision_ref"] == "decision-1"

    mismatched = _headers()
    mismatched["X-Site-ID"] = "other.localhost"
    rejected = _client().post(
        "/internal/v1/agent-context",
        json=_body(),
        headers=mismatched,
    )
    assert rejected.status_code == 403
    assert rejected.headers["cache-control"] == "no-store"


def test_agent_context_http_rejects_unknown_json_and_oversized_body() -> None:
    unknown = {**_body(), "raw_message": "forbidden"}
    response = _client().post(
        "/internal/v1/agent-context",
        json=unknown,
        headers=_headers(),
    )
    assert response.status_code == 400

    oversized = _client(max_body_bytes=128).post(
        "/internal/v1/agent-context",
        json=_body(),
        headers=_headers(),
    )
    assert oversized.status_code == 413
    assert oversized.headers["cache-control"] == "no-store"


def test_agent_context_http_requires_json_bearer_and_exact_auth_ref() -> None:
    bad_type = {**_headers(), "Content-Type": "text/plain"}
    response = _client().post(
        "/internal/v1/agent-context",
        content=b"{}",
        headers=bad_type,
    )
    assert response.status_code == 415

    bad_auth = {**_headers(), "X-Auth-Ref": "auth-other"}
    response = _client().post(
        "/internal/v1/agent-context",
        json=_body(),
        headers=bad_auth,
    )
    assert response.status_code == 401
