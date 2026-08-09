"""Fail-closed local HTTP read surface for the Agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI, Header, HTTPException, Query, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .materialization import MaterializationHealth
from .read_service import AiDraft, AiDraftPage, ModelUsage

_USAGE_QUERY_FIELDS = frozenset({"period"})
_DRAFT_QUERY_FIELDS = frozenset({"status", "cursor", "page_size"})
_NO_QUERY_FIELDS: frozenset[str] = frozenset()


class AgentReadService(Protocol):
    def get_usage(self, site_id: str, period: str) -> ModelUsage: ...

    def list_drafts(
        self,
        site_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
        status: Literal["AI Draft", "Pending"] | None = None,
    ) -> AiDraftPage: ...

    def get_draft(self, site_id: str, draft_id: str) -> AiDraft | None: ...


class AgentRequestAuthorizer(Protocol):
    def authorize(
        self,
        *,
        authorization: str | None,
        requested_site_id: str | None,
    ) -> str: ...


HealthProvider = Callable[[str], MaterializationHealth]


class _NoStoreMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_agent_runtime_app(
    *,
    read_service: AgentReadService | None = None,
    authorizer: AgentRequestAuthorizer | None = None,
    health_provider: HealthProvider | None = None,
) -> FastAPI:
    """Create a local-only read app; missing dependencies keep it unready."""

    application = FastAPI(
        title="ESAN GBOS Local Agent Read API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(_NoStoreMiddleware)

    @application.get("/health")
    def health() -> dict[str, Any]:
        ready = read_service is not None and authorizer is not None and health_provider is not None
        return {
            "status": "ok" if ready else "disabled",
            "ready": ready,
            "local_only": True,
            "capabilities": {
                "read_usage": ready,
                "read_ai_drafts": ready,
                "materialization_worker_health": ready,
                "formal_writer": False,
                "model_invocation": False,
            },
        }

    def authorize_request(
        authorization: str | None,
        site_id: str | None,
        request_id: str | None,
    ) -> tuple[str, str]:
        if read_service is None or authorizer is None or health_provider is None:
            raise HTTPException(status_code=503, detail="agent read runtime is disabled")
        try:
            authenticated_site = authorizer.authorize(
                authorization=authorization,
                requested_site_id=site_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=401, detail="service identity rejected") from exc
        if not site_id or authenticated_site != site_id:
            raise HTTPException(status_code=403, detail="tenant scope rejected")
        if not request_id:
            raise HTTPException(status_code=400, detail="request ID is required")
        return site_id, request_id

    @application.get("/internal/v1/model/usage")
    def get_usage(
        request: Request,
        period: str,
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        _require_bounded_query(request, _USAGE_QUERY_FIELDS)
        site_id, request_id = authorize_request(
            authorization,
            x_site_id,
            x_request_id,
        )
        assert read_service is not None
        try:
            usage = read_service.get_usage(site_id, period)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid usage query") from exc
        return _envelope(usage.to_wire(), request_id=request_id)

    @application.get("/internal/v1/ai-drafts")
    def list_drafts(
        request: Request,
        status: Literal["AI Draft", "Pending"] | None = None,
        cursor: str | None = None,
        page_size: Annotated[int, Query(ge=1, le=50)] = 20,
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        _require_bounded_query(request, _DRAFT_QUERY_FIELDS)
        site_id, request_id = authorize_request(
            authorization,
            x_site_id,
            x_request_id,
        )
        assert read_service is not None
        try:
            page = read_service.list_drafts(
                site_id,
                status=status,
                cursor=cursor,
                page_size=page_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid draft query") from exc
        return _envelope(
            {
                "drafts": [draft.to_wire() for draft in page.drafts],
                "next_cursor": page.next_cursor,
            },
            request_id=request_id,
            next_cursor=page.next_cursor,
            page_size=page_size,
        )

    @application.get("/internal/v1/ai-drafts/{draft_id}")
    def get_draft(
        draft_id: str,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        _require_bounded_query(request, _NO_QUERY_FIELDS)
        site_id, request_id = authorize_request(
            authorization,
            x_site_id,
            x_request_id,
        )
        assert read_service is not None
        try:
            draft = read_service.get_draft(site_id, draft_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid draft query") from exc
        if draft is None:
            raise HTTPException(status_code=404, detail="draft not found")
        return _envelope({"draft": draft.to_wire()}, request_id=request_id)

    @application.get("/internal/v1/materialization/health")
    def materialization_health(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        _require_bounded_query(request, _NO_QUERY_FIELDS)
        site_id, request_id = authorize_request(
            authorization,
            x_site_id,
            x_request_id,
        )
        assert health_provider is not None
        return _envelope(health_provider(site_id).to_wire(), request_id=request_id)

    return application


def _require_bounded_query(request: Request, allowed: frozenset[str]) -> None:
    if not set(request.query_params).issubset(allowed) or any(
        len(request.query_params.getlist(field)) != 1 for field in request.query_params
    ):
        raise HTTPException(status_code=400, detail="unsupported query field")


def _envelope(
    data: dict[str, Any],
    *,
    request_id: str,
    next_cursor: str | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "request_id": request_id,
        "schema_version": "4.0",
    }
    if page_size is not None:
        meta["page_size"] = page_size
        meta["next_cursor"] = next_cursor
    return {"data": data, "meta": meta}


__all__ = [
    "AgentReadService",
    "AgentRequestAuthorizer",
    "create_agent_runtime_app",
]
