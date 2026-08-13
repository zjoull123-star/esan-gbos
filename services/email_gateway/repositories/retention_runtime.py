from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from ..models import (
    ContentProjection,
    IdempotencyConflict,
    TenantScope,
    ValidationError,
    canonical_digest,
    require_scope,
    stable_ref,
)
from ..postgres import Connection, redacted_database_errors, site_transaction
from ..retention import (
    ContentExpirationReceipt,
    RetentionClaim,
    RetentionRun,
)

_LEASE_SECONDS = 30
_RETRY_DELAY = timedelta(seconds=60)
_MAX_BATCH = 100


class PostgresRetentionRepository:
    """Fenced PostgreSQL retention runs over immutable draft expiry projections."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __repr__(self) -> str:
        return "PostgresRetentionRepository(connection=<redacted>)"

    def discover_due_projections(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ContentProjection, ...]:
        _aware(now)
        _limit(limit)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT draft.draft_ref, draft.site_id, draft.content_evidence_ref,
                       draft.content_digest, draft.content_expires_at,
                       draft.observer_tombstone_receipt_ref
                  FROM email_gateway.reply_drafts AS draft
                 WHERE draft.site_id = %s
                   AND draft.state IN ('discarded', 'terminal')
                   AND draft.terminal_at IS NOT NULL
                   AND draft.content_expires_at <= %s
                   AND draft.legal_hold_ref IS NULL
                   AND draft.observer_tombstone_receipt_ref IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1
                         FROM email_gateway.content_expiration_receipts AS receipt
                        WHERE receipt.site_id = draft.site_id
                          AND receipt.projection_ref = draft.draft_ref
                   )
                 ORDER BY draft.content_expires_at, draft.draft_ref
                 LIMIT %s
                """,
                (scope.site_id, now, limit),
            )
            rows = cursor.fetchall()
        return tuple(_projection(row) for row in rows)

    def enqueue(self, scope: TenantScope, run: RetentionRun) -> RetentionRun:
        require_scope(scope, site_id=run.site_id)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT run_ref, payload_digest
                  FROM email_gateway.retention_runs
                 WHERE site_id = %s AND idempotency_key = %s
                 FOR UPDATE
                """,
                (scope.site_id, run.idempotency_key),
            )
            replay = cursor.fetchone()
            if replay is not None:
                if str(replay[1]) != run.payload_digest:
                    raise IdempotencyConflict("retention idempotency drift")
                return self._load_run(cursor, scope, str(replay[0]))
            cursor.execute(
                """
                INSERT INTO email_gateway.retention_runs (
                    site_id, run_ref, status, attempt, lease_generation, dry_run,
                    planned_count, expired_count, payload_digest, idempotency_key,
                    next_attempt_at, max_attempts, created_at, updated_at
                ) VALUES (%s, %s, 'queued', 0, 0, %s, %s, 0, %s, %s, %s, 5, %s, %s)
                """,
                (
                    scope.site_id,
                    run.run_ref,
                    run.dry_run,
                    len(run.projections),
                    run.payload_digest,
                    run.idempotency_key,
                    run.next_attempt_at,
                    run.created_at,
                    run.created_at,
                ),
            )
            for projection in run.projections:
                require_scope(scope, site_id=projection.site_id)
                if projection.observer_expiration_receipt_ref is None:
                    raise ValidationError("Observer tombstone receipt required")
                cursor.execute(
                    """
                    INSERT INTO email_gateway.retention_run_items (
                        site_id, run_ref, projection_ref, evidence_ref,
                        observer_tombstone_receipt_ref, payload_digest, expires_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scope.site_id,
                        run.run_ref,
                        projection.projection_ref,
                        projection.evidence_ref,
                        projection.observer_expiration_receipt_ref,
                        projection.payload_digest,
                        projection.expires_at,
                        run.created_at,
                    ),
                )
            self._audit(
                cursor,
                scope,
                run.run_ref,
                None,
                "enqueued",
                run.payload_digest,
                run.created_at,
            )
        return run

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
    ) -> RetentionClaim | None:
        _identifier(worker_id)
        _aware(now)
        _limit(limit)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                "SELECT * FROM email_gateway.claim_human_retention_run(%s, %s, %s, %s)",
                (scope.site_id, worker_id, now, _LEASE_SECONDS),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            run_ref, attempt, generation, lease_expires_at, dry_run = row
            cursor.execute(
                """
                SELECT projection_ref, site_id, evidence_ref, payload_digest, expires_at,
                       observer_tombstone_receipt_ref
                  FROM email_gateway.retention_run_items
                 WHERE site_id = %s AND run_ref = %s
                 ORDER BY projection_ref
                 LIMIT %s
                """,
                (scope.site_id, str(run_ref), limit),
            )
            projections = tuple(_projection(item) for item in cursor.fetchall())
            planned_refs = tuple(item.projection_ref for item in projections)
            digest = canonical_digest(
                {
                    "run_ref": str(run_ref),
                    "worker_id": worker_id,
                    "attempt": int(attempt),
                    "generation": int(generation),
                }
            )
            self._audit(cursor, scope, str(run_ref), None, "claimed", digest, now)
            return RetentionClaim(
                run_ref=str(run_ref),
                worker_id=worker_id,
                attempt=int(attempt),
                generation=int(generation),
                fence_token=_fence_token(
                    scope.site_id,
                    str(run_ref),
                    worker_id,
                    int(attempt),
                    int(generation),
                ),
                lease_expires_at=lease_expires_at,
                dry_run=bool(dry_run),
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
        require_scope(scope, site_id=projection.site_id)
        _aware(now)
        self._validate_fence_value(scope, claim)
        if projection.observer_expiration_receipt_ref is None:
            raise ValidationError("Observer tombstone receipt required")
        receipt_ref = stable_ref("EXP", scope.site_id, claim.run_ref, projection.projection_ref)
        digest = canonical_digest(
            {
                "run_ref": claim.run_ref,
                "projection_ref": projection.projection_ref,
                "observer_expiration_receipt_ref": projection.observer_expiration_receipt_ref,
            }
        )
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            self._require_live_claim(cursor, scope, claim, now)
            cursor.execute(
                """
                INSERT INTO email_gateway.content_expiration_receipts (
                    site_id, expiration_receipt_ref, projection_ref,
                    observer_expiration_receipt_ref, evidence_ref, payload_digest,
                    expired_at, run_ref, legal_hold_checked_at
                )
                SELECT draft.site_id, %s, draft.draft_ref,
                       draft.observer_tombstone_receipt_ref, draft.content_evidence_ref,
                       %s, %s, %s, %s
                  FROM email_gateway.reply_drafts AS draft
                  JOIN email_gateway.retention_run_items AS item
                    ON item.site_id = draft.site_id
                   AND item.projection_ref = draft.draft_ref
                   AND item.run_ref = %s
                 WHERE draft.site_id = %s
                   AND draft.draft_ref = %s
                   AND draft.state IN ('discarded', 'terminal')
                   AND draft.content_expires_at <= %s
                   AND draft.legal_hold_ref IS NULL
                   AND draft.observer_tombstone_receipt_ref = %s
                ON CONFLICT (site_id, projection_ref) DO NOTHING
                """,
                (
                    receipt_ref,
                    digest,
                    now,
                    claim.run_ref,
                    now,
                    claim.run_ref,
                    scope.site_id,
                    projection.projection_ref,
                    now,
                    projection.observer_expiration_receipt_ref,
                ),
            )
            cursor.execute(
                """
                SELECT expiration_receipt_ref, site_id, run_ref, projection_ref,
                       observer_expiration_receipt_ref, evidence_ref, payload_digest, expired_at
                  FROM email_gateway.content_expiration_receipts
                 WHERE site_id = %s AND projection_ref = %s
                """,
                (scope.site_id, projection.projection_ref),
            )
            row = cursor.fetchone()
            if row is None or str(row[4]) != projection.observer_expiration_receipt_ref:
                raise ValidationError("retention eligibility changed")
            self._audit(
                cursor,
                scope,
                claim.run_ref,
                projection.projection_ref,
                "content_expired",
                digest,
                now,
            )
            return _receipt(row)

    def complete(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        expired_count: int,
        now: datetime,
    ) -> RetentionRun:
        if isinstance(expired_count, bool) or not 0 <= expired_count <= len(claim.planned_refs):
            raise ValidationError("invalid retention expired count")
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            self._require_live_claim(cursor, scope, claim, now)
            cursor.execute(
                """
                UPDATE email_gateway.retention_runs
                   SET status = 'completed', expired_count = %s, lease_owner = NULL,
                       lease_expires_at = NULL, safe_error_code = NULL,
                       completed_at = %s, updated_at = %s
                 WHERE site_id = %s AND run_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s AND lease_generation = %s
                """,
                (
                    expired_count,
                    now,
                    now,
                    scope.site_id,
                    claim.run_ref,
                    claim.worker_id,
                    claim.attempt,
                    claim.generation,
                ),
            )
            self._audit(
                cursor,
                scope,
                claim.run_ref,
                None,
                "completed",
                canonical_digest({"expired_count": expired_count}),
                now,
            )
            return self._load_run(cursor, scope, claim.run_ref)

    def fail(
        self,
        scope: TenantScope,
        *,
        claim: RetentionClaim,
        safe_error_code: str,
        now: datetime,
    ) -> RetentionRun:
        if safe_error_code != "retention_apply_failed":
            raise ValidationError("unsafe retention error code")
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            current = self._require_live_claim(cursor, scope, claim, now)
            status = "dead_letter" if current.attempt >= 5 else "retry"
            cursor.execute(
                """
                UPDATE email_gateway.retention_runs
                   SET status = %s, expired_count = 0, lease_owner = NULL,
                       lease_expires_at = NULL, next_attempt_at = %s,
                       safe_error_code = %s, updated_at = %s
                 WHERE site_id = %s AND run_ref = %s AND status = 'leased'
                   AND lease_owner = %s AND attempt = %s AND lease_generation = %s
                """,
                (
                    status,
                    now + _RETRY_DELAY,
                    safe_error_code,
                    now,
                    scope.site_id,
                    claim.run_ref,
                    claim.worker_id,
                    claim.attempt,
                    claim.generation,
                ),
            )
            self._audit(
                cursor,
                scope,
                claim.run_ref,
                None,
                status,
                canonical_digest({"safe_error_code": safe_error_code}),
                now,
            )
            return self._load_run(cursor, scope, claim.run_ref)

    def record_worker_heartbeat(
        self,
        scope: TenantScope,
        *,
        worker_kind: str,
        at: datetime,
    ) -> int:
        _aware(at)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                "SELECT email_gateway.record_email_gateway_worker_heartbeat(%s, %s, %s)",
                (scope.site_id, worker_kind, at),
            )
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("worker heartbeat rejected")
            return int(row[0])

    def heartbeat_snapshot(self, scope: TenantScope) -> dict[str, datetime]:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT worker_kind, heartbeat_at
                  FROM email_gateway.worker_heartbeats
                 WHERE site_id = %s
                 ORDER BY worker_kind
                """,
                (scope.site_id,),
            )
            return {str(row[0]): row[1] for row in cursor.fetchall()}

    def retention_health(self, scope: TenantScope, *, now: datetime) -> tuple[int, int]:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status IN ('queued', 'retry', 'leased')),
                    count(*) FILTER (WHERE status = 'dead_letter')
                  FROM email_gateway.retention_runs
                 WHERE site_id = %s
                   AND created_at >= %s - interval '30 days'
                """,
                (scope.site_id, now),
            )
            row = cursor.fetchone()
            return (0, 0) if row is None else (int(row[0]), int(row[1]))

    def visible_content_evidence_ref(
        self,
        scope: TenantScope,
        *,
        draft_ref: str,
    ) -> str | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT content_evidence_ref
                  FROM email_gateway.visible_reply_draft_content
                 WHERE site_id = %s AND draft_ref = %s
                """,
                (scope.site_id, draft_ref),
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])

    def expiration_receipts(self, scope: TenantScope) -> tuple[ContentExpirationReceipt, ...]:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT expiration_receipt_ref, site_id, run_ref, projection_ref,
                       observer_expiration_receipt_ref, evidence_ref, payload_digest, expired_at
                  FROM email_gateway.content_expiration_receipts
                 WHERE site_id = %s ORDER BY projection_ref
                """,
                (scope.site_id,),
            )
            return tuple(_receipt(row) for row in cursor.fetchall())

    def _require_live_claim(
        self,
        cursor: Any,
        scope: TenantScope,
        claim: RetentionClaim,
        now: datetime,
    ) -> RetentionRun:
        self._validate_fence_value(scope, claim)
        run = self._load_run(cursor, scope, claim.run_ref)
        if (
            run.status != "leased"
            or run.lease_owner != claim.worker_id
            or run.attempt != claim.attempt
            or run.lease_generation != claim.generation
            or run.lease_expires_at is None
            or run.lease_expires_at < now
        ):
            raise ValidationError("retention lease fence conflict")
        return run

    @staticmethod
    def _validate_fence_value(scope: TenantScope, claim: RetentionClaim) -> None:
        expected = _fence_token(
            scope.site_id,
            claim.run_ref,
            claim.worker_id,
            claim.attempt,
            claim.generation,
        )
        if claim.fence_token != expected:
            raise ValidationError("retention lease fence conflict")

    @staticmethod
    def _load_run(cursor: Any, scope: TenantScope, run_ref: str) -> RetentionRun:
        cursor.execute(
            """
            SELECT run_ref, site_id, idempotency_key, payload_digest, dry_run, status,
                   planned_count, expired_count, attempt, lease_owner, lease_expires_at,
                   lease_generation, next_attempt_at, safe_error_code, created_at, completed_at
              FROM email_gateway.retention_runs
             WHERE site_id = %s AND run_ref = %s
            """,
            (scope.site_id, run_ref),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValidationError("retention run unavailable")
        cursor.execute(
            """
            SELECT projection_ref, site_id, evidence_ref, payload_digest, expires_at,
                   observer_tombstone_receipt_ref
              FROM email_gateway.retention_run_items
             WHERE site_id = %s AND run_ref = %s ORDER BY projection_ref
            """,
            (scope.site_id, run_ref),
        )
        projections = tuple(_projection(item) for item in cursor.fetchall())
        return RetentionRun(
            run_ref=str(row[0]),
            site_id=str(row[1]),
            idempotency_key=str(row[2]),
            payload_digest=str(row[3]),
            dry_run=bool(row[4]),
            status=str(row[5]),
            projections=projections,
            planned_refs=tuple(item.projection_ref for item in projections),
            planned_count=int(row[6]),
            expired_count=int(row[7]),
            attempt=int(row[8]),
            lease_owner=None if row[9] is None else str(row[9]),
            lease_expires_at=row[10],
            lease_generation=int(row[11]),
            next_attempt_at=row[12],
            safe_error_code=None if row[13] is None else str(row[13]),
            created_at=row[14],
            completed_at=row[15],
        )

    @staticmethod
    def _audit(
        cursor: Any,
        scope: TenantScope,
        run_ref: str,
        projection_ref: str | None,
        event_kind: str,
        payload_digest: str,
        occurred_at: datetime,
    ) -> None:
        audit_ref = stable_ref(
            "RAU",
            scope.site_id,
            run_ref,
            projection_ref or "run",
            event_kind,
            payload_digest,
        )
        cursor.execute(
            """
            INSERT INTO email_gateway.retention_audit_events (
                site_id, audit_event_ref, run_ref, projection_ref,
                event_kind, payload_digest, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (site_id, audit_event_ref) DO NOTHING
            """,
            (
                scope.site_id,
                audit_ref,
                run_ref,
                projection_ref,
                event_kind,
                payload_digest,
                occurred_at,
            ),
        )


def _projection(row: tuple[Any, ...]) -> ContentProjection:
    receipt = None if row[5] is None else str(row[5])
    return ContentProjection(
        projection_ref=str(row[0]),
        site_id=str(row[1]),
        kind="draft_projection",
        identity_ref=None,
        evidence_ref=str(row[2]),
        expires_at=row[4],
        observer_expiration_receipt_ref=receipt,
        payload_digest=str(row[3]),
        active_draft_ref=None,
        confirmed=False,
    )


def _receipt(row: tuple[Any, ...]) -> ContentExpirationReceipt:
    return ContentExpirationReceipt(
        expiration_receipt_ref=str(row[0]),
        site_id=str(row[1]),
        run_ref=str(row[2]),
        projection_ref=str(row[3]),
        observer_expiration_receipt_ref=str(row[4]),
        evidence_ref=str(row[5]),
        payload_digest=str(row[6]),
        expired_at=row[7],
    )


def _fence_token(
    site_id: str,
    run_ref: str,
    worker_id: str,
    attempt: int,
    generation: int,
) -> str:
    material = f"{site_id}\x1f{run_ref}\x1f{worker_id}\x1f{attempt}\x1f{generation}".encode()
    return "v1:" + hashlib.sha256(material).hexdigest()


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("invalid retention time")


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_BATCH:
        raise ValidationError("invalid retention batch limit")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or "@" in value:
        raise ValidationError("invalid retention worker")


__all__ = ["PostgresRetentionRepository"]
