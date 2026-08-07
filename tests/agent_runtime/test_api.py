from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from importlib.util import find_spec
from typing import Any

from fastapi.testclient import TestClient

from services.agent_runtime.api import create_agent_runtime_app
from services.agent_runtime.materialization import MaterializationHealth
from services.agent_runtime.read_service import (
    AiDraft,
    AiDraftPage,
    ModelUsage,
    UsageCost,
)


@dataclass
class _Authorizer:
    authenticated_site: str = "site-a"

    def authorize(
        self,
        *,
        authorization: str | None,
        requested_site_id: str | None,
    ) -> str:
        if authorization != "Bearer local-token":
            raise PermissionError("unauthorized")
        return self.authenticated_site


class _ReadService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.draft = AiDraft(
            draft_id="proposal-1",
            kind="CEO Informal Observation",
            status="AI Draft",
            origin="AI",
            subject="subject-1",
            evidence=({"ref": "evidence-1", "locator": "evidence://evidence-1"},),
            model_version="deepseek-v4-flash",
            revision=3,
        )

    def get_usage(self, site_id: str, period: str) -> ModelUsage:
        self.calls.append(("usage", site_id, period))
        return ModelUsage(
            model="deepseek-v4-flash",
            period=period,
            tokens=None,
            token_state="unknown",
            cost=UsageCost(currency="USD", amount=None, state="unknown"),
            soft_limit_usd=Decimal("50"),
            hard_limit_usd=Decimal("100"),
            state="unknown",
        )

    def list_drafts(
        self,
        site_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
        status: str | None = None,
    ) -> AiDraftPage:
        self.calls.append(("list", site_id, cursor, page_size, status))
        return AiDraftPage(drafts=(self.draft,), next_cursor=None)

    def get_draft(self, site_id: str, draft_id: str) -> AiDraft | None:
        self.calls.append(("get", site_id, draft_id))
        return self.draft if draft_id == self.draft.draft_id else None


HEADERS = {
    "Authorization": "Bearer local-token",
    "X-Site-ID": "site-a",
    "X-Request-ID": "request-1",
}


def test_default_runtime_module_is_available_but_unready_without_configuration() -> None:
    assert find_spec("services.agent_runtime.runtime") is not None


def test_default_app_is_not_ready_and_read_routes_fail_closed() -> None:
    client = TestClient(create_agent_runtime_app())

    health = client.get("/health")
    unavailable = client.get(
        "/internal/v1/model/usage",
        params={"period": "2026-08"},
        headers=HEADERS,
    )

    assert health.status_code == 200
    assert health.json()["ready"] is False
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"


def test_usage_requires_injected_auth_and_exact_site_then_returns_no_store() -> None:
    service = _ReadService()
    client = TestClient(
        create_agent_runtime_app(
            read_service=service,
            authorizer=_Authorizer(),
            health_provider=lambda _site: MaterializationHealth(0, 0, 0, 0),
        )
    )

    missing = client.get(
        "/internal/v1/model/usage",
        params={"period": "2026-08"},
    )
    wrong_site = client.get(
        "/internal/v1/model/usage",
        params={"period": "2026-08"},
        headers={**HEADERS, "X-Site-ID": "site-b"},
    )
    response = client.get(
        "/internal/v1/model/usage",
        params={"period": "2026-08"},
        headers=HEADERS,
    )

    assert missing.status_code == 401
    assert wrong_site.status_code == 403
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["tokens"] is None
    assert response.json()["data"]["cost"]["amount"] is None
    assert service.calls == [("usage", "site-a", "2026-08")]


def test_draft_routes_are_bounded_read_only_and_do_not_expose_content() -> None:
    service = _ReadService()
    app = create_agent_runtime_app(
        read_service=service,
        authorizer=_Authorizer(),
        health_provider=lambda _site: MaterializationHealth(1, 0, 0, 0),
    )
    client = TestClient(app)

    too_large = client.get(
        "/internal/v1/ai-drafts",
        params={"page_size": 51},
        headers=HEADERS,
    )
    unsupported = client.get(
        "/internal/v1/ai-drafts",
        params={"page_size": 20, "raw": "true"},
        headers=HEADERS,
    )
    unsupported_detail = client.get(
        "/internal/v1/ai-drafts/proposal-1",
        params={"raw": "true"},
        headers=HEADERS,
    )
    listed = client.get(
        "/internal/v1/ai-drafts",
        params={"page_size": 20, "status": "AI Draft"},
        headers=HEADERS,
    )
    detail = client.get(
        "/internal/v1/ai-drafts/proposal-1",
        headers=HEADERS,
    )

    assert too_large.status_code == 422
    assert unsupported.status_code == 400
    assert unsupported_detail.status_code == 400
    assert listed.status_code == 200
    assert detail.status_code == 200
    flattened = repr((listed.json(), detail.json())).casefold()
    for forbidden in ("prompt", "response", "document", "person_name", "raw_context"):
        assert forbidden not in flattened
    assert not any(
        route.path.startswith("/internal/v1/model/invoke")
        or "submit" in route.path
        or "write" in route.path
        for route in app.routes
    )


def test_materialization_health_is_authenticated_and_site_scoped() -> None:
    seen: list[str] = []

    def health(site_id: str) -> MaterializationHealth:
        seen.append(site_id)
        return MaterializationHealth(2, 1, 3, 0)

    client = TestClient(
        create_agent_runtime_app(
            read_service=_ReadService(),
            authorizer=_Authorizer(),
            health_provider=health,
        )
    )

    response = client.get("/internal/v1/materialization/health", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "ready": True,
        "pending": 2,
        "running": 1,
        "retry": 3,
        "dead_letter": 0,
    }
    assert seen == ["site-a"]
