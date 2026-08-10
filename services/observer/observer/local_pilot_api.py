from __future__ import annotations

import hmac
import ipaddress
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from .identity_resolution_work import (
    IdentityAuthorityDenial,
    IdentityAuthorityDenialConflict,
    IdentityResolutionWorkSnapshot,
)
from .models import ConnectorKey, TenantScope, _require_aware
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
_IDENTITY_METRICS_PURPOSE = "identity_resolution_metrics"
_IDENTITY_READINESS_WINDOW = timedelta(seconds=30)
_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4"
_OUTCOME_LABELS = ("confirmed", "unresolved", "revoked", "conflict", "error")
_LATENCY_BUCKETS = ("le_100_ms", "le_500_ms", "le_2000_ms", "gt_2000_ms")


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


class IdentityResolutionMetrics(Protocol):
    def snapshot(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        readiness_window: timedelta,
    ) -> IdentityResolutionWorkSnapshot: ...


class IdentityAuthorityDenials(Protocol):
    def record_authority_denial(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        mapping_ref: str,
        team_ref: str,
        deny_through_revision: int,
        reason: Literal["revoked", "superseded", "target_ineligible"],
        denied_at: datetime,
        idempotency_key: str,
    ) -> IdentityAuthorityDenial: ...


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


class IdentityAuthorityDenyRequest(_ClosedModel):
    identity_provider: Literal["email", "wecom", "whatsapp", "phone", "manual_import"]
    external_subject_ref: str = Field(min_length=1, max_length=160)
    mapping_ref: str = Field(pattern=r"^EID-[0-9A-HJKMNP-TV-Z]{26}$")
    team_ref: str = Field(min_length=1, max_length=256)
    deny_through_revision: int = Field(ge=1, le=2_147_483_647)
    reason: Literal["revoked", "superseded", "target_ineligible"]
    idempotency_key: str = Field(min_length=8, max_length=256)


def create_local_pilot_app(
    *,
    config: LocalPilotAPIConfig,
    control: ControlService,
    reader: ReadService,
    guard: LocalPilotRuntimeGuard,
    clock: Clock,
    identity_resolution_metrics: IdentityResolutionMetrics | None = None,
    identity_authority_denials: IdentityAuthorityDenials | None = None,
) -> FastAPI:
    """Create the authenticated Frappe v4 downstream surface without starting I/O."""

    application = FastAPI(
        title="ESAN GBOS Observer Local Pilot",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    active_identity_authority_denials = identity_authority_denials
    if active_identity_authority_denials is None and callable(
        getattr(identity_resolution_metrics, "record_authority_denial", None)
    ):
        active_identity_authority_denials = identity_resolution_metrics  # type: ignore[assignment]

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

    @application.exception_handler(IdentityAuthorityDenialConflict)
    async def authority_denial_conflict(
        request: Request,
        exc: IdentityAuthorityDenialConflict,
    ) -> JSONResponse:
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

    @application.get("/internal/v1/metrics/identity-resolution")
    def identity_resolution_metric_text(request: Request) -> Response:
        guard.require_running()
        scope, _request_id = _governed_scope(
            request,
            expected_purpose=_IDENTITY_METRICS_PURPOSE,
        )
        if identity_resolution_metrics is None:
            return _metrics_unavailable()
        try:
            now = clock()
            _require_aware(now, "metrics clock")
            normalized_now = now.astimezone(UTC)
            snapshot = identity_resolution_metrics.snapshot(
                scope,
                now=normalized_now,
                readiness_window=_IDENTITY_READINESS_WINDOW,
            )
            rendered = _render_identity_resolution_metrics(snapshot, now=normalized_now)
        except Exception:
            return _metrics_unavailable()
        return Response(
            content=rendered,
            media_type=_PROMETHEUS_CONTENT_TYPE,
        )

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

    @application.post("/internal/v1/identity-authority/deny")
    def deny_identity_authority(
        request: Request,
        payload: Annotated[IdentityAuthorityDenyRequest, Body()],
    ) -> dict[str, object]:
        guard.require_running()
        scope, current_request_id = _governed_scope(
            request,
            expected_purpose="identity_authority",
        )
        header_key = request.headers.get("idempotency-key")
        if header_key is None:
            raise ValueError("idempotency key header is required")
        if not hmac.compare_digest(header_key, payload.idempotency_key):
            raise IdempotencyConflict("header and body idempotency keys differ")
        if active_identity_authority_denials is None:
            raise KillSwitchEngaged("identity authority denial storage is unavailable")
        denied_at = clock()
        _require_aware(denied_at, "identity authority clock")
        denial = active_identity_authority_denials.record_authority_denial(
            scope,
            identity_provider=payload.identity_provider,
            identity_ref=payload.external_subject_ref,
            mapping_ref=payload.mapping_ref,
            team_ref=payload.team_ref,
            deny_through_revision=payload.deny_through_revision,
            reason=payload.reason,
            denied_at=denied_at.astimezone(UTC),
            idempotency_key=payload.idempotency_key,
        )
        return _bff_envelope(
            {
                "denial": {
                    "mapping_ref": denial.mapping_ref,
                    "deny_through_revision": denial.deny_through_revision,
                    "status": "denied",
                }
            },
            site_id=scope.site_id,
            request_id=current_request_id,
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


def _render_identity_resolution_metrics(
    snapshot: IdentityResolutionWorkSnapshot,
    *,
    now: datetime,
) -> str:
    if not isinstance(snapshot, IdentityResolutionWorkSnapshot):
        raise ValueError("invalid identity resolution metric snapshot")
    _require_aware(now, "metrics clock")
    heartbeat_age = _age_seconds(snapshot.worker_last_heartbeat_at, now=now)
    ready = snapshot.ready and _heartbeat_is_current(
        snapshot.worker_last_heartbeat_at,
        now=now,
    )
    oldest_age = _optional_count(snapshot.oldest_backlog_age_seconds)
    outcomes = snapshot.request_outcomes
    latency = snapshot.latency_buckets
    if set(outcomes) != set(_OUTCOME_LABELS) or set(latency) != set(_LATENCY_BUCKETS):
        raise ValueError("invalid identity resolution metric dimensions")
    outcome_values = {name: _count(outcomes[name]) for name in _OUTCOME_LABELS}
    latency_values = {name: _count(latency[name]) for name in _LATENCY_BUCKETS}
    le_100 = latency_values["le_100_ms"]
    le_500 = le_100 + latency_values["le_500_ms"]
    le_2000 = le_500 + latency_values["le_2000_ms"]
    total = le_2000 + latency_values["gt_2000_ms"]
    lines = [
        f"gbos_identity_resolver_ready {1 if ready else 0}",
        f"gbos_identity_resolver_heartbeat_age_seconds {_number(heartbeat_age)}",
        f"gbos_identity_resolver_backlog {_count(snapshot.backlog_count)}",
        f"gbos_identity_resolver_oldest_work_age_seconds {_number(oldest_age)}",
        f"gbos_identity_resolver_unresolved {_count(snapshot.unresolved_count)}",
        f"gbos_identity_resolver_conflicts {_count(snapshot.conflict_count)}",
    ]
    lines.extend(
        f'gbos_identity_resolver_requests_total{{outcome="{name}"}} {outcome_values[name]}'
        for name in _OUTCOME_LABELS
    )
    lines.extend(
        (
            f'gbos_identity_resolver_request_duration_seconds_bucket{{le="0.1"}} {le_100}',
            f'gbos_identity_resolver_request_duration_seconds_bucket{{le="0.5"}} {le_500}',
            f'gbos_identity_resolver_request_duration_seconds_bucket{{le="2"}} {le_2000}',
            f'gbos_identity_resolver_request_duration_seconds_bucket{{le="+Inf"}} {total}',
            f"gbos_identity_resolver_request_duration_seconds_count {total}",
        )
    )
    return "\n".join(lines) + "\n"


def _age_seconds(value: datetime | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    _require_aware(value, "worker heartbeat")
    return max(0, int((now - value.astimezone(UTC)).total_seconds()))


def _heartbeat_is_current(value: datetime | None, *, now: datetime) -> bool:
    if value is None:
        return False
    _require_aware(value, "worker heartbeat")
    normalized = value.astimezone(UTC)
    return now - _IDENTITY_READINESS_WINDOW <= normalized <= now


def _optional_count(value: int | None) -> int | None:
    return None if value is None else _count(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid identity resolution metric counter")
    return value


def _number(value: int | None) -> str:
    return "NaN" if value is None else str(value)


def _metrics_unavailable() -> Response:
    return Response(
        status_code=503,
        content="",
        media_type=_PROMETHEUS_CONTENT_TYPE,
    )


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
