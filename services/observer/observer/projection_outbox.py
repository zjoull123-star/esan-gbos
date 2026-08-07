"""Least-privilege fenced access to the Observer model projection outbox."""

from __future__ import annotations

import hashlib
import hmac
import re
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .models import TenantScope, _require_aware

if TYPE_CHECKING:
    from services.local_pilot_runtime.model_projection_worker import ProjectionOutboxClaim

_SAFE_TEXT = re.compile(r"^[^\x00\r\n]{1,256}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_PURPOSE = "observation_processing"


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresProjectionOutboxRepository:
    """Claim and transition model projection rows under attempt/generation fences."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresProjectionOutboxRepository(connection=<redacted>)"

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProjectionOutboxClaim | None:
        self._validate_scope(scope)
        _identifier(worker_id, "worker_id")
        _require_aware(now, "now")
        _duration(lease_duration)
        lease_expires_at = now + lease_duration
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT outbox.site_id, outbox.outbox_id
                    FROM observer.context_publication_outbox AS outbox
                    JOIN observer.observation_events AS event
                      ON event.site_id = outbox.site_id
                     AND event.event_id = outbox.observation_event_id
                    WHERE outbox.site_id = %s
                      AND event.processing_purpose = %s
                      AND outbox.attempt_count < outbox.max_attempts
                      AND (
                        (
                          outbox.status IN ('queued', 'retry_wait')
                          AND outbox.next_retry_at <= %s
                        )
                        OR (
                          outbox.status = 'leased'
                          AND outbox.lease_expires_at <= %s
                        )
                      )
                    ORDER BY outbox.next_retry_at, outbox.created_at, outbox.outbox_id
                    FOR UPDATE OF outbox SKIP LOCKED
                    LIMIT 1
                )
                UPDATE observer.context_publication_outbox AS outbox
                SET status = 'leased',
                    attempt_count = outbox.attempt_count + 1,
                    lease_generation = outbox.lease_generation + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    last_error_code = NULL,
                    updated_at = %s
                FROM candidate
                WHERE outbox.site_id = candidate.site_id
                  AND outbox.outbox_id = candidate.outbox_id
                RETURNING outbox.site_id, outbox.outbox_id,
                          outbox.observation_event_id, outbox.idempotency_key,
                          outbox.status, outbox.attempt_count, outbox.max_attempts,
                          outbox.lease_owner, outbox.lease_expires_at,
                          outbox.lease_generation
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    now,
                    now,
                    worker_id,
                    lease_expires_at,
                    now,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._claim_from_row(row)

    def heartbeat(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        generation = self._validate_transition(
            scope,
            outbox_id=outbox_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        _duration(lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                """
                UPDATE observer.context_publication_outbox AS outbox
                SET lease_expires_at = %s,
                    updated_at = %s
                WHERE outbox.site_id = %s
                  AND outbox.outbox_id = %s
                  AND outbox.status = 'leased'
                  AND outbox.lease_owner = %s
                  AND outbox.attempt_count = %s
                  AND outbox.lease_generation = %s
                  AND outbox.lease_expires_at > %s
                  AND EXISTS (
                    SELECT 1
                    FROM observer.observation_events AS event
                    WHERE event.site_id = outbox.site_id
                      AND event.event_id = outbox.observation_event_id
                      AND event.processing_purpose = %s
                  )
                RETURNING outbox.site_id
                """,
                (
                    now + lease_duration,
                    now,
                    scope.site_id,
                    outbox_id,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                    scope.processing_purpose,
                ),
            )
            if cursor.fetchone() is None:
                raise _lease_conflict()

    def mark_published(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
    ) -> None:
        generation = self._validate_transition(
            scope,
            outbox_id=outbox_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                """
                UPDATE observer.context_publication_outbox AS outbox
                SET status = 'published',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = NULL,
                    updated_at = %s
                WHERE outbox.site_id = %s
                  AND outbox.outbox_id = %s
                  AND outbox.status = 'leased'
                  AND outbox.lease_owner = %s
                  AND outbox.attempt_count = %s
                  AND outbox.lease_generation = %s
                  AND outbox.lease_expires_at > %s
                  AND EXISTS (
                    SELECT 1
                    FROM observer.observation_events AS event
                    WHERE event.site_id = outbox.site_id
                      AND event.event_id = outbox.observation_event_id
                      AND event.processing_purpose = %s
                  )
                RETURNING outbox.site_id
                """,
                (
                    now,
                    scope.site_id,
                    outbox_id,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                    scope.processing_purpose,
                ),
            )
            if cursor.fetchone() is None:
                raise _lease_conflict()

    def mark_failed(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]:
        generation = self._validate_transition(
            scope,
            outbox_id=outbox_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            fence_token=fence_token,
            now=now,
        )
        _require_aware(retry_at, "retry_at")
        if retry_at <= now or _SAFE_CODE.fullmatch(error_code) is None:
            raise ValueError("invalid projection failure metadata")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                """
                UPDATE observer.context_publication_outbox AS outbox
                SET status = CASE
                      WHEN outbox.attempt_count >= outbox.max_attempts
                        THEN 'dead_letter'
                      ELSE 'retry_wait'
                    END,
                    next_retry_at = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE outbox.site_id = %s
                  AND outbox.outbox_id = %s
                  AND outbox.status = 'leased'
                  AND outbox.lease_owner = %s
                  AND outbox.attempt_count = %s
                  AND outbox.lease_generation = %s
                  AND outbox.lease_expires_at > %s
                  AND EXISTS (
                    SELECT 1
                    FROM observer.observation_events AS event
                    WHERE event.site_id = outbox.site_id
                      AND event.event_id = outbox.observation_event_id
                      AND event.processing_purpose = %s
                  )
                RETURNING outbox.status, outbox.attempt_count
                """,
                (
                    retry_at,
                    error_code,
                    now,
                    scope.site_id,
                    outbox_id,
                    worker_id,
                    expected_attempt,
                    generation,
                    now,
                    scope.processing_purpose,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise _lease_conflict()
            status = str(row[0])
            attempt_count = int(row[1])
            if status == "dead_letter":
                dead_letter_id = hashlib.sha256(
                    f"{scope.site_id}\x1f{outbox_id}".encode()
                ).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO observer.local_pilot_dead_letter (
                        site_id, dead_letter_id, outbox_id, reason_code,
                        attempt_count, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (site_id, dead_letter_id) DO NOTHING
                    """,
                    (
                        scope.site_id,
                        dead_letter_id,
                        outbox_id,
                        error_code,
                        attempt_count,
                        now,
                    ),
                )
                return "dead_letter"
            if status != "retry_wait":
                raise RuntimeError("projection outbox returned an invalid safe status")
            return "retry"

    @staticmethod
    def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
        cursor.execute(
            "SELECT set_config('app.processing_purpose', %s, true)",
            (scope.processing_purpose,),
        )

    @staticmethod
    def _validate_scope(scope: TenantScope) -> None:
        if scope.processing_purpose != _PURPOSE:
            raise ValueError("projection outbox requires observation processing scope")

    def _claim_from_row(self, row: tuple[Any, ...]) -> ProjectionOutboxClaim:
        from services.local_pilot_runtime.model_projection_worker import ProjectionOutboxClaim

        if len(row) != 10:
            raise RuntimeError("projection outbox returned an invalid row")
        site_id = str(row[0])
        outbox_id = str(row[1])
        observation_id = str(row[2])
        idempotency_key = str(row[3])
        status = str(row[4])
        attempt = int(row[5])
        max_attempts = int(row[6])
        worker_id = str(row[7])
        lease_expires_at = row[8]
        generation = int(row[9])
        if status != "leased" or generation < 1:
            raise RuntimeError("projection outbox returned an invalid leased row")
        return ProjectionOutboxClaim(
            site_id=site_id,
            outbox_id=outbox_id,
            observation_id=observation_id,
            idempotency_key=idempotency_key,
            status="leased",
            attempt=attempt,
            max_attempts=max_attempts,
            lease_owner=worker_id,
            lease_expires_at=lease_expires_at,
            fence_token=_fence_token(
                site_id=site_id,
                outbox_id=outbox_id,
                worker_id=worker_id,
                attempt=attempt,
                generation=generation,
            ),
        )

    def _validate_transition(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
    ) -> int:
        self._validate_scope(scope)
        _identifier(outbox_id, "outbox_id")
        _identifier(worker_id, "worker_id")
        _require_aware(now, "now")
        if (
            not isinstance(expected_attempt, int)
            or isinstance(expected_attempt, bool)
            or not 1 <= expected_attempt <= 100
        ):
            raise ValueError("invalid expected attempt")
        try:
            prefix, generation_text, supplied_digest = fence_token.split(":")
            generation = int(generation_text)
        except AttributeError, TypeError, ValueError:
            raise _lease_conflict() from None
        expected = _fence_token(
            site_id=scope.site_id,
            outbox_id=outbox_id,
            worker_id=worker_id,
            attempt=expected_attempt,
            generation=generation,
        )
        if (
            prefix != "v1"
            or generation < 1
            or len(supplied_digest) != 64
            or not hmac.compare_digest(expected, fence_token)
        ):
            raise _lease_conflict()
        return generation


def _fence_token(
    *,
    site_id: str,
    outbox_id: str,
    worker_id: str,
    attempt: int,
    generation: int,
) -> str:
    digest = hashlib.sha256(
        f"projection-fence-v1\x1f{site_id}\x1f{outbox_id}\x1f{worker_id}"
        f"\x1f{attempt}\x1f{generation}".encode()
    ).hexdigest()
    return f"v1:{generation}:{digest}"


def _lease_conflict() -> RuntimeError:
    from services.local_pilot_runtime.model_projection_worker import ProjectionLeaseConflict

    return ProjectionLeaseConflict("projection outbox lease transition rejected")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or _SAFE_TEXT.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")


def _duration(value: timedelta) -> None:
    if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(hours=1):
        raise ValueError("projection lease duration is invalid")


__all__ = ["PostgresProjectionOutboxRepository"]
