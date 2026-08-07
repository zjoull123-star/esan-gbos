from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from services.agent_runtime.context_resolver import (
    ContextBinding,
    ContextEndpoint,
    ContextResolutionError,
    HttpContextResolver,
)
from services.agent_runtime.models import LocalPilotFactVersionRef
from services.agent_runtime.worker import ContextResolutionRequest


def _request() -> ContextResolutionRequest:
    return ContextResolutionRequest(
        site_id="gbos.localhost",
        task_id="task-1",
        subject_type="CRM Contact",
        subject_ref="contact-1",
        evidence_refs=("evidence-1",),
        fact_version_refs=(LocalPilotFactVersionRef("verified-fact-1", 2),),
    )


def _binding(_: ContextResolutionRequest) -> ContextBinding:
    return ContextBinding(
        processing_purpose="sales_follow_up",
        decision_ref="decision-1",
        request_id="request-1",
    )


def _context() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "subject_type": "CRM Contact",
        "subject_ref": "contact-1",
        "decision_ref": "decision-1",
        "fact_version_refs": [{"fact_id": "verified-fact-1", "fact_version": 2}],
        "evidence_refs": ["evidence-1"],
        "facts": [
            {
                "fact_id": "verified-fact-1",
                "fact_version": 2,
                "predicate": "requested_quantity",
                "value": {"type": "number", "number": 1000, "unit": "pcs"},
                "valid_time": {"start": "2026-08-08T01:00:00Z", "end": None},
                "recorded_time": "2026-08-08T02:00:00Z",
                "review_status": "human_reviewed",
            }
        ],
    }


def _resolver(handler: httpx.MockTransport) -> HttpContextResolver:
    return HttpContextResolver(
        endpoint=ContextEndpoint("http://127.0.0.1:8123"),
        bearer_token="secret-token",
        auth_ref="auth-agent-runtime",
        binding_resolver=_binding,
        transport=handler,
    )


def test_http_context_resolver_binds_request_and_returns_canonical_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://127.0.0.1:8123/internal/v1/agent-context")
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.headers["x-auth-ref"] == "auth-agent-runtime"
        assert request.headers["x-site-id"] == "gbos.localhost"
        assert request.headers["x-processing-purpose"] == "sales_follow_up"
        assert request.headers["x-request-id"] == "request-1"
        sent = json.loads(request.content)
        assert sent["decision_ref"] == "decision-1"
        assert sent["fact_version_refs"] == [{"fact_id": "verified-fact-1", "fact_version": 2}]
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "request_id": "request-1",
                "context": _context(),
            },
            headers={"Cache-Control": "no-store"},
        )

    resolved = _resolver(httpx.MockTransport(handler)).resolve(_request())

    assert resolved.site_id == "gbos.localhost"
    assert resolved.subject_type == "CRM Contact"
    assert resolved.subject_ref == "contact-1"
    assert resolved.evidence_refs == ("evidence-1",)
    assert resolved.fact_version_refs == (LocalPilotFactVersionRef("verified-fact-1", 2),)
    assert json.loads(resolved.raw_context) == _context()
    assert "secret-token" not in repr(resolved)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8123",
        "http://0.0.0.0:8123",
        "http://192.168.1.10:8123",
        "https://api.deepseek.com",
        "http://127.0.0.1:8123/path",
        "http://user@127.0.0.1:8123",
    ],
)
def test_context_endpoint_rejects_every_non_literal_loopback_target(url: str) -> None:
    with pytest.raises(ValueError, match="loopback|origin"):
        ContextEndpoint(url)


def test_context_endpoint_accepts_absolute_unix_socket_without_dns(tmp_path: Path) -> None:
    endpoint = ContextEndpoint(
        "http://context.internal",
        unix_socket=tmp_path / "context.sock",
    )

    assert endpoint.unix_socket == tmp_path / "context.sock"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(site_id="other.localhost"),
        lambda value: value.update(processing_purpose="metric_reporting"),
        lambda value: value.update(decision_ref="decision-other"),
        lambda value: value.update(evidence_refs=[]),
        lambda value: value.update(fact_version_refs=[]),
    ],
)
def test_http_context_resolver_rejects_any_response_ref_mismatch(mutate: object) -> None:
    context = _context()
    mutate(context)  # type: ignore[operator]
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "request_id": "request-1",
                "context": context,
            },
            headers={"Cache-Control": "no-store"},
        )
    )

    with pytest.raises(ContextResolutionError, match="mismatch"):
        _resolver(transport).resolve(_request())


def test_http_context_resolver_never_follows_redirects_or_unbounded_responses() -> None:
    redirected = _resolver(
        httpx.MockTransport(
            lambda _: httpx.Response(307, headers={"Location": "https://example.com"})
        )
    )
    with pytest.raises(ContextResolutionError, match="status"):
        redirected.resolve(_request())

    oversized = HttpContextResolver(
        endpoint=ContextEndpoint("http://[::1]:8123"),
        bearer_token="secret-token",
        auth_ref="auth-agent-runtime",
        binding_resolver=_binding,
        max_body_bytes=128,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=b"{" + b'"padding":"' + b"x" * 256 + b'"}',
                headers={"Cache-Control": "no-store"},
            )
        ),
    )
    with pytest.raises(ContextResolutionError, match="large"):
        oversized.resolve(_request())
