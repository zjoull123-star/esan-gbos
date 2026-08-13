from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Protocol

from .models import (
    ContentProjection,
    IdempotencyConflict,
    TenantScope,
    ValidationError,
    canonical_digest,
    require_scope,
    stable_ref,
)

if TYPE_CHECKING:
    from .metrics import GatewayMetrics

TERMINAL_DRAFT_RETENTION = timedelta(days=30)
_LEASE_DURATION = timedelta(seconds=30)
_RETRY_DELAY = timedelta(seconds=60)
_MAX_ATTEMPTS = 5
_MAX_BATCH = 100


def terminal_draft_expires_at(terminal_at: datetime) -> datetime:
    _aware(terminal_at, "terminal_at")
    return terminal_at + TERMINAL_DRAFT_RETENTION


class RetentionPlanner:
    """Plan only Gateway content-ref expiry already authorized by Observer."""

    def plan(
        self,
        scope: TenantScope,
        projections: tuple[ContentProjection, ...],
        *,
        now: datetime,
        legal_hold_evidence_refs: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        _aware(now, "retention time")
        eligible: list[str] = []
        for item in projections:
            require_scope(scope, site_id=item.site_id)
            if (
                not item.confirmed
                and item.active_draft_ref is None
                and item.evidence_ref not in legal_hold_evidence_refs
                and item.observer_expiration_receipt_ref is not None
                and item.expires_at <= now
                and item.kind in {"unconfirmed_display", "unconfirmed_subject", "draft_projection"}
            ):
                eligible.append(item.projection_ref)
        return tuple(sorted(eligible))


class ObserverTombstoneVerifier(Protocol):
    """Read-only authority boundary for an Observer-owned deletion receipt."""

    def verify_tombstone(
        self,
        scope: TenantScope,
        projection: ContentProjection,
        *,
        now: datetime,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class RetentionRun:
    run_ref: str
    site_id: str
    idempotency_key: str
    payload_digest: str
    dry_run: bool
    status: str
    projections: tuple[ContentProjection, ...]
    planned_refs: tuple[str, ...]
    planned_count: int
    expired_count: int
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    next_attempt_at: datetime
    safe_error_code: str | None
    created_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetentionClaim:
    run_ref: str
    worker_id: str
    attempt: int
    generation: int
    fence_token: str
    lease_expires_at: datetime
    dry_run: bool
    projections: tuple[ContentProjection, ...]
    planned_refs: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "RetentionClaim("
            f"run_ref={self.run_ref!r}, attempt={self.attempt}, generation={self.generation}, "
            f"dry_run={self.dry_run}, planned_count={len(self.planned_refs)}, "
            "worker_id=<redacted>, fence_token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ContentExpirationReceipt:
    expiration_receipt_ref: str
    site_id: str
    run_ref: str
    projection_ref: str
    observer_expiration_receipt_ref: str
    evidence_ref: str
    payload_digest: str
    expired_at: datetime


class RetentionRunRepository(Protocol):
    def enqueue(self, scope: TenantScope, run: RetentionRun) -> RetentionRun: ...

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> RetentionClaim | None: ...

    def record_expiration(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        projection: ContentProjection,
        now: datetime,
    ) -> ContentExpirationReceipt: ...

    def complete(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        expired_count: int,
        now: datetime,
    ) -> RetentionRun: ...

    def fail(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        safe_error_code: str,
        now: datetime,
    ) -> RetentionRun: ...


class InMemoryRetentionRunRepository:
    """Durable-semantics reference repository used by offline runs and contract tests."""

    def __init__(self, *, fail_projection_ref: str | None = None) -> None:
        self._runs: dict[tuple[str, str], RetentionRun] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._receipts: dict[tuple[str, str], ContentExpirationReceipt] = {}
        self._fail_projection_ref = fail_projection_ref
        self._lock = RLock()

    def enqueue(self, scope: TenantScope, run: RetentionRun) -> RetentionRun:
        require_scope(scope, site_id=run.site_id)
        key = (scope.site_id, run.idempotency_key)
        with self._lock:
            replay = self._idempotency.get(key)
            if replay is not None:
                prior_digest, prior_ref = replay
                if prior_digest != run.payload_digest:
                    raise IdempotencyConflict("retention idempotency drift")
                return self._runs[(scope.site_id, prior_ref)]
            self._runs[(scope.site_id, run.run_ref)] = run
            self._idempotency[key] = (run.payload_digest, run.run_ref)
            return run

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> RetentionClaim | None:
        _safe_identifier(worker_id, "worker")
        _aware(now, "claim time")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_BATCH:
            raise ValidationError("invalid retention batch limit")
        with self._lock:
            candidates = sorted(
                (
                    run
                    for (site_id, _), run in self._runs.items()
                    if site_id == scope.site_id
                    and run.status in {"queued", "retry"}
                    and run.next_attempt_at <= now
                ),
                key=lambda item: (item.next_attempt_at, item.created_at, item.run_ref),
            )
            if not candidates:
                return None
            current = candidates[0]
            planned_refs = current.planned_refs[:limit]
            by_ref = {item.projection_ref: item for item in current.projections}
            projections = tuple(by_ref[item] for item in planned_refs)
            lease_expires_at = now + _LEASE_DURATION
            leased = replace(
                current,
                status="leased",
                planned_refs=planned_refs,
                planned_count=len(planned_refs),
                attempt=current.attempt + 1,
                lease_owner=worker_id,
                lease_expires_at=lease_expires_at,
                lease_generation=current.lease_generation + 1,
                safe_error_code=None,
            )
            self._runs[(scope.site_id, current.run_ref)] = leased
            return RetentionClaim(
                run_ref=leased.run_ref,
                worker_id=worker_id,
                attempt=leased.attempt,
                generation=leased.lease_generation,
                fence_token=_fence_token(leased, worker_id),
                lease_expires_at=lease_expires_at,
                dry_run=leased.dry_run,
                projections=projections,
                planned_refs=planned_refs,
            )

    def record_expiration(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        projection: ContentProjection,
        now: datetime,
    ) -> ContentExpirationReceipt:
        self._require_live_claim(scope, claim, now)
        require_scope(scope, site_id=projection.site_id)
        if projection.projection_ref not in claim.planned_refs:
            raise ValidationError("retention projection is outside claim")
        if projection.observer_expiration_receipt_ref is None:
            raise ValidationError("Observer tombstone receipt required")
        key = (scope.site_id, projection.projection_ref)
        with self._lock:
            replay = self._receipts.get(key)
            if replay is not None:
                return replay
            if projection.projection_ref == self._fail_projection_ref:
                raise RuntimeError("injected retention persistence failure")
            receipt = ContentExpirationReceipt(
                expiration_receipt_ref=stable_ref(
                    "EXP", scope.site_id, claim.run_ref, projection.projection_ref
                ),
                site_id=scope.site_id,
                run_ref=claim.run_ref,
                projection_ref=projection.projection_ref,
                observer_expiration_receipt_ref=projection.observer_expiration_receipt_ref,
                evidence_ref=projection.evidence_ref,
                payload_digest=canonical_digest(
                    {
                        "run_ref": claim.run_ref,
                        "projection_ref": projection.projection_ref,
                        "observer_expiration_receipt_ref": (
                            projection.observer_expiration_receipt_ref
                        ),
                    }
                ),
                expired_at=now,
            )
            self._receipts[key] = receipt
            return receipt

    def complete(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        expired_count: int,
        now: datetime,
    ) -> RetentionRun:
        current = self._require_live_claim(scope, claim, now)
        if isinstance(expired_count, bool) or not 0 <= expired_count <= len(claim.planned_refs):
            raise ValidationError("invalid retention expired count")
        completed = replace(
            current,
            status="completed",
            expired_count=expired_count,
            lease_owner=None,
            lease_expires_at=None,
            safe_error_code=None,
            completed_at=now,
        )
        with self._lock:
            self._runs[(scope.site_id, claim.run_ref)] = completed
        return completed

    def fail(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        safe_error_code: str,
        now: datetime,
    ) -> RetentionRun:
        current = self._require_live_claim(scope, claim, now)
        if safe_error_code != "retention_apply_failed":
            raise ValidationError("unsafe retention error code")
        status = "dead_letter" if current.attempt >= _MAX_ATTEMPTS else "retry"
        failed = replace(
            current,
            status=status,
            expired_count=0,
            lease_owner=None,
            lease_expires_at=None,
            next_attempt_at=now + _RETRY_DELAY,
            safe_error_code=safe_error_code,
        )
        with self._lock:
            self._runs[(scope.site_id, claim.run_ref)] = failed
        return failed

    def expiration_receipts(self, scope: TenantScope) -> tuple[ContentExpirationReceipt, ...]:
        return tuple(
            sorted(
                (item for (site_id, _), item in self._receipts.items() if site_id == scope.site_id),
                key=lambda item: item.projection_ref,
            )
        )

    def _require_live_claim(
        self, scope: TenantScope, claim: RetentionClaim, now: datetime
    ) -> RetentionRun:
        _aware(now, "retention transition time")
        current = self._runs.get((scope.site_id, claim.run_ref))
        if (
            current is None
            or current.status != "leased"
            or current.lease_owner != claim.worker_id
            or current.attempt != claim.attempt
            or current.lease_generation != claim.generation
            or current.lease_expires_at is None
            or current.lease_expires_at < now
            or not hmac.compare_digest(_fence_token(current, claim.worker_id), claim.fence_token)
        ):
            raise ValidationError("retention lease fence conflict")
        return current


class RetentionScheduler:
    """One-at-a-time retention executor; it never opens or deletes Observer CAS."""

    def __init__(
        self,
        repository: RetentionRunRepository,
        *,
        emergency_stop: Callable[[], bool],
        observer_tombstone_verifier: ObserverTombstoneVerifier,
        metrics: GatewayMetrics | None = None,
        planner: RetentionPlanner | None = None,
    ) -> None:
        self.repository = repository
        self.emergency_stop = emergency_stop
        self.observer_tombstone_verifier = observer_tombstone_verifier
        self.metrics = metrics
        self.planner = planner or RetentionPlanner()

    def schedule(
        self,
        scope: TenantScope,
        *,
        run_ref: str,
        idempotency_key: str,
        projections: tuple[ContentProjection, ...],
        dry_run: bool,
        now: datetime,
        legal_hold_evidence_refs: frozenset[str] = frozenset(),
    ) -> RetentionRun:
        _safe_identifier(run_ref, "run ref")
        _safe_identifier(idempotency_key, "idempotency key")
        _aware(now, "schedule time")
        if not isinstance(dry_run, bool):
            raise ValidationError("invalid retention dry-run flag")
        planned_refs = self.planner.plan(
            scope,
            projections,
            now=now,
            legal_hold_evidence_refs=legal_hold_evidence_refs,
        )
        payload_digest = canonical_digest(
            {
                "site_id": scope.site_id,
                "dry_run": dry_run,
                "planned_refs": planned_refs,
                "projection_digests": tuple(
                    (item.projection_ref, item.payload_digest) for item in projections
                ),
            }
        )
        return self.repository.enqueue(
            scope,
            RetentionRun(
                run_ref=run_ref,
                site_id=scope.site_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                dry_run=dry_run,
                status="queued",
                projections=projections,
                planned_refs=planned_refs,
                planned_count=len(planned_refs),
                expired_count=0,
                attempt=0,
                lease_owner=None,
                lease_expires_at=None,
                lease_generation=0,
                next_attempt_at=now,
                safe_error_code=None,
                created_at=now,
            ),
        )

    def run_once(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> RetentionRun | None:
        if self.emergency_stop():
            return None
        if self.metrics is not None:
            self.metrics.record_persisted_heartbeat("retention", at=now)
        claim = self.repository.claim(scope, worker_id=worker_id, now=now, limit=limit)
        if claim is None:
            return None
        try:
            expired_count = 0
            if not claim.dry_run:
                for projection in claim.projections:
                    if not self.observer_tombstone_verifier.verify_tombstone(
                        scope,
                        projection,
                        now=now,
                    ):
                        raise ValidationError("Observer tombstone verification required")
                    self.repository.record_expiration(
                        scope,
                        claim=claim,
                        projection=projection,
                        now=now,
                    )
                    expired_count += 1
            return self.repository.complete(
                scope,
                claim=claim,
                expired_count=expired_count,
                now=now,
            )
        except Exception:
            failed = self.repository.fail(
                scope,
                claim=claim,
                safe_error_code="retention_apply_failed",
                now=now,
            )
            if self.metrics is not None and failed.status == "dead_letter":
                self.metrics.increment(
                    "gbos_email_gateway_dead_letter_total",
                    labels={"work_kind": "retention"},
                )
            return failed


def _fence_token(run: RetentionRun, worker_id: str) -> str:
    material = (
        f"{run.site_id}\x1f{run.run_ref}\x1f{worker_id}\x1f{run.attempt}\x1f{run.lease_generation}"
    ).encode()
    return "v1:" + hashlib.sha256(material).hexdigest()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"invalid {name}")


def _safe_identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 160
        or "@" in value
    ):
        raise ValidationError(f"invalid retention {name}")
