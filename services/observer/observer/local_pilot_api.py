from __future__ import annotations

import hmac
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, Protocol

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .control_service import (
    ConnectorControlResult,
    ConnectorStatus,
    IdempotencyConflict,
    RevisionConflict,
)
from .models import ConnectorKey, TenantScope
from .read_service import (
    CommunicationAccess,
    CommunicationDetail,
    CommunicationPage,
    InvalidCursor,
)
from .runtime import KillSwitchEngaged, LocalPilotRuntimeGuard

Clock = Callable[[], datetime]
_BOUND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_REPLAY_REQUIRES = (
    "within_connector_replay_window",
    "not_retention_expired",
    "same_site_and_instance",
)


@dataclass(frozen=True, slots=True, repr=False)
class LocalPilotAPIConfig:
    """Secret-bearing bind policy; repr never exposes authentication values."""

    bind_host: str
    network_mode: Literal["loopback", "internal_network", "unix_socket"]
    bearer_token: str = field(repr=False)
    auth_ref: str = field(repr=False)
    max_request_bytes: int = 262_144

    def __post_init__(self) -> None:
        _safe_secret(self.bearer_token, "bearer_token")
        _safe_secret(self.auth_ref, "auth_ref")
        if not 1 <= self.max_request_bytes <= 1_048_576:
            raise ValueError("max_request_bytes is outside the local API budget")
        if self.network_mode == "loopback":
            _require_loopback(self.bind_host)
        elif self.network_mode == "internal_network":
            if (
                not self.bind_host
                or self.bind_host != self.bind_host.strip()
                or len(self.bind_host) > 253
            ):
                raise ValueError("invalid internal network bind host")
        elif self.network_mode == "unix_socket":
            path = PurePosixPath(self.bind_host)
            if not path.is_absolute() or ".." in path.parts or len(self.bind_host) > 512:
                raise ValueError("invalid Unix socket path")
        else:
            raise ValueError("invalid local API network mode")


class ControlService(Protocol):
    def resolve_instance(
        self,
        scope: TenantScope,
        *,
        instance_id: str,
    ) -> ConnectorKey: ...

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None = None,
    ) -> tuple[ConnectorStatus, ...]: ...

    def pause(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ConnectorControlResult: ...

    def resume(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ConnectorControlResult: ...

    def replay(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
        limit: int = 100,
    ) -> ConnectorControlResult: ...


class ReadService(Protocol):
    def list_communications(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        channel: str | None = None,
        classification: str | None = None,
        review_status: str | None = None,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> CommunicationPage: ...

    def get_communication(
        self,
        scope: TenantScope,
        access: CommunicationAccess,
        *,
        observation_id: str,
        include_raw: bool = False,
    ) -> CommunicationDetail: ...


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConnectorListRequest(_ClosedModel):
    channel: str | None = Field(default=None, min_length=1, max_length=80)


class ConnectorCommandRequest(_ClosedModel):
    instance_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=256)


class ReplayCommandRequest(ConnectorCommandRequest):
    delivery_scope: Literal["eligible_failed_deliveries"]
    limit: Literal[100]
    requires: list[str] = Field(min_length=3, max_length=3)


class CommunicationListRequest(_ClosedModel):
    actor_ref: str = Field(min_length=1, max_length=256)
    allowed_team_refs: list[str] = Field(min_length=1, max_length=100)
    scope: Literal["all_business_projection", "team_and_self"]
    include_raw: Literal[False]
    page_size: int = Field(ge=1, le=50)
    channel: str | None = Field(default=None, min_length=1, max_length=80)
    classification: str | None = Field(default=None, min_length=1, max_length=80)
    review_status: str | None = Field(default=None, min_length=1, max_length=80)
    cursor: str | None = Field(default=None, min_length=1, max_length=8192)


class CommunicationGetRequest(_ClosedModel):
    actor_ref: str = Field(min_length=1, max_length=256)
    allowed_team_refs: list[str] = Field(min_length=1, max_length=100)
    scope: Literal["all_business_projection", "team_and_self"]
    include_raw: Literal[False]
    observation_id: str = Field(min_length=1, max_length=256)


def create_local_pilot_app(
    *,
    config: LocalPilotAPIConfig,
    control: ControlService,
    reader: ReadService,
    guard: LocalPilotRuntimeGuard,
    clock: Clock,
) -> FastAPI:
    """Create the authenticated Frappe v4 downstream surface without starting I/O."""

    del clock
    application = FastAPI(
        title="ESAN GBOS Observer Local Pilot",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def governed_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path.startswith("/internal/"):
            rejected = await _validate_internal_request(request, config)
            if rejected is not None:
                rejected.headers["Cache-Control"] = "no-store"
                return rejected
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(RevisionConflict)
    async def revision_conflict(request: Request, exc: RevisionConflict) -> JSONResponse:
        del exc
        return _error(request, 409, "revision_conflict")

    @application.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(request: Request, exc: IdempotencyConflict) -> JSONResponse:
        del exc
        return _error(request, 409, "idempotency_conflict")

    @application.exception_handler(LookupError)
    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(request, 404, "not_found")

    @application.exception_handler(PermissionError)
    async def forbidden(request: Request, exc: Exception) -> JSONResponse:
        del exc
        return _error(request, 403, "scope_mismatch")

    @application.exception_handler(InvalidCursor)
    async def invalid_cursor(request: Request, exc: InvalidCursor) -> JSONResponse:
        del exc
        return _error(request, 422, "invalid_query")

    @application.exception_handler(KillSwitchEngaged)
    async def stopped(request: Request, exc: KillSwitchEngaged) -> JSONResponse:
        del exc
        return _error(request, 503, "runtime_stopped")

    @application.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError) -> JSONResponse:
        del exc
        return _error(request, 422, "invalid_query")

    @application.exception_handler(RequestValidationError)
    async def invalid_body(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del exc
        return _error(request, 422, "invalid_query")

    @application.get("/health")
    def health() -> dict[str, object]:
        return {
            **guard.health(),
            "network_mode": config.network_mode,
            "authenticated_internal_api": True,
        }

    @application.post("/internal/v1/bff/connectors/list")
    def bff_connector_list(
        request: Request,
        payload: Annotated[ConnectorListRequest, Body()],
    ) -> dict[str, object]:
        guard.require_running()
        scope, request_id = _governed_scope(
            request,
            expected_purpose="connector_status",
        )
        channel = payload.channel.strip() if payload.channel is not None else None
        statuses = control.list_status(scope, channel=channel)
        return _bff_envelope(
            {"connectors": [status.as_dict() for status in statuses]},
            site_id=scope.site_id,
            request_id=request_id,
        )

    def run_connector_command(
        request: Request,
        payload: ConnectorCommandRequest,
        *,
        operation: Literal["pause", "resume", "replay"],
        replay_limit: int = 100,
    ) -> dict[str, object]:
        guard.require_running()
        scope, request_id = _governed_scope(
            request,
            expected_purpose="connector_control",
        )
        header_key = request.headers.get("idempotency-key")
        if header_key is None:
            raise ValueError("idempotency key header is required")
        if not hmac.compare_digest(header_key, payload.idempotency_key):
            raise IdempotencyConflict("header and body idempotency keys differ")
        key = control.resolve_instance(scope, instance_id=payload.instance_id)
        if operation == "replay":
            result = control.replay(
                scope,
                key,
                expected_revision=payload.expected_revision,
                idempotency_key=payload.idempotency_key,
                limit=replay_limit,
            )
        else:
            action = control.pause if operation == "pause" else control.resume
            result = action(
                scope,
                key,
                expected_revision=payload.expected_revision,
                idempotency_key=payload.idempotency_key,
            )
        return _bff_envelope(
            {
                "connector": result.status.as_dict(),
            },
            site_id=scope.site_id,
            request_id=request_id,
            replayed=result.replayed,
        )

    @application.post("/internal/v1/bff/connectors/pause")
    def bff_pause(
        request: Request,
        payload: Annotated[ConnectorCommandRequest, Body()],
    ) -> dict[str, object]:
        return run_connector_command(request, payload, operation="pause")

    @application.post("/internal/v1/bff/connectors/resume")
    def bff_resume(
        request: Request,
        payload: Annotated[ConnectorCommandRequest, Body()],
    ) -> dict[str, object]:
        return run_connector_command(request, payload, operation="resume")

    @application.post("/internal/v1/bff/connectors/replay")
    def bff_replay(
        request: Request,
        payload: Annotated[ReplayCommandRequest, Body()],
    ) -> dict[str, object]:
        if tuple(payload.requires) != _REPLAY_REQUIRES:
            raise ValueError("invalid replay requirements")
        return run_connector_command(
            request,
            payload,
            operation="replay",
            replay_limit=payload.limit,
        )

    @application.post("/internal/v1/bff/communications/list")
    def bff_communication_list(
        request: Request,
        payload: Annotated[CommunicationListRequest, Body()],
    ) -> dict[str, object]:
        guard.require_running()
        scope, request_id = _governed_scope(
            request,
            expected_purpose="communication_projection",
        )
        page = reader.list_communications(
            scope,
            _communication_access(payload),
            channel=payload.channel,
            classification=payload.classification,
            review_status=payload.review_status,
            cursor=payload.cursor,
            page_size=payload.page_size,
        )
        return _bff_envelope(
            {
                "communications": [
                    communication.as_dict() for communication in page.communications
                ],
                "next_cursor": page.next_cursor,
            },
            site_id=scope.site_id,
            request_id=request_id,
        )

    @application.post("/internal/v1/bff/communications/get")
    def bff_communication_get(
        request: Request,
        payload: Annotated[CommunicationGetRequest, Body()],
    ) -> dict[str, object]:
        guard.require_running()
        scope, request_id = _governed_scope(
            request,
            expected_purpose="communication_projection",
        )
        detail = reader.get_communication(
            scope,
            _communication_access(payload),
            observation_id=payload.observation_id,
            include_raw=False,
        )
        return _bff_envelope(
            {"communication": detail.as_dict()},
            site_id=scope.site_id,
            request_id=request_id,
        )

    return application


async def _validate_internal_request(
    request: Request,
    config: LocalPilotAPIConfig,
) -> JSONResponse | None:
    authorization = request.headers.get("authorization")
    auth_ref = request.headers.get("x-gbos-local-auth-ref")
    if (
        not isinstance(authorization, str)
        or not hmac.compare_digest(
            authorization,
            f"Bearer {config.bearer_token}",
        )
        or not isinstance(auth_ref, str)
        or not hmac.compare_digest(auth_ref, config.auth_ref)
    ):
        return _error(request, 401, "authentication_required")
    for header in (
        "x-site-id",
        "x-processing-purpose",
        "x-request-id",
    ):
        value = request.headers.get(header)
        if value is None or _BOUND_ID.fullmatch(value) is None:
            return _error(request, 422, "invalid_query")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            return _error(request, 422, "invalid_query")
        if size < 0:
            return _error(request, 422, "invalid_query")
        if size > config.max_request_bytes:
            return _error(request, 413, "invalid_query")
    body = await request.body()
    if len(body) > config.max_request_bytes:
        return _error(request, 413, "invalid_query")
    return None


def _governed_scope(
    request: Request,
    *,
    expected_purpose: str,
) -> tuple[TenantScope, str]:
    purpose = request.headers["x-processing-purpose"]
    if not hmac.compare_digest(purpose, expected_purpose):
        raise PermissionError("processing purpose mismatch")
    try:
        scope = TenantScope(
            request.headers["x-site-id"],
            "observation_processing",
        )
    except ValueError as exc:
        raise ValueError("invalid site scope") from exc
    return scope, request.headers["x-request-id"]


def _communication_access(
    payload: CommunicationListRequest | CommunicationGetRequest,
) -> CommunicationAccess:
    actor_ref = payload.actor_ref.strip()
    team_refs = [value.strip() for value in payload.allowed_team_refs]
    if (
        not actor_ref
        or any(not value or len(value) > 256 for value in team_refs)
        or len(team_refs) != len(set(team_refs))
    ):
        raise ValueError("invalid communication scope")
    if payload.scope == "all_business_projection":
        if team_refs != ["*"]:
            raise PermissionError("all-business scope requires wildcard authority")
        return CommunicationAccess(
            team_refs=frozenset(),
            actor_ref=actor_ref,
            allow_all_teams=True,
        )
    if "*" in team_refs:
        raise PermissionError("team scope cannot include wildcard authority")
    return CommunicationAccess(
        team_refs=frozenset(team_refs),
        actor_ref=actor_ref,
    )


def _bff_envelope(
    data: dict[str, object],
    *,
    site_id: str,
    request_id: str,
    replayed: bool | None = None,
) -> dict[str, object]:
    meta: dict[str, object] = {
        "request_id": request_id,
        "schema_version": "1.0",
    }
    if replayed is not None:
        meta["replayed"] = replayed
    return {
        "site_id": site_id,
        "data": data,
        "meta": meta,
    }


def _safe_secret(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 2_048
        or value != value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError(f"invalid {field_name}")


def _require_loopback(bind_host: str) -> None:
    if bind_host == "localhost":
        return
    candidate = bind_host.removeprefix("[").removesuffix("]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise ValueError("local pilot API must bind to a loopback host")


def _error(request: Request, status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": code,
                "request_id": request.headers.get("X-Request-ID", "unknown"),
                "details": {},
            }
        },
    )
