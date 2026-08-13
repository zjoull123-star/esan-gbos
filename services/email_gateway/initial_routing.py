from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from .models import AuthorityRoute, IdentityProjection, TenantScope, ValidationError, stable_ref
from .operations import InboxOperations
from .repositories.identity_route_work import (
    IdentityRouteCandidate,
    IdentityRouteLeaseLost,
    IdentityRouteWorkClaim,
)

FRAPPE_INITIAL_ROUTE_URL = (
    "http://frappe-backend:8000/api/method/"
    "esan_gbos.api.internal.email_gateway_authority.resolve_initial_route"
)
_AUTHORITY_PURPOSE = "email_gateway_authority"


class InitialRouteTransportTimeout(RuntimeError):
    """The exact internal request timed out without a usable authority response."""


class RetryableInitialRouteError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PermanentInitialRouteError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InitialRouteTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class FrappeInitialRouteClient:
    """Closed client for one internal, current Frappe route authority read."""

    def __init__(
        self,
        *,
        transport: InitialRouteTransport,
        api_key: str,
        api_secret: str,
        auth_ref: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if (
            not 16 <= len(api_key) <= 128
            or not 16 <= len(api_secret) <= 128
            or ":" in api_key
            or api_key != api_key.strip()
            or api_secret != api_secret.strip()
            or auth_ref != "email-gateway-authority-v1"
            or not 0 < timeout_seconds <= 10
        ):
            raise ValueError("invalid Frappe initial route client")
        self._transport = transport
        self._authorization = f"token {api_key}:{api_secret}"
        self._auth_ref = auth_ref
        self._timeout_seconds = timeout_seconds

    def resolve(
        self,
        *,
        projection: IdentityProjection,
        request_id: str,
    ) -> AuthorityRoute:
        if (
            not request_id
            or request_id != request_id.strip()
            or projection.identity_type != "Party"
            or projection.status != "confirmed"
        ):
            raise PermanentInitialRouteError("authority_request_rejected")
        request = {
            "site_id": projection.site_id,
            "processing_purpose": _AUTHORITY_PURPOSE,
            "request_id": request_id,
            "auth_ref": self._auth_ref,
            "mapping_ref": projection.external_identity_ref,
            "expected_mapping_revision": projection.external_identity_revision,
            "expected_team_ref": projection.team_ref,
        }
        try:
            status, body = self._transport.post(
                url=FRAPPE_INITIAL_ROUTE_URL,
                headers={
                    "Accept": "application/json",
                    "Authorization": self._authorization,
                    "Content-Type": "application/json",
                    "Host": projection.site_id,
                    "X-GBOS-Frappe-Auth-Ref": self._auth_ref,
                    "X-Processing-Purpose": _AUTHORITY_PURPOSE,
                    "X-Request-ID": request_id,
                    "X-Site-ID": projection.site_id,
                },
                payload={"payload": request},
                timeout_seconds=self._timeout_seconds,
            )
        except InitialRouteTransportTimeout:
            raise RetryableInitialRouteError("authority_timeout") from None
        except RetryableInitialRouteError, PermanentInitialRouteError:
            raise
        except Exception:
            raise PermanentInitialRouteError("authority_transport_rejected") from None
        if status == 429:
            raise RetryableInitialRouteError("authority_rate_limited")
        if 500 <= status <= 599:
            raise RetryableInitialRouteError("authority_server_error")
        if status != 200:
            raise PermanentInitialRouteError("authority_rejected")
        if (
            not isinstance(body, dict)
            or set(body) != {"message"}
            or not isinstance(body.get("message"), dict)
            or set(body["message"]) != {"route_authority"}
        ):
            raise PermanentInitialRouteError("authority_response_invalid")
        try:
            return AuthorityRoute.from_wire(body["message"]["route_authority"])
        except ValidationError:
            raise PermanentInitialRouteError("authority_response_invalid") from None

    def __repr__(self) -> str:
        return "FrappeInitialRouteClient(credentials=<redacted>)"


class InitialRouteStatus(StrEnum):
    IDLE = "idle"
    CONTINUED = "continued"
    COMPLETED = "completed"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    SUPERSEDED = "superseded"
    LEASE_LOST = "lease_lost"


class InitialRouteRepository(Protocol):
    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityRouteWorkClaim | None: ...

    def projection_state(self, scope: TenantScope, claim: IdentityRouteWorkClaim) -> str: ...

    def list_candidate_refs(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[str, ...]: ...

    def load_candidate(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        inbox_item_ref: str,
        *,
        now: datetime,
    ) -> IdentityRouteCandidate | None: ...

    def workflow_for(self, claim: IdentityRouteWorkClaim) -> object: ...

    def complete(
        self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime
    ) -> None: ...

    def continue_work(
        self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime
    ) -> None: ...

    def retry(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        code: str,
        retry_at: datetime,
        now: datetime,
    ) -> None: ...

    def reject(
        self,
        scope: TenantScope,
        claim: IdentityRouteWorkClaim,
        *,
        code: str,
        now: datetime,
    ) -> None: ...

    def supersede(
        self, scope: TenantScope, claim: IdentityRouteWorkClaim, *, now: datetime
    ) -> None: ...


class InitialRouteAuthority(Protocol):
    def resolve(self, *, projection: IdentityProjection, request_id: str) -> AuthorityRoute: ...


class InitialRouteProcessor:
    def __init__(
        self,
        *,
        repository: InitialRouteRepository,
        authority: InitialRouteAuthority,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=30),
        batch_limit: int = 50,
    ) -> None:
        if (
            not worker_id
            or worker_id != worker_id.strip()
            or "@" in worker_id
            or not timedelta(seconds=5) <= lease_duration <= timedelta(minutes=5)
            or not timedelta(seconds=1) <= retry_delay <= timedelta(hours=1)
            or not 1 <= batch_limit <= 100
        ):
            raise ValueError("invalid initial route processor")
        self._repository = repository
        self._authority = authority
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._batch_limit = batch_limit

    def run_once(self, scope: TenantScope) -> InitialRouteResult:
        now = self._clock()
        claim = self._repository.claim(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return InitialRouteResult(InitialRouteStatus.IDLE)
        try:
            state = self._repository.projection_state(scope, claim)
            if state == "superseded":
                self._repository.supersede(scope, claim, now=self._clock())
                return InitialRouteResult(InitialRouteStatus.SUPERSEDED, claim.attempt)
            if state != "current_routeable":
                self._repository.reject(
                    scope, claim, code="projection_not_routeable", now=self._clock()
                )
                return InitialRouteResult(InitialRouteStatus.DEAD_LETTER, claim.attempt)
            refs = self._repository.list_candidate_refs(
                scope,
                claim,
                now=self._clock(),
                limit=self._batch_limit + 1,
            )
            for inbox_ref in refs[: self._batch_limit]:
                candidate = self._repository.load_candidate(
                    scope, claim, inbox_ref, now=self._clock()
                )
                if candidate is None:
                    if self._repository.projection_state(scope, claim) == "superseded":
                        self._repository.supersede(scope, claim, now=self._clock())
                        return InitialRouteResult(InitialRouteStatus.SUPERSEDED, claim.attempt)
                    continue
                request_id = stable_ref(
                    "IRQ",
                    scope.site_id,
                    scope.processing_purpose,
                    inbox_ref,
                    str(claim.mapping_revision),
                )
                try:
                    authority = self._authority.resolve(
                        projection=candidate.projection,
                        request_id=request_id,
                    )
                except RetryableInitialRouteError as error:
                    self._repository.retry(
                        scope,
                        claim,
                        code=error.code,
                        retry_at=self._clock() + self._retry_delay,
                        now=self._clock(),
                    )
                    return InitialRouteResult(InitialRouteStatus.RETRY, claim.attempt)
                except PermanentInitialRouteError as error:
                    code = (
                        "authority_response_invalid"
                        if error.code == "authority_response_invalid"
                        else "authority_rejected"
                    )
                    self._repository.reject(scope, claim, code=code, now=self._clock())
                    return InitialRouteResult(InitialRouteStatus.DEAD_LETTER, claim.attempt)
                target_state = "unassigned"
                assignee_ref: str | None = None
                assignee_team_ref: str | None = None
                assignee_enabled = False
                if (
                    authority.route_status == "assigned"
                    and authority.team_ref == claim.expected_team_ref
                ):
                    target_state = "assigned"
                    assignee_ref = authority.owner_user_ref
                    assignee_team_ref = authority.team_ref
                    assignee_enabled = True
                operation_now = self._clock()
                if operation_now < candidate.inbox.updated_at:
                    operation_now = candidate.inbox.updated_at
                InboxOperations(self._repository.workflow_for(claim)).apply_identity_route(  # type: ignore[arg-type]
                    scope,
                    worker_kind="routing_worker",
                    inbox_item_ref=inbox_ref,
                    target_state=target_state,
                    assignee_user_ref=assignee_ref,
                    assignee_team_ref=assignee_team_ref,
                    assignee_enabled=assignee_enabled,
                    expected_revision=candidate.inbox.revision,
                    request_id=request_id,
                    idempotency_key=f"identity-route:{request_id}",
                    now=operation_now,
                )
            if len(refs) > self._batch_limit:
                self._repository.continue_work(scope, claim, now=self._clock())
                return InitialRouteResult(InitialRouteStatus.CONTINUED, claim.attempt)
            self._repository.complete(scope, claim, now=self._clock())
            return InitialRouteResult(InitialRouteStatus.COMPLETED, claim.attempt)
        except IdentityRouteLeaseLost:
            return InitialRouteResult(InitialRouteStatus.LEASE_LOST, claim.attempt)
        except ValidationError:
            try:
                state = self._repository.projection_state(scope, claim)
                if state == "superseded":
                    self._repository.supersede(scope, claim, now=self._clock())
                    return InitialRouteResult(InitialRouteStatus.SUPERSEDED, claim.attempt)
                self._repository.reject(
                    scope, claim, code="route_apply_rejected", now=self._clock()
                )
            except ValidationError:
                return InitialRouteResult(InitialRouteStatus.LEASE_LOST, claim.attempt)
            return InitialRouteResult(InitialRouteStatus.DEAD_LETTER, claim.attempt)


@dataclass(frozen=True, slots=True)
class InitialRouteResult:
    status: InitialRouteStatus
    attempt: int | None = None


__all__ = [
    "FRAPPE_INITIAL_ROUTE_URL",
    "FrappeInitialRouteClient",
    "InitialRouteProcessor",
    "InitialRouteResult",
    "InitialRouteStatus",
    "InitialRouteTransportTimeout",
    "PermanentInitialRouteError",
    "RetryableInitialRouteError",
]
