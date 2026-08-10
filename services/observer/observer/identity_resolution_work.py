"""Durable, site-scoped work for resolving opaque participant identities."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .models import TenantScope, _require_aware

WorkStatus = Literal[
    "queued",
    "leased",
    "retry_wait",
    "unresolved",
    "confirmed",
    "revoked",
    "conflict",
    "dead_letter",
]
ResolutionOutcome = Literal["unresolved", "confirmed", "revoked", "conflict"]
LastResolutionStatus = Literal["unresolved", "confirmed", "revoked"]
IdentityAuthorityDenialReason = Literal["revoked", "superseded", "target_ineligible"]

_PURPOSE = "observation_processing"
_PROVIDERS = frozenset({"email", "wecom", "whatsapp", "phone", "manual_import"})
_STATUSES = frozenset(
    {
        "queued",
        "leased",
        "retry_wait",
        "unresolved",
        "confirmed",
        "revoked",
        "conflict",
        "dead_letter",
    }
)
_OUTCOMES = frozenset({"unresolved", "confirmed", "revoked", "conflict"})
_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "invalid_resolver_response",
        "permission_denied",
        "resolver_timeout",
        "resolver_unavailable",
        "team_mismatch",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_WORK_ID = re.compile(r"^IRW-[0-9a-f]{64}$")
_DENIAL_NOTICE_ID = re.compile(r"^IAD-[0-9a-f]{64}$")
_MAPPING_REF = re.compile(r"^EID-[0-9A-HJKMNP-TV-Z]{26}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{7,255}$")
_SUBJECT_TAIL = re.compile(r"^[A-Za-z0-9_-]{43}$")
_WORK_COLUMNS = ", ".join(
    (
        "site_id",
        "work_id",
        "identity_provider",
        "identity_ref",
        "team_ref",
        "status",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "lease_generation",
        "last_error_code",
        "last_resolution_status",
        "last_resolution_success_at",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
)
_DENIAL_COLUMNS = ", ".join(
    (
        "site_id",
        "notice_id",
        "identity_provider",
        "identity_ref",
        "mapping_ref",
        "team_ref",
        "deny_through_revision",
        "reason",
        "denied_at",
    )
)


class IdentityResolutionLeaseConflict(RuntimeError):
    """A stale worker no longer owns the current fenced lease."""


class IdentityAuthorityDenialConflict(ValueError):
    """An authority-denial idempotency key was reused for different authority."""


@dataclass(frozen=True, slots=True, repr=False)
class IdentityAuthorityDenial:
    site_id: str
    notice_id: str
    identity_provider: str
    identity_ref: str
    mapping_ref: str
    team_ref: str
    deny_through_revision: int
    reason: IdentityAuthorityDenialReason
    denied_at: datetime

    def __post_init__(self) -> None:
        _validate_scope(TenantScope(self.site_id, _PURPOSE))
        _safe_identifier(self.notice_id, "denial notice", pattern=_DENIAL_NOTICE_ID)
        _validate_identity(self.identity_provider, self.identity_ref)
        _safe_identifier(self.mapping_ref, "mapping", pattern=_MAPPING_REF)
        _safe_identifier(self.team_ref, "team")
        _bounded_integer(
            self.deny_through_revision,
            "denial revision",
            minimum=1,
            maximum=2_147_483_647,
        )
        if self.reason not in {"revoked", "superseded", "target_ineligible"}:
            raise ValueError("invalid identity authority denial reason")
        _require_aware(self.denied_at, "denied_at")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self.site_id!r}, "
            f"notice_id={self.notice_id!r}, identity_provider={self.identity_provider!r}, "
            "identity_ref=<redacted>, "
            f"mapping_ref={self.mapping_ref!r}, team_ref={self.team_ref!r}, "
            f"deny_through_revision={self.deny_through_revision}, "
            f"reason={self.reason!r}, denied_at={self.denied_at!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class IdentityResolutionWorkItem:
    site_id: str
    work_id: str
    identity_provider: str
    identity_ref: str
    team_ref: str
    status: WorkStatus
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    last_error_code: str | None
    last_resolution_status: LastResolutionStatus | None
    last_resolution_success_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_scope(TenantScope(self.site_id, _PURPOSE))
        _validate_identity(self.identity_provider, self.identity_ref)
        _safe_identifier(self.work_id, "work id", pattern=_WORK_ID)
        _safe_identifier(self.team_ref, "team")
        if self.status not in _STATUSES:
            raise ValueError("invalid identity work status")
        _bounded_integer(self.attempt_count, "attempt count", minimum=0, maximum=100)
        _bounded_integer(self.max_attempts, "max attempts", minimum=1, maximum=100)
        _bounded_integer(self.lease_generation, "lease generation", minimum=0)
        if self.attempt_count > self.max_attempts:
            raise ValueError("invalid identity work attempts")
        for name in (
            "next_attempt_at",
            "first_seen_at",
            "last_seen_at",
            "created_at",
            "updated_at",
        ):
            _require_aware(getattr(self, name), name)
        if self.lease_expires_at is not None:
            _require_aware(self.lease_expires_at, "lease_expires_at")
        if self.status == "leased":
            if self.lease_owner is None or self.lease_expires_at is None:
                raise ValueError("invalid leased identity work")
            _safe_identifier(self.lease_owner, "lease owner")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise ValueError("invalid unleased identity work")
        if self.last_error_code is not None and self.last_error_code not in _ERROR_CODES:
            raise ValueError("invalid identity resolution error code")
        if (self.last_resolution_status is None) != (self.last_resolution_success_at is None):
            raise ValueError("invalid identity work last resolution pair")
        if self.last_resolution_status is not None:
            if self.last_resolution_status not in {"unresolved", "confirmed", "revoked"}:
                raise ValueError("invalid identity work last resolution status")
            if self.last_resolution_success_at is None:
                raise ValueError("invalid identity work last resolution pair")
            _require_aware(
                self.last_resolution_success_at,
                "last_resolution_success_at",
            )
            if self.last_resolution_success_at > self.updated_at:
                raise ValueError("invalid identity work last resolution timestamp")
        if not self.first_seen_at <= self.last_seen_at:
            raise ValueError("invalid identity work observation timestamps")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self.site_id!r}, work_id={self.work_id!r}, "
            f"identity_provider={self.identity_provider!r}, identity_ref=<redacted>, "
            f"team_ref={self.team_ref!r}, status={self.status!r}, "
            f"attempt_count={self.attempt_count}, max_attempts={self.max_attempts}, "
            f"next_attempt_at={self.next_attempt_at!r}, lease_owner={self.lease_owner!r}, "
            f"lease_expires_at={self.lease_expires_at!r}, "
            f"lease_generation={self.lease_generation}, "
            f"last_error_code={self.last_error_code!r}, "
            f"last_resolution_status={self.last_resolution_status!r}, "
            f"last_resolution_success_at={self.last_resolution_success_at!r}, "
            f"first_seen_at={self.first_seen_at!r}, last_seen_at={self.last_seen_at!r}, "
            f"created_at={self.created_at!r}, updated_at={self.updated_at!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class IdentityResolutionWorkClaim:
    item: IdentityResolutionWorkItem
    fence_token: str

    def __post_init__(self) -> None:
        if self.item.status != "leased" or not self.fence_token.startswith("v1:"):
            raise ValueError("invalid identity resolution work claim")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.item, name)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(work_id={self.item.work_id!r}, "
            f"status={self.item.status!r}, attempt_count={self.item.attempt_count}, "
            f"lease_generation={self.item.lease_generation}, fence_token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class IdentityResolutionWorkSnapshot:
    ready: bool
    worker_last_heartbeat_at: datetime | None
    backlog_count: int
    oldest_backlog_age_seconds: int | None
    unresolved_count: int
    conflict_count: int
    request_outcomes: Mapping[str, int]
    latency_buckets: Mapping[str, int]


class IdentityResolutionWorkRepository(Protocol):
    def record_authority_denial(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        mapping_ref: str,
        team_ref: str,
        deny_through_revision: int,
        reason: IdentityAuthorityDenialReason,
        denied_at: datetime,
        idempotency_key: str,
    ) -> IdentityAuthorityDenial: ...

    def is_denied(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        mapping_ref: str,
        *,
        mapping_revision: int,
    ) -> bool: ...

    def enqueue(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> IdentityResolutionWorkItem: ...

    def get(self, scope: TenantScope, work_id: str) -> IdentityResolutionWorkItem | None: ...

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkClaim | None: ...

    def heartbeat(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkItem: ...

    def record_outcome(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        outcome: ResolutionOutcome,
        latency: timedelta,
        recheck_at: datetime | None = None,
    ) -> IdentityResolutionWorkItem: ...

    def mark_failed(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> IdentityResolutionWorkItem: ...

    def record_worker_heartbeat(self, scope: TenantScope, *, now: datetime) -> None: ...

    def snapshot(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        readiness_window: timedelta,
    ) -> IdentityResolutionWorkSnapshot: ...


class InMemoryIdentityResolutionWorkRepository:
    __slots__ = ("_denials", "_items", "_keys", "_metrics")

    def __init__(self) -> None:
        self._denials: dict[tuple[str, str], IdentityAuthorityDenial] = {}
        self._items: dict[tuple[str, str], IdentityResolutionWorkItem] = {}
        self._keys: dict[tuple[str, str, str, str], str] = {}
        self._metrics: dict[str, dict[str, int | datetime | None]] = {}

    def __repr__(self) -> str:
        return "InMemoryIdentityResolutionWorkRepository(items=<redacted>)"

    def record_authority_denial(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        mapping_ref: str,
        team_ref: str,
        deny_through_revision: int,
        reason: IdentityAuthorityDenialReason,
        denied_at: datetime,
        idempotency_key: str,
    ) -> IdentityAuthorityDenial:
        denial = _authority_denial(
            scope,
            identity_provider=identity_provider,
            identity_ref=identity_ref,
            mapping_ref=mapping_ref,
            team_ref=team_ref,
            deny_through_revision=deny_through_revision,
            reason=reason,
            denied_at=denied_at,
            idempotency_key=idempotency_key,
        )
        key = (scope.site_id, denial.notice_id)
        existing = self._denials.get(key)
        if existing is not None:
            if _denial_authority_values(existing) == _denial_authority_values(denial):
                return existing
            raise IdentityAuthorityDenialConflict("identity authority denial conflict")
        self._denials[key] = denial
        return denial

    def is_denied(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        mapping_ref: str,
        *,
        mapping_revision: int,
    ) -> bool:
        _validate_denial_lookup(
            scope,
            identity_provider,
            identity_ref,
            team_ref,
            mapping_ref,
            mapping_revision,
        )
        return any(
            denial.site_id == scope.site_id
            and denial.identity_provider == identity_provider
            and denial.identity_ref == identity_ref
            and denial.team_ref == team_ref
            and denial.mapping_ref == mapping_ref
            and denial.deny_through_revision >= mapping_revision
            for denial in self._denials.values()
        )

    def enqueue(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> IdentityResolutionWorkItem:
        _validate_enqueue(scope, identity_provider, identity_ref, team_ref, now, max_attempts)
        key = (scope.site_id, identity_provider, identity_ref, team_ref)
        existing_id = self._keys.get(key)
        if existing_id is not None:
            existing = self._items[(scope.site_id, existing_id)]
            seen = replace(
                existing,
                last_seen_at=max(existing.last_seen_at, now),
                updated_at=max(existing.updated_at, now),
            )
            self._items[(scope.site_id, existing_id)] = seen
            return seen
        work_id = _work_id(scope.site_id, identity_provider, identity_ref, team_ref)
        item = IdentityResolutionWorkItem(
            site_id=scope.site_id,
            work_id=work_id,
            identity_provider=identity_provider,
            identity_ref=identity_ref,
            team_ref=team_ref,
            status="queued",
            attempt_count=0,
            max_attempts=max_attempts,
            next_attempt_at=now,
            lease_owner=None,
            lease_expires_at=None,
            lease_generation=0,
            last_error_code=None,
            last_resolution_status=None,
            last_resolution_success_at=None,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        self._keys[key] = work_id
        self._items[(scope.site_id, work_id)] = item
        return item

    def get(self, scope: TenantScope, work_id: str) -> IdentityResolutionWorkItem | None:
        _validate_scope(scope)
        _safe_identifier(work_id, "work id", pattern=_WORK_ID)
        return self._items.get((scope.site_id, work_id))

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkClaim | None:
        _validate_claim(scope, worker_id, now, lease_duration)
        for key, item in tuple(self._items.items()):
            if (
                key[0] == scope.site_id
                and item.status == "leased"
                and item.lease_expires_at is not None
                and item.lease_expires_at <= now
                and item.attempt_count >= item.max_attempts
            ):
                self._items[key] = replace(
                    item,
                    status="dead_letter",
                    lease_owner=None,
                    lease_expires_at=None,
                    last_error_code="resolver_timeout",
                    updated_at=now,
                )
        candidates = [
            item
            for (site_id, _), item in self._items.items()
            if site_id == scope.site_id and _is_claimable(item, now)
        ]
        if not candidates:
            return None
        current = min(
            candidates, key=lambda item: (item.next_attempt_at, item.first_seen_at, item.work_id)
        )
        leased = replace(
            current,
            status="leased",
            attempt_count=current.attempt_count + 1,
            lease_owner=worker_id,
            lease_expires_at=now + lease_duration,
            lease_generation=current.lease_generation + 1,
            last_error_code=None,
            updated_at=now,
        )
        self._items[(scope.site_id, current.work_id)] = leased
        return _claim(leased)

    def heartbeat(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkItem:
        current = self._leased(scope, work_id, worker_id, fence_token, now)
        _duration(lease_duration, "lease duration", maximum=timedelta(hours=1))
        updated = replace(
            current,
            lease_expires_at=now + lease_duration,
            updated_at=now,
        )
        self._items[(scope.site_id, work_id)] = updated
        return updated

    def record_outcome(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        outcome: ResolutionOutcome,
        latency: timedelta,
        recheck_at: datetime | None = None,
    ) -> IdentityResolutionWorkItem:
        current = self._leased(scope, work_id, worker_id, fence_token, now)
        _validate_outcome(outcome, now, latency, recheck_at)
        terminal = outcome == "conflict"
        next_attempt_at = now if terminal else _required_recheck(recheck_at)
        updated = replace(
            current,
            status=outcome,
            attempt_count=0 if not terminal else current.attempt_count,
            next_attempt_at=next_attempt_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=None,
            last_resolution_status=(
                current.last_resolution_status if terminal else _successful_outcome(outcome)
            ),
            last_resolution_success_at=(current.last_resolution_success_at if terminal else now),
            updated_at=now,
        )
        self._items[(scope.site_id, work_id)] = updated
        self._increment_outcome(scope.site_id, outcome, latency)
        return updated

    def mark_failed(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> IdentityResolutionWorkItem:
        current = self._leased(scope, work_id, worker_id, fence_token, now)
        _validate_failure(now, retry_at, error_code)
        final = current.attempt_count >= current.max_attempts
        updated = replace(
            current,
            status="dead_letter" if final else "retry_wait",
            next_attempt_at=retry_at,
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=error_code,
            updated_at=now,
        )
        self._items[(scope.site_id, work_id)] = updated
        metrics = self._site_metrics(scope.site_id)
        metrics["request_error_count"] = _metric_count(metrics, "request_error_count") + 1
        return updated

    def record_worker_heartbeat(self, scope: TenantScope, *, now: datetime) -> None:
        _validate_scope(scope)
        _require_aware(now, "now")
        self._site_metrics(scope.site_id)["worker_last_heartbeat_at"] = now

    def snapshot(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        readiness_window: timedelta,
    ) -> IdentityResolutionWorkSnapshot:
        _validate_scope(scope)
        _require_aware(now, "now")
        _duration(readiness_window, "readiness window", maximum=timedelta(days=1))
        items = [item for (site_id, _), item in self._items.items() if site_id == scope.site_id]
        backlog = [item for item in items if item.status in {"queued", "leased", "retry_wait"}]
        metrics = self._site_metrics(scope.site_id)
        heartbeat_at = metrics["worker_last_heartbeat_at"]
        ready = isinstance(heartbeat_at, datetime) and now - readiness_window <= heartbeat_at <= now
        oldest = min((item.first_seen_at for item in backlog), default=None)
        return _snapshot(
            ready=ready,
            heartbeat_at=heartbeat_at if isinstance(heartbeat_at, datetime) else None,
            backlog_count=len(backlog),
            oldest_age=None if oldest is None else max(0, int((now - oldest).total_seconds())),
            unresolved_count=sum(item.status == "unresolved" for item in items),
            conflict_count=sum(item.status == "conflict" for item in items),
            metrics=metrics,
        )

    def _leased(
        self,
        scope: TenantScope,
        work_id: str,
        worker_id: str,
        fence_token: str,
        now: datetime,
    ) -> IdentityResolutionWorkItem:
        _validate_transition(scope, work_id, worker_id, fence_token, now)
        current = self._items.get((scope.site_id, work_id))
        if (
            current is None
            or current.status != "leased"
            or current.lease_owner != worker_id
            or current.lease_expires_at is None
            or current.lease_expires_at <= now
            or not hmac.compare_digest(_fence_token(current, worker_id), fence_token)
        ):
            raise _lease_conflict()
        return current

    def _site_metrics(self, site_id: str) -> dict[str, int | datetime | None]:
        return self._metrics.setdefault(
            site_id,
            {
                "worker_last_heartbeat_at": None,
                "request_confirmed_count": 0,
                "request_unresolved_count": 0,
                "request_revoked_count": 0,
                "request_conflict_count": 0,
                "request_error_count": 0,
                "latency_le_100_ms_count": 0,
                "latency_le_500_ms_count": 0,
                "latency_le_2000_ms_count": 0,
                "latency_gt_2000_ms_count": 0,
            },
        )

    def _increment_outcome(
        self,
        site_id: str,
        outcome: ResolutionOutcome,
        latency: timedelta,
    ) -> None:
        metrics = self._site_metrics(site_id)
        outcome_key = f"request_{outcome}_count"
        metrics[outcome_key] = _metric_count(metrics, outcome_key) + 1
        bucket = _latency_bucket(latency)
        metrics[bucket] = _metric_count(metrics, bucket) + 1


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresIdentityResolutionWorkRepository:
    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresIdentityResolutionWorkRepository(connection=<redacted>)"

    def record_authority_denial(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        mapping_ref: str,
        team_ref: str,
        deny_through_revision: int,
        reason: IdentityAuthorityDenialReason,
        denied_at: datetime,
        idempotency_key: str,
    ) -> IdentityAuthorityDenial:
        denial = _authority_denial(
            scope,
            identity_provider=identity_provider,
            identity_ref=identity_ref,
            mapping_ref=mapping_ref,
            team_ref=team_ref,
            deny_through_revision=deny_through_revision,
            reason=reason,
            denied_at=denied_at,
            idempotency_key=idempotency_key,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                f"""
                INSERT INTO observer.identity_authority_denials (
                    site_id, notice_id, identity_provider, identity_ref,
                    mapping_ref, team_ref, deny_through_revision, reason, denied_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (site_id, notice_id) DO NOTHING
                RETURNING {_DENIAL_COLUMNS}
                """,
                _denial_values(denial),
            )
            row = cursor.fetchone()
            if row is not None:
                return _denial_from_row(row)
            cursor.execute(
                f"""
                SELECT {_DENIAL_COLUMNS}
                FROM observer.identity_authority_denials
                WHERE site_id = %s AND notice_id = %s
                """,
                (scope.site_id, denial.notice_id),
            )
            existing_row = cursor.fetchone()
            if existing_row is None:
                raise IdentityAuthorityDenialConflict("identity authority denial write rejected")
            existing = _denial_from_row(existing_row)
            if _denial_authority_values(existing) != _denial_authority_values(denial):
                raise IdentityAuthorityDenialConflict("identity authority denial conflict")
            return existing

    def is_denied(
        self,
        scope: TenantScope,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        mapping_ref: str,
        *,
        mapping_revision: int,
    ) -> bool:
        _validate_denial_lookup(
            scope,
            identity_provider,
            identity_ref,
            team_ref,
            mapping_ref,
            mapping_revision,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM observer.identity_authority_denials AS denial
                    WHERE denial.site_id = %s
                      AND denial.identity_provider = %s
                      AND denial.identity_ref = %s
                      AND denial.team_ref = %s
                      AND denial.mapping_ref = %s
                      AND denial.deny_through_revision >= %s
                )
                """,
                (
                    scope.site_id,
                    identity_provider,
                    identity_ref,
                    team_ref,
                    mapping_ref,
                    mapping_revision,
                ),
            )
            row = cursor.fetchone()
            if row is None or len(row) != 1 or not isinstance(row[0], bool):
                raise RuntimeError("identity authority denial lookup returned an invalid row")
            return row[0]

    def enqueue(
        self,
        scope: TenantScope,
        *,
        identity_provider: str,
        identity_ref: str,
        team_ref: str,
        now: datetime,
        max_attempts: int = 5,
    ) -> IdentityResolutionWorkItem:
        _validate_enqueue(scope, identity_provider, identity_ref, team_ref, now, max_attempts)
        work_id = _work_id(scope.site_id, identity_provider, identity_ref, team_ref)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                f"""
                INSERT INTO observer.identity_resolution_work AS work (
                    site_id, work_id, identity_provider, identity_ref, team_ref,
                    status, attempt_count, max_attempts, next_attempt_at,
                    lease_generation, first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, 'queued', 0, %s, %s, 0, %s, %s, %s, %s
                )
                ON CONFLICT (site_id, identity_provider, identity_ref, team_ref)
                DO UPDATE SET
                    last_seen_at = GREATEST(work.last_seen_at, EXCLUDED.last_seen_at),
                    updated_at = GREATEST(work.updated_at, EXCLUDED.updated_at)
                RETURNING {_WORK_COLUMNS}
                """,
                (
                    scope.site_id,
                    work_id,
                    identity_provider,
                    identity_ref,
                    team_ref,
                    max_attempts,
                    now,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            return _required_item(cursor.fetchone())

    def get(self, scope: TenantScope, work_id: str) -> IdentityResolutionWorkItem | None:
        _validate_scope(scope)
        _safe_identifier(work_id, "work id", pattern=_WORK_ID)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_WORK_COLUMNS}
                FROM observer.identity_resolution_work
                WHERE site_id = %s AND work_id = %s
                """,
                (scope.site_id, work_id),
            )
            row = cursor.fetchone()
            return None if row is None else _item_from_row(row)

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkClaim | None:
        _validate_claim(scope, worker_id, now, lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                f"""
                WITH exhausted AS (
                    UPDATE observer.identity_resolution_work AS expired
                    SET status = 'dead_letter',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'resolver_timeout',
                        updated_at = %s
                    WHERE expired.site_id = %s
                      AND expired.status = 'leased'
                      AND expired.lease_expires_at <= %s
                      AND expired.attempt_count >= expired.max_attempts
                    RETURNING expired.site_id
                ), candidate AS (
                    SELECT site_id, work_id
                    FROM observer.identity_resolution_work
                    WHERE site_id = %s
                      AND attempt_count < max_attempts
                      AND (
                        (
                          status IN ('queued', 'retry_wait', 'unresolved', 'confirmed', 'revoked')
                          AND next_attempt_at <= %s
                        )
                        OR (status = 'leased' AND lease_expires_at <= %s)
                      )
                    ORDER BY next_attempt_at, first_seen_at, work_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE observer.identity_resolution_work AS work
                SET status = 'leased',
                    attempt_count = work.attempt_count + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    lease_generation = work.lease_generation + 1,
                    last_error_code = NULL,
                    updated_at = %s
                FROM candidate
                WHERE work.site_id = candidate.site_id
                  AND work.work_id = candidate.work_id
                RETURNING work.{_WORK_COLUMNS.replace(", ", ", work.")}
                """,
                (
                    now,
                    scope.site_id,
                    now,
                    scope.site_id,
                    now,
                    now,
                    worker_id,
                    now + lease_duration,
                    now,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else _claim(_item_from_row(row))

    def heartbeat(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityResolutionWorkItem:
        attempt, generation = _validate_transition(scope, work_id, worker_id, fence_token, now)
        _duration(lease_duration, "lease duration", maximum=timedelta(hours=1))
        return self._leased_update(
            scope,
            work_id,
            worker_id=worker_id,
            expected_attempt=attempt,
            generation=generation,
            now=now,
            assignments="lease_expires_at = %s, updated_at = %s",
            assignment_params=(now + lease_duration, now),
        )

    def record_outcome(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        outcome: ResolutionOutcome,
        latency: timedelta,
        recheck_at: datetime | None = None,
    ) -> IdentityResolutionWorkItem:
        attempt, generation = _validate_transition(scope, work_id, worker_id, fence_token, now)
        _validate_outcome(outcome, now, latency, recheck_at)
        terminal = outcome == "conflict"
        next_attempt_at = now if terminal else _required_recheck(recheck_at)
        outcome_column = f"request_{outcome}_count"
        latency_column = _latency_bucket(latency)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            item = _execute_leased_update(
                cursor,
                scope,
                work_id,
                worker_id=worker_id,
                expected_attempt=attempt,
                generation=generation,
                now=now,
                assignments=(
                    "status = %s, attempt_count = CASE WHEN %s "
                    "THEN attempt_count ELSE 0 END, next_attempt_at = %s, "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "last_error_code = NULL, "
                    "last_resolution_status = CASE WHEN %s = 'conflict' "
                    "THEN last_resolution_status ELSE %s END, "
                    "last_resolution_success_at = CASE WHEN %s = 'conflict' "
                    "THEN last_resolution_success_at ELSE %s END, updated_at = %s"
                ),
                assignment_params=(
                    outcome,
                    terminal,
                    next_attempt_at,
                    outcome,
                    outcome,
                    outcome,
                    now,
                    now,
                ),
            )
            cursor.execute(
                f"""
                INSERT INTO observer.identity_resolution_worker_metrics (
                    site_id, {outcome_column}, {latency_column}, updated_at
                ) VALUES (%s, 1, 1, %s)
                ON CONFLICT (site_id) DO UPDATE SET
                    {outcome_column} =
                        observer.identity_resolution_worker_metrics.{outcome_column} + 1,
                    {latency_column} =
                        observer.identity_resolution_worker_metrics.{latency_column} + 1,
                    updated_at = EXCLUDED.updated_at
                """,
                (scope.site_id, now),
            )
            return item

    def mark_failed(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> IdentityResolutionWorkItem:
        attempt, generation = _validate_transition(scope, work_id, worker_id, fence_token, now)
        _validate_failure(now, retry_at, error_code)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            item = _execute_leased_update(
                cursor,
                scope,
                work_id,
                worker_id=worker_id,
                expected_attempt=attempt,
                generation=generation,
                now=now,
                assignments=(
                    "status = CASE WHEN attempt_count >= max_attempts "
                    "THEN 'dead_letter' ELSE 'retry_wait' END, "
                    "next_attempt_at = %s, lease_owner = NULL, lease_expires_at = NULL, "
                    "last_error_code = %s, updated_at = %s"
                ),
                assignment_params=(retry_at, error_code, now),
            )
            cursor.execute(
                """
                INSERT INTO observer.identity_resolution_worker_metrics (
                    site_id, request_error_count, updated_at
                ) VALUES (%s, 1, %s)
                ON CONFLICT (site_id) DO UPDATE SET
                    request_error_count =
                        observer.identity_resolution_worker_metrics.request_error_count + 1,
                    updated_at = EXCLUDED.updated_at
                """,
                (scope.site_id, now),
            )
            return item

    def record_worker_heartbeat(self, scope: TenantScope, *, now: datetime) -> None:
        _validate_scope(scope)
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                """
                INSERT INTO observer.identity_resolution_worker_metrics (
                    site_id, worker_last_heartbeat_at, updated_at
                ) VALUES (%s, %s, %s)
                ON CONFLICT (site_id) DO UPDATE SET
                    worker_last_heartbeat_at = EXCLUDED.worker_last_heartbeat_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (scope.site_id, now, now),
            )

    def snapshot(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        readiness_window: timedelta,
    ) -> IdentityResolutionWorkSnapshot:
        _validate_scope(scope)
        _require_aware(now, "now")
        _duration(readiness_window, "readiness window", maximum=timedelta(days=1))
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                """
                WITH queue AS (
                    SELECT
                        count(*) FILTER (
                            WHERE status IN ('queued', 'leased', 'retry_wait')
                        )::bigint AS backlog_count,
                        min(first_seen_at) FILTER (
                            WHERE status IN ('queued', 'leased', 'retry_wait')
                        ) AS oldest_backlog_at,
                        count(*) FILTER (WHERE status = 'unresolved')::bigint
                            AS unresolved_count,
                        count(*) FILTER (WHERE status = 'conflict')::bigint
                            AS conflict_count
                    FROM observer.identity_resolution_work
                    WHERE site_id = %s
                )
                SELECT
                    metrics.worker_last_heartbeat_at,
                    queue.backlog_count,
                    queue.oldest_backlog_at,
                    queue.unresolved_count,
                    queue.conflict_count,
                    COALESCE(metrics.request_confirmed_count, 0),
                    COALESCE(metrics.request_unresolved_count, 0),
                    COALESCE(metrics.request_revoked_count, 0),
                    COALESCE(metrics.request_conflict_count, 0),
                    COALESCE(metrics.request_error_count, 0),
                    COALESCE(metrics.latency_le_100_ms_count, 0),
                    COALESCE(metrics.latency_le_500_ms_count, 0),
                    COALESCE(metrics.latency_le_2000_ms_count, 0),
                    COALESCE(metrics.latency_gt_2000_ms_count, 0)
                FROM queue
                LEFT JOIN observer.identity_resolution_worker_metrics AS metrics
                  ON metrics.site_id = %s
                """,
                (scope.site_id, scope.site_id),
            )
            row = cursor.fetchone()
            if row is None or len(row) != 14:
                raise RuntimeError("identity resolution metrics returned an invalid row")
            heartbeat_at = row[0]
            oldest_at = row[2]
            ready = (
                isinstance(heartbeat_at, datetime) and now - readiness_window <= heartbeat_at <= now
            )
            return _snapshot(
                ready=ready,
                heartbeat_at=heartbeat_at if isinstance(heartbeat_at, datetime) else None,
                backlog_count=int(row[1]),
                oldest_age=(
                    None if oldest_at is None else max(0, int((now - oldest_at).total_seconds()))
                ),
                unresolved_count=int(row[3]),
                conflict_count=int(row[4]),
                metrics={
                    "request_confirmed_count": int(row[5]),
                    "request_unresolved_count": int(row[6]),
                    "request_revoked_count": int(row[7]),
                    "request_conflict_count": int(row[8]),
                    "request_error_count": int(row[9]),
                    "latency_le_100_ms_count": int(row[10]),
                    "latency_le_500_ms_count": int(row[11]),
                    "latency_le_2000_ms_count": int(row[12]),
                    "latency_gt_2000_ms_count": int(row[13]),
                },
            )

    def _leased_update(
        self,
        scope: TenantScope,
        work_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        generation: int,
        now: datetime,
        assignments: str,
        assignment_params: tuple[Any, ...],
    ) -> IdentityResolutionWorkItem:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            return _execute_leased_update(
                cursor,
                scope,
                work_id,
                worker_id=worker_id,
                expected_attempt=expected_attempt,
                generation=generation,
                now=now,
                assignments=assignments,
                assignment_params=assignment_params,
            )


def _execute_leased_update(
    cursor: Cursor,
    scope: TenantScope,
    work_id: str,
    *,
    worker_id: str,
    expected_attempt: int,
    generation: int,
    now: datetime,
    assignments: str,
    assignment_params: tuple[Any, ...],
) -> IdentityResolutionWorkItem:
    cursor.execute(
        f"""
        UPDATE observer.identity_resolution_work AS work
        SET {assignments}
        WHERE work.site_id = %s
          AND work.work_id = %s
          AND work.status = 'leased'
          AND work.lease_owner = %s
          AND work.attempt_count = %s
          AND work.lease_generation = %s
          AND work.lease_expires_at > %s
        RETURNING work.{_WORK_COLUMNS.replace(", ", ", work.")}
        """,
        (
            *assignment_params,
            scope.site_id,
            work_id,
            worker_id,
            expected_attempt,
            generation,
            now,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise _lease_conflict()
    return _item_from_row(row)


def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
    cursor.execute(
        "SELECT set_config('app.processing_purpose', %s, true)",
        (scope.processing_purpose,),
    )


def _required_item(row: tuple[Any, ...] | None) -> IdentityResolutionWorkItem:
    if row is None:
        raise RuntimeError("identity resolution work write returned no row")
    return _item_from_row(row)


def _item_from_row(row: tuple[Any, ...]) -> IdentityResolutionWorkItem:
    if len(row) != 19:
        raise RuntimeError("identity resolution work returned an invalid row")
    return IdentityResolutionWorkItem(
        site_id=str(row[0]),
        work_id=str(row[1]),
        identity_provider=str(row[2]),
        identity_ref=str(row[3]),
        team_ref=str(row[4]),
        status=str(row[5]),  # type: ignore[arg-type]
        attempt_count=int(row[6]),
        max_attempts=int(row[7]),
        next_attempt_at=row[8],
        lease_owner=None if row[9] is None else str(row[9]),
        lease_expires_at=row[10],
        lease_generation=int(row[11]),
        last_error_code=None if row[12] is None else str(row[12]),
        last_resolution_status=(None if row[13] is None else str(row[13])),  # type: ignore[arg-type]
        last_resolution_success_at=row[14],
        first_seen_at=row[15],
        last_seen_at=row[16],
        created_at=row[17],
        updated_at=row[18],
    )


def _claim(item: IdentityResolutionWorkItem) -> IdentityResolutionWorkClaim:
    if item.lease_owner is None:
        raise RuntimeError("identity resolution work returned an invalid lease")
    return IdentityResolutionWorkClaim(item=item, fence_token=_fence_token(item, item.lease_owner))


def _fence_token(item: IdentityResolutionWorkItem, worker_id: str) -> str:
    return _fence_value(
        site_id=item.site_id,
        work_id=item.work_id,
        worker_id=worker_id,
        attempt=item.attempt_count,
        generation=item.lease_generation,
    )


def _fence_value(
    *,
    site_id: str,
    work_id: str,
    worker_id: str,
    attempt: int,
    generation: int,
) -> str:
    digest = hashlib.sha256(
        f"identity-resolution-work-fence-v1\x1f{site_id}\x1f{work_id}"
        f"\x1f{worker_id}\x1f{attempt}\x1f{generation}".encode()
    ).hexdigest()
    return f"v1:{attempt}:{generation}:{digest}"


def _work_id(site_id: str, provider: str, identity_ref: str, team_ref: str) -> str:
    digest = hashlib.sha256(
        f"identity-resolution-work-v1\x1f{site_id}\x1f{provider}\x1f{identity_ref}"
        f"\x1f{team_ref}".encode()
    ).hexdigest()
    return f"IRW-{digest}"


def _authority_denial(
    scope: TenantScope,
    *,
    identity_provider: str,
    identity_ref: str,
    mapping_ref: str,
    team_ref: str,
    deny_through_revision: int,
    reason: IdentityAuthorityDenialReason,
    denied_at: datetime,
    idempotency_key: str,
) -> IdentityAuthorityDenial:
    _validate_scope(scope)
    if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("invalid identity authority denial idempotency key")
    digest = hashlib.sha256(
        f"identity-authority-denial-v1\x1f{scope.site_id}\x1f{idempotency_key}".encode()
    ).hexdigest()
    return IdentityAuthorityDenial(
        site_id=scope.site_id,
        notice_id=f"IAD-{digest}",
        identity_provider=identity_provider,
        identity_ref=identity_ref,
        mapping_ref=mapping_ref,
        team_ref=team_ref,
        deny_through_revision=deny_through_revision,
        reason=reason,
        denied_at=denied_at,
    )


def _validate_denial_lookup(
    scope: TenantScope,
    identity_provider: str,
    identity_ref: str,
    team_ref: str,
    mapping_ref: str,
    mapping_revision: int,
) -> None:
    _validate_scope(scope)
    _validate_identity(identity_provider, identity_ref)
    _safe_identifier(team_ref, "team")
    _safe_identifier(mapping_ref, "mapping", pattern=_MAPPING_REF)
    _bounded_integer(
        mapping_revision,
        "mapping revision",
        minimum=1,
        maximum=2_147_483_647,
    )


def _denial_values(denial: IdentityAuthorityDenial) -> tuple[Any, ...]:
    return (
        denial.site_id,
        denial.notice_id,
        denial.identity_provider,
        denial.identity_ref,
        denial.mapping_ref,
        denial.team_ref,
        denial.deny_through_revision,
        denial.reason,
        denial.denied_at,
    )


def _denial_authority_values(denial: IdentityAuthorityDenial) -> tuple[Any, ...]:
    return _denial_values(denial)[:-1]


def _denial_from_row(row: tuple[Any, ...]) -> IdentityAuthorityDenial:
    if len(row) != 9:
        raise RuntimeError("identity authority denial returned an invalid row")
    return IdentityAuthorityDenial(
        site_id=str(row[0]),
        notice_id=str(row[1]),
        identity_provider=str(row[2]),
        identity_ref=str(row[3]),
        mapping_ref=str(row[4]),
        team_ref=str(row[5]),
        deny_through_revision=int(row[6]),
        reason=str(row[7]),  # type: ignore[arg-type]
        denied_at=row[8],
    )


def _validate_enqueue(
    scope: TenantScope,
    provider: str,
    identity_ref: str,
    team_ref: str,
    now: datetime,
    max_attempts: int,
) -> None:
    _validate_scope(scope)
    _validate_identity(provider, identity_ref)
    _safe_identifier(team_ref, "team")
    _require_aware(now, "now")
    _bounded_integer(max_attempts, "max attempts", minimum=1, maximum=100)


def _validate_claim(
    scope: TenantScope,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> None:
    _validate_scope(scope)
    _safe_identifier(worker_id, "worker")
    _require_aware(now, "now")
    _duration(lease_duration, "lease duration", maximum=timedelta(hours=1))


def _validate_transition(
    scope: TenantScope,
    work_id: str,
    worker_id: str,
    fence_token: str,
    now: datetime,
) -> tuple[int, int]:
    _validate_scope(scope)
    _safe_identifier(work_id, "work id", pattern=_WORK_ID)
    _safe_identifier(worker_id, "worker")
    _require_aware(now, "now")
    try:
        prefix, attempt_text, generation_text, digest = fence_token.split(":")
        attempt = int(attempt_text)
        generation = int(generation_text)
    except AttributeError, TypeError, ValueError:
        raise _lease_conflict() from None
    if (
        prefix != "v1"
        or attempt < 1
        or generation < 1
        or len(digest) != 64
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise _lease_conflict()
    expected = _fence_value(
        site_id=scope.site_id,
        work_id=work_id,
        worker_id=worker_id,
        attempt=attempt,
        generation=generation,
    )
    if not hmac.compare_digest(expected, fence_token):
        raise _lease_conflict()
    return attempt, generation


def _validate_outcome(
    outcome: str,
    now: datetime,
    latency: timedelta,
    recheck_at: datetime | None,
) -> None:
    if outcome not in _OUTCOMES:
        raise ValueError("invalid identity resolution outcome")
    _duration(latency, "resolver latency", maximum=timedelta(hours=1), allow_zero=True)
    if outcome == "conflict":
        if recheck_at is not None:
            raise ValueError("conflicted identity work cannot schedule a recheck")
    else:
        if recheck_at is None:
            raise ValueError("identity resolution outcome requires a recheck")
        _require_aware(recheck_at, "recheck_at")
        if recheck_at <= now:
            raise ValueError("identity resolution recheck must be in the future")


def _validate_failure(now: datetime, retry_at: datetime, error_code: str) -> None:
    _require_aware(retry_at, "retry_at")
    if retry_at <= now:
        raise ValueError("identity resolution retry must be in the future")
    if error_code not in _ERROR_CODES:
        raise ValueError("invalid identity resolution error code")


def _validate_scope(scope: TenantScope) -> None:
    if scope.processing_purpose != _PURPOSE:
        raise ValueError("identity resolution work requires observation processing scope")


def _validate_identity(provider: object, identity_ref: object) -> None:
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise ValueError("invalid identity provider")
    prefix = f"extid:v1:{provider}:"
    tail = (
        identity_ref[len(prefix) :]
        if isinstance(identity_ref, str) and identity_ref.startswith(prefix)
        else ""
    )
    if (
        not isinstance(identity_ref, str)
        or len(identity_ref) > 160
        or not identity_ref.startswith(prefix)
        or _SUBJECT_TAIL.fullmatch(tail) is None
    ):
        raise ValueError("invalid opaque identity reference")


def _safe_identifier(value: object, name: str, *, pattern: re.Pattern[str] = _SAFE_ID) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"invalid identity resolution {name}")


def _bounded_integer(value: object, name: str, *, minimum: int, maximum: int = 2**63 - 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid identity resolution {name}")


def _duration(
    value: timedelta,
    name: str,
    *,
    maximum: timedelta,
    allow_zero: bool = False,
) -> None:
    if not isinstance(value, timedelta):
        raise ValueError(f"invalid identity resolution {name}")
    lower_valid = value >= timedelta(0) if allow_zero else value > timedelta(0)
    if not lower_valid or value > maximum:
        raise ValueError(f"invalid identity resolution {name}")


def _is_claimable(item: IdentityResolutionWorkItem, now: datetime) -> bool:
    if item.attempt_count >= item.max_attempts:
        return False
    if item.status == "leased":
        return item.lease_expires_at is not None and item.lease_expires_at <= now
    return item.status in {"queued", "retry_wait", "unresolved", "confirmed", "revoked"} and (
        item.next_attempt_at <= now
    )


def _required_recheck(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("identity resolution recheck was not validated")
    return value


def _successful_outcome(outcome: ResolutionOutcome) -> LastResolutionStatus:
    if outcome == "conflict":
        raise RuntimeError("conflict is not a successful identity resolution outcome")
    return outcome


def _latency_bucket(latency: timedelta) -> str:
    milliseconds = latency.total_seconds() * 1000
    if milliseconds <= 100:
        return "latency_le_100_ms_count"
    if milliseconds <= 500:
        return "latency_le_500_ms_count"
    if milliseconds <= 2000:
        return "latency_le_2000_ms_count"
    return "latency_gt_2000_ms_count"


def _snapshot(
    *,
    ready: bool,
    heartbeat_at: datetime | None,
    backlog_count: int,
    oldest_age: int | None,
    unresolved_count: int,
    conflict_count: int,
    metrics: Mapping[str, object],
) -> IdentityResolutionWorkSnapshot:
    outcomes = MappingProxyType(
        {
            "confirmed": _metric_count(metrics, "request_confirmed_count"),
            "conflict": _metric_count(metrics, "request_conflict_count"),
            "error": _metric_count(metrics, "request_error_count"),
            "revoked": _metric_count(metrics, "request_revoked_count"),
            "unresolved": _metric_count(metrics, "request_unresolved_count"),
        }
    )
    latency = MappingProxyType(
        {
            "le_100_ms": _metric_count(metrics, "latency_le_100_ms_count"),
            "le_500_ms": _metric_count(metrics, "latency_le_500_ms_count"),
            "le_2000_ms": _metric_count(metrics, "latency_le_2000_ms_count"),
            "gt_2000_ms": _metric_count(metrics, "latency_gt_2000_ms_count"),
        }
    )
    return IdentityResolutionWorkSnapshot(
        ready=ready,
        worker_last_heartbeat_at=heartbeat_at,
        backlog_count=backlog_count,
        oldest_backlog_age_seconds=oldest_age,
        unresolved_count=unresolved_count,
        conflict_count=conflict_count,
        request_outcomes=outcomes,
        latency_buckets=latency,
    )


def _metric_count(metrics: Mapping[str, object], key: str) -> int:
    value = metrics[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("identity resolution metrics returned an invalid counter")
    return value


def _lease_conflict() -> IdentityResolutionLeaseConflict:
    return IdentityResolutionLeaseConflict("identity resolution lease transition rejected")


__all__ = [
    "IdentityAuthorityDenial",
    "IdentityAuthorityDenialConflict",
    "IdentityResolutionLeaseConflict",
    "IdentityResolutionWorkClaim",
    "IdentityResolutionWorkItem",
    "IdentityResolutionWorkRepository",
    "IdentityResolutionWorkSnapshot",
    "InMemoryIdentityResolutionWorkRepository",
    "PostgresIdentityResolutionWorkRepository",
]
