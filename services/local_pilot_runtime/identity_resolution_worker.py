"""Default-off, fenced runtime for governed Frappe identity resolution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, Protocol, TypeVar, cast

import httpx

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.observer.observer.identity_projection_outbox import (
    IdentityProjectionRelayClaim,
    PostgresIdentityProjectionOutbox,
)
from services.observer.observer.identity_resolution import (
    IdentityResolutionConflict,
    IdentityResolutionRepository,
    ParticipantIdentityResolution,
    PostgresIdentityResolutionRepository,
)
from services.observer.observer.identity_resolution_work import (
    IdentityResolutionLeaseConflict,
    IdentityResolutionWorkClaim,
    IdentityResolutionWorkRepository,
    PostgresIdentityResolutionWorkRepository,
)
from services.observer.observer.models import TenantScope, _require_aware
from services.observer.observer.storage import Connection

from .email_gateway_config import EMAIL_GATEWAY_API_URL
from .email_gateway_worker import RelayResult, RelayStatus
from .runtime_support import (
    PostgresSettings,
    RuntimeSupportError,
    close_connection,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_API_KEY_FILE = Path("/run/secrets/identity_resolver_api_key")
DEFAULT_API_SECRET_FILE = Path("/run/secrets/identity_resolver_api_secret")
DEFAULT_IDENTITY_PROJECTION_BEARER_FILE = Path("/run/secrets/identity_projection_bearer")
DEFAULT_IDENTITY_PROJECTOR_PASSWORD_FILE = Path(
    "/run/secrets/postgres_observer_identity_projector_password"
)
DEFAULT_FRAPPE_BASE_URL = "http://frappe-backend:8000"
DEFAULT_FRAPPE_UNIX_SOCKET = Path("/run/gbos/sockets/frappe.sock")

_RESOLVE_PATH = "/api/method/esan_gbos.api.internal.identity_resolution.resolve"
_AUTH_REF = "observer-identity-resolver-v1"
_FRAPPE_PURPOSE = "identity_resolution"
_WORK_PURPOSE = "observation_processing"
_MAX_REQUEST_BYTES = 16_384
_MAX_RESPONSE_BYTES = 65_536
_RESPONSE_FIELDS = frozenset({"resolutions"})
_RESOLUTION_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "identity_provider",
        "external_subject_ref",
        "mapping_ref",
        "mapping_revision",
        "team_ref",
        "target_type",
        "target_ref",
        "status",
        "resolved_at",
    }
)
_ERROR_RESPONSE_FIELDS = frozenset({"error"})
_ERROR_FIELDS = frozenset({"code"})
_SAFE_HEADER = re.compile(r"^[^\x00-\x20\x7f]{1,256}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})

ErrorCode = Literal[
    "authentication_failed",
    "invalid_resolver_response",
    "permission_denied",
    "resolver_timeout",
    "resolver_unavailable",
    "team_mismatch",
]
ResolutionStatus = Literal["unresolved", "confirmed", "revoked"]
T = TypeVar("T")


class IdentityResolutionClientError(RuntimeError):
    """A fixed safe category for a closed resolver transport or response failure."""

    __slots__ = ("code", "transient")

    def __init__(self, message: str, *, code: ErrorCode, transient: bool) -> None:
        del message
        super().__init__("identity resolver endpoint failed closed")
        self.code = code
        self.transient = transient

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"transient={self.transient!r}, details=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class IdentityLookupResult:
    status: ResolutionStatus
    resolution: ParticipantIdentityResolution | None

    def __post_init__(self) -> None:
        if self.status == "unresolved":
            if self.resolution is not None:
                raise ValueError("unresolved lookup cannot contain a projection")
        elif self.resolution is None or self.resolution.status != self.status:
            raise ValueError("resolved lookup requires a matching projection")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status!r}, resolution=<redacted>)"


class IdentityResolverTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxIdentityResolverTransport:
    """Bounded POST-only transport with redirects and environment proxies disabled."""

    __slots__ = ("_unix_socket",)

    def __init__(self, unix_socket: Path | None = None) -> None:
        if unix_socket is not None and Path(unix_socket) != DEFAULT_FRAPPE_UNIX_SOCKET:
            raise _client_error("resolver endpoint Unix socket is not allowed")
        self._unix_socket = unix_socket

    def __repr__(self) -> str:
        return f"{type(self).__name__}(endpoint=<redacted>)"

    def request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        if url != DEFAULT_FRAPPE_BASE_URL + _RESOLVE_PATH:
            raise _client_error("resolver endpoint URL is not allowed")
        encoded = _encode_request(payload)
        transport = httpx.HTTPTransport(
            uds=None if self._unix_socket is None else str(self._unix_socket),
            retries=0,
        )
        try:
            with (
                httpx.Client(
                    transport=transport,
                    timeout=httpx.Timeout(timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", url, headers=headers, content=encoded) as response,
            ):
                if 300 <= response.status_code < 400:
                    raise IdentityResolutionClientError(
                        "redirect rejected",
                        code="invalid_resolver_response",
                        transient=False,
                    )
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise IdentityResolutionClientError(
                            "response too large",
                            code="invalid_resolver_response",
                            transient=False,
                        )
                return int(response.status_code), _decode_response(bytes(raw))
        except IdentityResolutionClientError:
            raise
        except httpx.TimeoutException:
            raise IdentityResolutionClientError(
                "timeout",
                code="resolver_timeout",
                transient=True,
            ) from None
        except httpx.HTTPError, OSError:
            raise IdentityResolutionClientError(
                "unavailable",
                code="resolver_unavailable",
                transient=True,
            ) from None


class FrappeIdentityResolverClient:
    """Resolve exactly one opaque identity through one fixed internal Frappe method."""

    __slots__ = (
        "_api_key",
        "_api_secret",
        "_auth_ref",
        "_site_id",
        "_timeout_seconds",
        "_transport",
    )

    def __init__(
        self,
        *,
        base_url: str,
        unix_socket: Path | None,
        site_id: str,
        auth_ref: str,
        api_key: str,
        api_secret: str,
        timeout_seconds: float,
        lease_duration: timedelta,
        transport: IdentityResolverTransport | None = None,
    ) -> None:
        if base_url != DEFAULT_FRAPPE_BASE_URL:
            raise _client_error("resolver endpoint is not allowed")
        if unix_socket is not None and Path(unix_socket) != DEFAULT_FRAPPE_UNIX_SOCKET:
            raise _client_error("resolver endpoint Unix socket is not allowed")
        if (
            not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) < lease_duration.total_seconds()
            or lease_duration > timedelta(hours=1)
        ):
            raise _client_error("resolver timeout must be shorter than the lease")
        self._site_id = _header(site_id)
        self._auth_ref = _header(auth_ref)
        if self._auth_ref != _AUTH_REF:
            raise _client_error("resolver authentication reference is not allowed")
        self._api_key = _header(api_key)
        self._api_secret = _header(api_secret)
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport or HttpxIdentityResolverTransport(unix_socket)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self._site_id!r}, "
            "endpoint=<redacted>, auth_ref=<redacted>, credentials=<redacted>)"
        )

    def resolve(
        self,
        *,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        request_id: str,
    ) -> IdentityLookupResult:
        _validate_lookup_input(identity_provider, identity_ref, team_ref, request_id)
        request = {
            "site_id": self._site_id,
            "processing_purpose": _FRAPPE_PURPOSE,
            "request_id": request_id,
            "auth_ref": self._auth_ref,
            "lookups": [
                {
                    "identity_provider": identity_provider,
                    "external_subject_ref": identity_ref,
                    "expected_team_ref": team_ref,
                }
            ],
        }
        request_envelope = {"payload": request}
        _encode_request(request_envelope)
        try:
            status, raw_response = self._transport.request(
                url=DEFAULT_FRAPPE_BASE_URL + _RESOLVE_PATH,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"token {self._api_key}:{self._api_secret}",
                    "Content-Type": "application/json",
                    "Host": self._site_id,
                    "X-GBOS-Frappe-Auth-Ref": self._auth_ref,
                    "X-Processing-Purpose": _FRAPPE_PURPOSE,
                    "X-Request-ID": request_id,
                    "X-Site-ID": self._site_id,
                },
                payload=request_envelope,
                timeout_seconds=self._timeout_seconds,
            )
        except IdentityResolutionClientError:
            raise
        except httpx.TimeoutException:
            raise IdentityResolutionClientError(
                "timeout",
                code="resolver_timeout",
                transient=True,
            ) from None
        except httpx.HTTPError, OSError:
            raise IdentityResolutionClientError(
                "unavailable",
                code="resolver_unavailable",
                transient=True,
            ) from None
        except Exception:
            raise _client_error("resolver transport violated its contract") from None
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise _client_error("resolver returned an invalid status")
        raw_response = _bounded_transport_response(raw_response)
        response = _unwrap_message(raw_response)
        if status == 404 and _error_code(response) == "mapping_not_resolved":
            return IdentityLookupResult(status="unresolved", resolution=None)
        if status != 200:
            raise _status_error(status, _error_code(response))
        resolution = _parse_resolution(
            response,
            site_id=self._site_id,
            identity_provider=identity_provider,
            identity_ref=identity_ref,
            team_ref=team_ref,
        )
        return IdentityLookupResult(
            status=cast(Literal["confirmed", "revoked"], resolution.status),
            resolution=resolution,
        )


class IdentityProjectionOutbox(Protocol):
    def claim(
        self,
        *,
        site_id: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityProjectionRelayClaim | None: ...

    def heartbeat(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None: ...

    def acknowledge(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        receipt_ref: str,
    ) -> None: ...

    def fail(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        retryable: bool,
    ) -> str: ...


class IdentityProjectionTransport(Protocol):
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxIdentityProjectionTransport:
    """Proxy-free, redirect-free transport for the one internal Gateway boundary."""

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        expected = EMAIL_GATEWAY_API_URL + "/internal/v1/identity-projections/accept"
        if url != expected:
            raise ValueError("identity projection endpoint rejected")
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("identity projection request is too large")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(url, headers=dict(headers), content=encoded)
        except httpx.TimeoutException:
            raise
        except httpx.HTTPError as exc:
            raise ValueError("identity projection transport rejected") from exc
        if 300 <= response.status_code < 400 or len(response.content) > _MAX_RESPONSE_BYTES:
            raise ValueError("identity projection response rejected")
        try:
            value = response.json()
        except ValueError as exc:
            raise ValueError("identity projection response rejected") from exc
        if not isinstance(value, dict):
            raise ValueError("identity projection response rejected")
        return int(response.status_code), value


class IdentityProjectionOutboxAdapter:
    """Bind one least-privilege outbox repository to its exact site."""

    def __init__(self, outbox: IdentityProjectionOutbox, *, site_id: str) -> None:
        if not site_id or site_id != site_id.strip():
            raise ValueError("invalid identity projection outbox site")
        self._outbox = outbox
        self._site_id = site_id

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityProjectionRelayClaim | None:
        return self._outbox.claim(
            site_id=self._site_id,
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
        )

    def heartbeat(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        self._outbox.heartbeat(
            claim,
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
        )

    def acknowledge(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        receipt_ref: str,
    ) -> None:
        self._outbox.acknowledge(
            claim,
            worker_id=worker_id,
            now=now,
            receipt_ref=receipt_ref,
        )

    def fail(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        retryable: bool,
    ) -> str:
        return self._outbox.fail(
            claim,
            worker_id=worker_id,
            now=now,
            retry_at=retry_at,
            error_code=error_code,
            retryable=retryable,
        )


class IdentityProjectionRelayWorker:
    """Deliver one frozen identity projection with bounded retry and exact receipt."""

    def __init__(
        self,
        *,
        outbox: IdentityProjectionOutboxAdapter,
        transport: IdentityProjectionTransport,
        bearer_token: str,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
        retry_delay: timedelta = timedelta(seconds=30),
        timeout_seconds: float = 5,
    ) -> None:
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or len(bearer_token) > 4096
            or not worker_id
            or worker_id != worker_id.strip()
            or not timedelta(0) < lease_duration <= timedelta(minutes=5)
            or not timedelta(0) < retry_delay <= timedelta(hours=1)
            or not 0 < timeout_seconds < lease_duration.total_seconds()
        ):
            raise ValueError("invalid identity projection relay configuration")
        self._outbox = outbox
        self._transport = transport
        self._bearer_token = bearer_token
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(outbox=<redacted>, transport=<redacted>, "
            "credentials=<redacted>)"
        )

    def run_once(self) -> RelayResult:
        now = self._now()
        claim = self._outbox.claim(
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return RelayResult(RelayStatus.IDLE)
        try:
            self._outbox.heartbeat(
                claim,
                worker_id=self._worker_id,
                now=self._now(),
                lease_duration=self._lease_duration,
            )
        except Exception:
            return RelayResult(RelayStatus.LEASE_LOST, claim.attempt)
        try:
            status, response = self._transport.post(
                url=EMAIL_GATEWAY_API_URL + "/internal/v1/identity-projections/accept",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._bearer_token}",
                    "Content-Type": "application/json",
                    "X-GBOS-Local-Auth-Ref": "observer-identity-projection-v1",
                    "X-Payload-Digest": claim.payload_digest,
                    "X-Processing-Purpose": claim.processing_purpose,
                    "X-Request-ID": claim.request_id,
                    "X-Site-ID": claim.site_id,
                },
                payload=claim.payload,
                timeout_seconds=self._timeout_seconds,
            )
        except httpx.TimeoutException:
            return self._fail(claim, now=now, code="gateway_timeout", retryable=True)
        except Exception:
            return self._fail(
                claim,
                now=now,
                code="gateway_transport_rejected",
                retryable=False,
            )
        receipt = _identity_projection_receipt(status, response, claim)
        if receipt is None:
            retryable = status == 429 or 500 <= status <= 599
            return self._fail(
                claim,
                now=now,
                code=("gateway_retryable" if retryable else "gateway_rejected"),
                retryable=retryable,
            )
        try:
            self._outbox.acknowledge(
                claim,
                worker_id=self._worker_id,
                now=self._now(),
                receipt_ref=receipt,
            )
        except Exception:
            return RelayResult(RelayStatus.LEASE_LOST, claim.attempt)
        return RelayResult(RelayStatus.DELIVERED, claim.attempt)

    def _fail(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        now: datetime,
        code: str,
        retryable: bool,
    ) -> RelayResult:
        try:
            state = self._outbox.fail(
                claim,
                worker_id=self._worker_id,
                now=now,
                retry_at=now + self._retry_delay,
                error_code=code,
                retryable=retryable,
            )
        except Exception:
            return RelayResult(RelayStatus.LEASE_LOST, claim.attempt)
        return RelayResult(
            RelayStatus.RETRY if state == "retry" else RelayStatus.DEAD_LETTER,
            claim.attempt,
        )

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, "clock")
        return value.astimezone(UTC)


def _identity_projection_receipt(
    status: int,
    response: object,
    claim: IdentityProjectionRelayClaim,
) -> str | None:
    if (
        status != 200
        or not isinstance(response, dict)
        or set(response) != {"schema_version", "projection_receipt", "payload_digest"}
        or response.get("schema_version") != "1.0"
        or response.get("payload_digest") != claim.payload_digest
        or response.get("projection_receipt") != claim.projection_receipt
    ):
        return None
    return claim.projection_receipt


class IdentityResolutionRunStatus(StrEnum):
    IDLE = "idle"
    UNRESOLVED = "unresolved"
    CONFIRMED = "confirmed"
    REVOKED = "revoked"
    CONFLICT = "conflict"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    CLOSED = "closed"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True, repr=False)
class IdentityResolutionRunResult:
    status: IdentityResolutionRunStatus
    attempt: int | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status!r}, attempt={self.attempt!r})"


class HeartbeatRunner(Protocol):
    def run(self, execute: Callable[[], T], heartbeat: Callable[[], object]) -> T: ...


class ThreadedIdentityResolutionHeartbeatRunner:
    """Renew the queue lease while a blocking Frappe/projection operation runs."""

    __slots__ = ("_interval_seconds",)

    def __init__(self, *, interval_seconds: float) -> None:
        if not 0 < interval_seconds <= 60:
            raise ValueError("heartbeat interval must be positive and bounded")
        self._interval_seconds = interval_seconds

    def run(self, execute: Callable[[], T], heartbeat: Callable[[], object]) -> T:
        stop = Event()
        failure: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self._interval_seconds):
                try:
                    heartbeat()
                except BaseException as exc:
                    failure.append(exc)
                    stop.set()
                    return

        thread = Thread(
            target=renew,
            name="identity-resolution-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            result = execute()
        finally:
            stop.set()
            thread.join()
        if failure:
            raise failure[0]
        return result


class IdentityResolutionWorker:
    """Claim and project one identity under an attempt-bound durable fence."""

    __slots__ = (
        "_client",
        "_clock",
        "_heartbeat_runner",
        "_lease_duration",
        "_projection_repository",
        "_retry_base",
        "_retry_cap",
        "_successful_recheck",
        "_unresolved_recheck",
        "_work_repository",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        work_repository: IdentityResolutionWorkRepository,
        projection_repository: IdentityResolutionRepository,
        client: FrappeIdentityResolverClient,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
        unresolved_recheck: timedelta = timedelta(minutes=5),
        successful_recheck: timedelta = timedelta(hours=1),
        retry_base: timedelta = timedelta(seconds=30),
        retry_cap: timedelta = timedelta(minutes=15),
        heartbeat_runner: HeartbeatRunner | None = None,
    ) -> None:
        if (
            not isinstance(client, FrappeIdentityResolverClient)
            or not callable(clock)
            or _SAFE_REQUEST_ID.fullmatch(worker_id) is None
        ):
            raise ValueError("invalid identity resolution worker configuration")
        for value in (
            lease_duration,
            unresolved_recheck,
            successful_recheck,
            retry_base,
            retry_cap,
        ):
            if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(days=7):
                raise ValueError("identity resolution worker duration is invalid")
        if retry_base > retry_cap:
            raise ValueError("identity resolution retry bounds are invalid")
        self._work_repository = work_repository
        self._projection_repository = projection_repository
        self._client = client
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._unresolved_recheck = unresolved_recheck
        self._successful_recheck = successful_recheck
        self._retry_base = retry_base
        self._retry_cap = retry_cap
        self._heartbeat_runner = heartbeat_runner or ThreadedIdentityResolutionHeartbeatRunner(
            interval_seconds=max(0.1, lease_duration.total_seconds() / 3)
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(repositories=<redacted>, client=<redacted>, "
            "worker_id=<redacted>)"
        )

    def run_once(self, scope: TenantScope) -> IdentityResolutionRunResult:
        now = self._now()
        self._work_repository.record_worker_heartbeat(scope, now=now)
        claim = self._work_repository.claim(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return IdentityResolutionRunResult(IdentityResolutionRunStatus.IDLE)
        try:
            self._validate_claim(scope, claim)

            def heartbeat() -> object:
                return self._work_repository.heartbeat(
                    scope,
                    claim.work_id,
                    worker_id=self._worker_id,
                    fence_token=claim.fence_token,
                    now=self._now(),
                    lease_duration=self._lease_duration,
                )

            lookup = self._heartbeat_runner.run(
                lambda: self._resolve(claim),
                heartbeat,
            )
            resolution = lookup.resolution
            if resolution is not None:
                self._heartbeat_runner.run(
                    lambda: self._project(scope, resolution),
                    heartbeat,
                )
        except IdentityResolutionLeaseConflict:
            return IdentityResolutionRunResult(
                IdentityResolutionRunStatus.LEASE_LOST,
                claim.attempt_count,
            )
        except IdentityResolutionConflict:
            return self._record_conflict(scope, claim)
        except IdentityResolutionClientError as exc:
            return self._record_failure(scope, claim, exc.code, closed=not exc.transient)
        except Exception:
            return self._record_failure(scope, claim, "resolver_unavailable", closed=False)

        outcome = lookup.status
        recheck = self._unresolved_recheck if outcome == "unresolved" else self._successful_recheck
        finished_at = self._now()
        try:
            self._work_repository.record_outcome(
                scope,
                claim.work_id,
                worker_id=self._worker_id,
                fence_token=claim.fence_token,
                now=finished_at,
                outcome=outcome,
                latency=max(timedelta(0), finished_at - now),
                recheck_at=finished_at + recheck,
            )
        except IdentityResolutionLeaseConflict:
            return IdentityResolutionRunResult(
                IdentityResolutionRunStatus.LEASE_LOST,
                claim.attempt_count,
            )
        return IdentityResolutionRunResult(
            IdentityResolutionRunStatus(outcome),
            claim.attempt_count,
        )

    def _resolve(self, claim: IdentityResolutionWorkClaim) -> IdentityLookupResult:
        return self._client.resolve(
            identity_provider=claim.identity_provider,
            identity_ref=claim.identity_ref,
            team_ref=claim.team_ref,
            request_id=_request_id(claim),
        )

    def _project(
        self,
        scope: TenantScope,
        resolution: ParticipantIdentityResolution,
    ) -> None:
        recorded_at = self._now()
        if resolution.resolved_at > recorded_at:
            raise _client_error("resolver timestamp is in the future")
        recorded = ParticipantIdentityResolution(
            site_id=resolution.site_id,
            identity_provider=resolution.identity_provider,
            external_subject_ref=resolution.external_subject_ref,
            mapping_ref=resolution.mapping_ref,
            mapping_revision=resolution.mapping_revision,
            team_ref=resolution.team_ref,
            target_type=resolution.target_type,
            target_ref=resolution.target_ref,
            status=resolution.status,
            resolved_at=resolution.resolved_at,
            recorded_at=recorded_at,
        )
        self._projection_repository.record(scope, recorded)

    def _record_conflict(
        self,
        scope: TenantScope,
        claim: IdentityResolutionWorkClaim,
    ) -> IdentityResolutionRunResult:
        now = self._now()
        try:
            self._work_repository.record_outcome(
                scope,
                claim.work_id,
                worker_id=self._worker_id,
                fence_token=claim.fence_token,
                now=now,
                outcome="conflict",
                latency=timedelta(0),
            )
        except IdentityResolutionLeaseConflict:
            return IdentityResolutionRunResult(
                IdentityResolutionRunStatus.LEASE_LOST,
                claim.attempt_count,
            )
        return IdentityResolutionRunResult(
            IdentityResolutionRunStatus.CONFLICT,
            claim.attempt_count,
        )

    def _record_failure(
        self,
        scope: TenantScope,
        claim: IdentityResolutionWorkClaim,
        code: ErrorCode,
        *,
        closed: bool,
    ) -> IdentityResolutionRunResult:
        now = self._now()
        multiplier = 1 << min(max(claim.attempt_count - 1, 0), 16)
        retry_delay = min(self._retry_base * multiplier, self._retry_cap)
        try:
            failed = self._work_repository.mark_failed(
                scope,
                claim.work_id,
                worker_id=self._worker_id,
                fence_token=claim.fence_token,
                now=now,
                retry_at=now + retry_delay,
                error_code=code,
            )
        except IdentityResolutionLeaseConflict:
            return IdentityResolutionRunResult(
                IdentityResolutionRunStatus.LEASE_LOST,
                claim.attempt_count,
            )
        if closed:
            status = IdentityResolutionRunStatus.CLOSED
        elif failed.status == "dead_letter":
            status = IdentityResolutionRunStatus.DEAD_LETTER
        else:
            status = IdentityResolutionRunStatus.RETRY
        return IdentityResolutionRunResult(status, claim.attempt_count)

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now.astimezone(UTC)

    def _validate_claim(self, scope: TenantScope, claim: IdentityResolutionWorkClaim) -> None:
        if (
            scope.processing_purpose != _WORK_PURPOSE
            or claim.site_id != scope.site_id
            or claim.status != "leased"
            or claim.lease_owner != self._worker_id
            or claim.lease_expires_at is None
            or claim.lease_expires_at <= self._now()
        ):
            raise IdentityResolutionLeaseConflict("identity resolution lease is no longer owned")


@dataclass(frozen=True, slots=True, repr=False)
class IdentityResolutionComponents:
    work_repository: IdentityResolutionWorkRepository
    projection_repository: IdentityResolutionRepository

    def __repr__(self) -> str:
        return f"{type(self).__name__}(repositories=<redacted>)"


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


class IdentityProjectionRelayRunner(Protocol):
    def run_once(self) -> RelayResult: ...


def run_worker_daemon(
    worker: IdentityResolutionWorker,
    scope: TenantScope,
    *,
    stop_event: StopEvent,
    idle_delay_seconds: float,
    identity_projection_worker: IdentityProjectionRelayRunner | None = None,
) -> None:
    if (
        not callable(getattr(worker, "run_once", None))
        or isinstance(idle_delay_seconds, bool)
        or not isinstance(idle_delay_seconds, int | float)
        or not 0 < idle_delay_seconds <= 60
    ):
        raise ValueError("invalid identity resolution daemon composition")
    while not stop_event.is_set():
        result = worker.run_once(scope)
        if result.status is IdentityResolutionRunStatus.CLOSED:
            raise RuntimeSupportError("identity resolver readiness failed closed")
        relay_result = (
            RelayResult(RelayStatus.IDLE)
            if identity_projection_worker is None
            else identity_projection_worker.run_once()
        )
        if (
            result.status is IdentityResolutionRunStatus.IDLE
            and relay_result.status is RelayStatus.IDLE
        ):
            stop_event.wait(float(idle_delay_seconds))


ComponentsFactory = Callable[[object], IdentityResolutionComponents]
TransportFactory = Callable[[Path | None], IdentityResolverTransport]


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    api_key_file: Path = DEFAULT_API_KEY_FILE,
    api_secret_file: Path = DEFAULT_API_SECRET_FILE,
    identity_projection_bearer_file: Path = DEFAULT_IDENTITY_PROJECTION_BEARER_FILE,
    identity_projector_password_file: Path = DEFAULT_IDENTITY_PROJECTOR_PASSWORD_FILE,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    components_factory: ComponentsFactory | None = None,
    transport_factory: TransportFactory | None = None,
    identity_projection_transport_factory: Callable[[], IdentityProjectionTransport] | None = None,
    daemon_runner: Callable[..., None] | None = None,
    stop_event: StopEvent | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Preflight without DB/HTTP side effects, then run the internal-only worker."""

    environment = os.environ if environ is None else environ
    connection: object | None = None
    identity_projector_connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        if environment.get("GBOS_IDENTITY_RESOLUTION_KILL_SWITCH") != "false":
            raise LocalEntrypointDisabled("identity resolution worker is kill-switched")
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="identity-resolution-worker",
            environ=environment,
        )
        if not _identity_capable_channel_enabled(manifest):
            raise LocalEntrypointDisabled(
                "identity resolution worker requires an enabled identity-capable channel"
            )
        runtime = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, runtime)
        if runtime.context_endpoint.base_url != DEFAULT_FRAPPE_BASE_URL:
            raise RuntimeSupportError("identity resolver endpoint is not allowed")
        socket = runtime.context_endpoint.unix_socket
        if socket is not None and socket != DEFAULT_FRAPPE_UNIX_SOCKET:
            raise RuntimeSupportError("identity resolver Unix socket is not allowed")
        lease_duration = timedelta(seconds=runtime.worker.heartbeat_interval_seconds * 10)
        timeout_seconds = 3.0
        if timeout_seconds >= lease_duration.total_seconds():
            raise RuntimeSupportError("identity resolver timeout must be shorter than its lease")

        api_key = load_secret_file(api_key_file)
        api_secret = load_secret_file(api_secret_file)
        identity_projection_enabled = _identity_projection_enabled(manifest, environment)
        identity_projection_bearer = None
        identity_projector_password = None
        if identity_projection_enabled:
            identity_projection_bearer = load_secret_file(identity_projection_bearer_file)
            identity_projector_password = load_secret_file(identity_projector_password_file)
            if not identity_projection_bearer.reveal() or not identity_projector_password.reveal():
                raise RuntimeSupportError("identity projection credentials unavailable")
        active_transport = (
            HttpxIdentityResolverTransport(socket)
            if transport_factory is None
            else transport_factory(socket)
        )
        client = FrappeIdentityResolverClient(
            base_url=runtime.context_endpoint.base_url,
            unix_socket=socket,
            site_id=runtime.site_id,
            auth_ref=_AUTH_REF,
            api_key=api_key.reveal(),
            api_secret=api_secret.reveal(),
            timeout_seconds=timeout_seconds,
            lease_duration=lease_duration,
            transport=active_transport,
        )
        connection = connect_postgres(runtime.postgres, connector=connector)
        components = (
            IdentityResolutionComponents(
                work_repository=PostgresIdentityResolutionWorkRepository(
                    cast(Connection, connection)
                ),
                projection_repository=PostgresIdentityResolutionRepository(
                    cast(Connection, connection)
                ),
            )
            if components_factory is None
            else components_factory(connection)
        )
        active_clock = clock or _utc_now
        worker = IdentityResolutionWorker(
            work_repository=components.work_repository,
            projection_repository=components.projection_repository,
            client=client,
            worker_id=runtime.worker.worker_id,
            clock=active_clock,
            lease_duration=lease_duration,
            heartbeat_runner=ThreadedIdentityResolutionHeartbeatRunner(
                interval_seconds=runtime.worker.heartbeat_interval_seconds
            ),
        )
        identity_projection_worker = None
        if identity_projection_enabled:
            assert identity_projection_bearer is not None
            projector_settings = PostgresSettings(
                host=runtime.postgres.host,
                port=runtime.postgres.port,
                database=runtime.postgres.database,
                user="gbos_observer_identity_projector",
                password_file=identity_projector_password_file,
                connect_timeout_seconds=runtime.postgres.connect_timeout_seconds,
            )
            identity_projector_connection = connect_postgres(
                projector_settings,
                connector=connector,
            )
            identity_projection_worker = IdentityProjectionRelayWorker(
                outbox=IdentityProjectionOutboxAdapter(
                    PostgresIdentityProjectionOutbox(
                        cast(Connection, identity_projector_connection)
                    ),
                    site_id=runtime.site_id,
                ),
                transport=(
                    HttpxIdentityProjectionTransport()
                    if identity_projection_transport_factory is None
                    else identity_projection_transport_factory()
                ),
                bearer_token=identity_projection_bearer.reveal(),
                worker_id=f"{runtime.worker.worker_id}-projection",
                clock=active_clock,
                lease_duration=lease_duration,
                retry_delay=timedelta(seconds=30),
                timeout_seconds=timeout_seconds,
            )
        runner: Callable[..., None] = daemon_runner or run_worker_daemon
        runner_kwargs: dict[str, object] = {
            "stop_event": stop_event or Event(),
            "idle_delay_seconds": runtime.worker.idle_delay_seconds,
        }
        if identity_projection_worker is not None:
            runner_kwargs["identity_projection_worker"] = identity_projection_worker
        runner(worker, TenantScope(runtime.site_id, _WORK_PURPOSE), **runner_kwargs)
        return 0
    except Exception:
        return 78
    finally:
        if identity_projector_connection is not None:
            with suppress(Exception):
                close_connection(identity_projector_connection)
        if connection is not None:
            with suppress(Exception):
                close_connection(connection)


def _identity_capable_channel_enabled(manifest: Mapping[str, Any]) -> bool:
    channels = manifest.get("channels")
    if not isinstance(channels, Mapping):
        return False
    return any(
        isinstance(channels.get(channel), Mapping) and channels[channel].get("enabled") is True
        for channel in ("email", "wecom", "whatsapp")
    )


def _identity_projection_enabled(
    manifest: Mapping[str, Any],
    environment: Mapping[str, str],
) -> bool:
    gateway = manifest.get("email_gateway")
    return (
        environment.get("GBOS_IDENTITY_PROJECTION_KILL_SWITCH", "true") == "false"
        and isinstance(gateway, Mapping)
        and gateway.get("kill_switch") is False
        and gateway.get("identity_projection_kill_switch") is False
    )


def _parse_resolution(
    response: Mapping[str, Any],
    *,
    site_id: str,
    identity_provider: str,
    identity_ref: str,
    team_ref: str,
) -> ParticipantIdentityResolution:
    try:
        if set(response) != set(_RESPONSE_FIELDS):
            raise ValueError
        values = response["resolutions"]
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError
        value = values[0]
        if not isinstance(value, dict) or set(value) != set(_RESOLUTION_FIELDS):
            raise ValueError
        if value.get("schema_version") != "1.0":
            raise ValueError
        if value.get("site_id") != site_id:
            raise ValueError
        if value.get("identity_provider") != identity_provider:
            raise ValueError
        if value.get("external_subject_ref") != identity_ref:
            raise ValueError
        if value.get("team_ref") != team_ref:
            raise IdentityResolutionClientError(
                "team mismatch",
                code="team_mismatch",
                transient=False,
            )
        mapping_ref = value.get("mapping_ref")
        mapping_revision = value.get("mapping_revision")
        target_type = value.get("target_type")
        target_ref = value.get("target_ref")
        status = value.get("status")
        if (
            not isinstance(mapping_ref, str)
            or not isinstance(mapping_revision, int)
            or isinstance(mapping_revision, bool)
            or not isinstance(target_type, str)
            or not isinstance(target_ref, str)
            or not isinstance(status, str)
        ):
            raise ValueError
        resolved_at = _timestamp(value.get("resolved_at"))
        resolution = ParticipantIdentityResolution(
            site_id=site_id,
            identity_provider=identity_provider,
            external_subject_ref=identity_ref,
            mapping_ref=mapping_ref,
            mapping_revision=mapping_revision,
            team_ref=team_ref,
            target_type=target_type,
            target_ref=target_ref,
            status=status,
            resolved_at=resolved_at,
            recorded_at=resolved_at,
        )
    except IdentityResolutionClientError:
        raise
    except TypeError, ValueError:
        raise _client_error("resolver response is malformed") from None
    return resolution


def _status_error(status: int, error_code: str | None) -> IdentityResolutionClientError:
    if status == 401:
        return IdentityResolutionClientError(
            "authentication rejected",
            code="authentication_failed",
            transient=False,
        )
    if status == 403:
        code: ErrorCode = (
            "team_mismatch" if error_code == "team_scope_mismatch" else "permission_denied"
        )
        return IdentityResolutionClientError("permission rejected", code=code, transient=False)
    if status == 429 or 500 <= status <= 599:
        return IdentityResolutionClientError(
            "resolver unavailable",
            code="resolver_unavailable",
            transient=True,
        )
    return _client_error("resolver returned an invalid status")


def _unwrap_message(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != {"message"}:
        raise _client_error("resolver response envelope is malformed")
    message = value.get("message")
    if not isinstance(message, dict):
        raise _client_error("resolver response envelope is malformed")
    return message


def _error_code(value: Mapping[str, Any]) -> str | None:
    if set(value) != set(_ERROR_RESPONSE_FIELDS):
        return None
    error = value.get("error")
    if not isinstance(error, dict) or set(error) != set(_ERROR_FIELDS):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError from None
    _require_aware(parsed, "resolved_at")
    return parsed


def _validate_lookup_input(
    identity_provider: str,
    identity_ref: str,
    team_ref: str,
    request_id: str,
) -> None:
    try:
        ParticipantIdentityResolution(
            site_id="validate.invalid",
            identity_provider=identity_provider,
            external_subject_ref=identity_ref,
            mapping_ref="EID-00000000000000000000000000",
            mapping_revision=1,
            team_ref=team_ref,
            target_type="User",
            target_ref="validate.invalid",
            status="confirmed",
            resolved_at=datetime(2000, 1, 1, tzinfo=UTC),
            recorded_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
    except TypeError, ValueError:
        raise _client_error("resolver lookup is invalid") from None
    if identity_provider not in _PROVIDERS or _SAFE_REQUEST_ID.fullmatch(request_id) is None:
        raise _client_error("resolver lookup is invalid")


def _request_id(claim: IdentityResolutionWorkClaim) -> str:
    return (
        f"identity-resolution-{claim.work_id[4:36]}-{claim.attempt_count}-{claim.lease_generation}"
    )


def _header(value: object) -> str:
    if not isinstance(value, str) or _SAFE_HEADER.fullmatch(value) is None:
        raise _client_error("resolver header is invalid")
    return value


def _encode_request(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except TypeError, ValueError:
        raise _client_error("resolver request is invalid JSON") from None
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise _client_error("resolver request is too large")
    return encoded


def _decode_response(value: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        raise _client_error("resolver response is invalid JSON") from None
    if not isinstance(decoded, dict):
        raise _client_error("resolver response is not an object")
    return decoded


def _bounded_transport_response(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _client_error("resolver response is not an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    except TypeError, ValueError:
        raise _client_error("resolver response is invalid JSON") from None
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise _client_error("resolver response is too large")
    return value


def _client_error(_message: str) -> IdentityResolutionClientError:
    return IdentityResolutionClientError(
        "invalid resolver response",
        code="invalid_resolver_response",
        transient=False,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_API_KEY_FILE",
    "DEFAULT_API_SECRET_FILE",
    "DEFAULT_IDENTITY_PROJECTION_BEARER_FILE",
    "DEFAULT_IDENTITY_PROJECTOR_PASSWORD_FILE",
    "DEFAULT_FRAPPE_BASE_URL",
    "DEFAULT_FRAPPE_UNIX_SOCKET",
    "FrappeIdentityResolverClient",
    "HeartbeatRunner",
    "HttpxIdentityResolverTransport",
    "HttpxIdentityProjectionTransport",
    "IdentityProjectionOutboxAdapter",
    "IdentityProjectionRelayWorker",
    "IdentityLookupResult",
    "IdentityResolutionClientError",
    "IdentityResolutionComponents",
    "IdentityResolutionRunResult",
    "IdentityResolutionRunStatus",
    "IdentityResolutionWorker",
    "ThreadedIdentityResolutionHeartbeatRunner",
    "main",
    "run_worker_daemon",
]
