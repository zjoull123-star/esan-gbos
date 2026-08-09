"""Authenticated local HTTP boundary for refs-only Agent context."""

from __future__ import annotations

import hmac
import json
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .agent_view import (
    AgentContextError,
    AgentContextRequest,
    AgentContextView,
    AgentFactVersionRef,
)
from .decision_storage import DecisionStorage

_BODY_FIELDS = frozenset(
    {
        "schema_version",
        "auth_ref",
        "request_id",
        "site_id",
        "processing_purpose",
        "subject_type",
        "subject_ref",
        "decision_ref",
        "fact_version_refs",
        "evidence_refs",
    }
)
_FACT_REF_FIELDS = frozenset({"fact_id", "fact_version"})


class _NoStoreMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response


def create_agent_context_runtime_app(
    *,
    storage: DecisionStorage | None = None,
    local_token: str | None = None,
    local_auth_ref: str | None = None,
    max_body_bytes: int = 65_536,
) -> FastAPI:
    if max_body_bytes < 1:
        raise ValueError("max_body_bytes must be positive")
    application = FastAPI(
        title="ESAN GBOS Context to Agent API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(_NoStoreMiddleware)

    @application.get("/health")
    def health() -> dict[str, object]:
        ready = storage is not None and local_token is not None and local_auth_ref is not None
        return {
            "status": "ok" if ready else "disabled",
            "ready": ready,
            "local_only": True,
            "raw_content": False,
            "external_effects": False,
        }

    @application.post("/internal/v1/agent-context")
    async def resolve_agent_context(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_auth_ref: Annotated[str | None, Header(alias="X-Auth-Ref")] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_processing_purpose: Annotated[
            str | None,
            Header(alias="X-Processing-Purpose"),
        ] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        if storage is None or local_token is None or local_auth_ref is None:
            raise HTTPException(status_code=503, detail="agent context runtime is disabled")
        _authorize(
            configured_token=local_token,
            configured_auth_ref=local_auth_ref,
            authorization=authorization,
            auth_ref=x_auth_ref,
        )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="application/json is required")
        payload = await _read_closed_json(request, max_body_bytes=max_body_bytes)
        _bind_request(
            payload,
            auth_ref=x_auth_ref,
            site_id=x_site_id,
            purpose=x_processing_purpose,
            request_id=x_request_id,
        )
        try:
            context_request = _context_request(payload)
            assert storage is not None
            bundle = AgentContextView(storage).resolve(context_request)
        except (AgentContextError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="agent context rejected") from exc
        assert x_request_id is not None
        return {
            "schema_version": "1.0",
            "request_id": x_request_id,
            "context": bundle.to_wire(),
        }

    return application


def _authorize(
    *,
    configured_token: str,
    configured_auth_ref: str,
    authorization: str | None,
    auth_ref: str | None,
) -> None:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer identity")
    if not hmac.compare_digest(authorization[len(prefix) :], configured_token):
        raise HTTPException(status_code=401, detail="invalid bearer identity")
    if auth_ref is None or not hmac.compare_digest(auth_ref, configured_auth_ref):
        raise HTTPException(status_code=401, detail="invalid auth reference")


async def _read_closed_json(request: Request, *, max_body_bytes: int) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="invalid content length")
        if declared_length > max_body_bytes:
            raise HTTPException(status_code=413, detail="request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise HTTPException(status_code=413, detail="request body is too large")
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict) or set(payload) != _BODY_FIELDS:
        raise HTTPException(status_code=400, detail="request JSON is not closed")
    return payload


def _bind_request(
    payload: dict[str, Any],
    *,
    auth_ref: str | None,
    site_id: str | None,
    purpose: str | None,
    request_id: str | None,
) -> None:
    if not auth_ref or not site_id or not purpose or not request_id:
        raise HTTPException(status_code=400, detail="governed request headers are required")
    if (
        payload.get("auth_ref") != auth_ref
        or payload.get("site_id") != site_id
        or payload.get("processing_purpose") != purpose
        or payload.get("request_id") != request_id
    ):
        raise HTTPException(status_code=403, detail="request binding mismatch")


def _context_request(payload: dict[str, Any]) -> AgentContextRequest:
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported request schema")
    fact_refs = payload.get("fact_version_refs")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(fact_refs, list) or not all(
        isinstance(item, dict) and set(item) == _FACT_REF_FIELDS for item in fact_refs
    ):
        raise ValueError("fact_version_refs must be closed objects")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(item, str) for item in evidence_refs
    ):
        raise ValueError("evidence_refs must be strings")
    return AgentContextRequest(
        site_id=_text(payload, "site_id"),
        processing_purpose=_text(payload, "processing_purpose"),
        subject_type=_text(payload, "subject_type"),
        subject_ref=_text(payload, "subject_ref"),
        decision_ref=_text(payload, "decision_ref"),
        fact_version_refs=tuple(
            AgentFactVersionRef(
                fact_id=_text(item, "fact_id"),
                fact_version=_integer(item, "fact_version"),
            )
            for item in fact_refs
        ),
        evidence_refs=tuple(evidence_refs),
    )


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


__all__ = ["create_agent_context_runtime_app"]
