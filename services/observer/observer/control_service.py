from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from .local_pilot_storage import LocalPilotStorage
from .models import ConnectorKey, TenantScope, _require_aware, stable_ulid
from .storage import Connection

_COMMAND_KEY_MIN = 8
_COMMAND_KEY_MAX = 256
_REPLAY_LIMIT = 100
_REPLAY_RETENTION = timedelta(days=30)


class RevisionConflict(ValueError):
    """The caller's connector revision is stale."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different control command."""


class ConnectorNotFound(LookupError):
    """The requested site-local connector instance does not exist."""


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    instance_id: str
    channel: str
    status: str
    checkpoint_version: int
    backlog: int
    last_success_at: datetime | None
    safe_error_code: str | None
    freshness: str
    revision: int

    def __post_init__(self) -> None:
        if not self.instance_id or len(self.instance_id) > 256:
            raise ValueError("invalid connector instance_id")
        if not self.channel or len(self.channel) > 80:
            raise ValueError("invalid connector channel")
        if self.status not in {"enabled", "paused", "error", "disabled"}:
            raise ValueError("invalid connector status")
        if self.checkpoint_version < 0 or self.backlog < 0 or self.revision < 0:
            raise ValueError("connector counters must be non-negative")
        if self.last_success_at is not None:
            _require_aware(self.last_success_at, "last_success_at")
        if self.safe_error_code is not None and len(self.safe_error_code) > 80:
            raise ValueError("invalid safe_error_code")
        if self.freshness not in {"fresh", "stale", "unknown"}:
            raise ValueError("invalid connector freshness")

    def as_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "channel": self.channel,
            "status": self.status,
            "checkpoint_version": self.checkpoint_version,
            "backlog": self.backlog,
            "last_success_at": self.last_success_at,
            "safe_error_code": self.safe_error_code,
            "freshness": self.freshness,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class ConnectorControlResult:
    status: ConnectorStatus
    replayed_count: int
    replayed: bool

    def __post_init__(self) -> None:
        if self.replayed_count < 0 or self.replayed_count > _REPLAY_LIMIT:
            raise ValueError("invalid replayed_count")


class ControlRepository(Protocol):
    """Atomic PostgreSQL-facing seam for connector control operations."""

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None,
    ) -> tuple[ConnectorStatus, ...]: ...

    def resolve_connector(
        self,
        scope: TenantScope,
        *,
        instance_id: str,
    ) -> ConnectorKey:
        """Return exactly one site-local connector or fail closed."""
        ...

    def mutate_status(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        target_status: str,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        now: datetime,
    ) -> ConnectorControlResult: ...

    def replay_failed(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        cutoff: datetime,
        limit: int,
        now: datetime,
    ) -> ConnectorControlResult: ...


class PostgresControlRepository:
    """Site-scoped connector control backed by the 006 command ledger."""

    __slots__ = ("_connection", "_replay_storage")

    def __init__(
        self,
        *,
        connection: Connection,
        replay_storage: LocalPilotStorage,
    ) -> None:
        self._connection = connection
        self._replay_storage = replay_storage

    def __repr__(self) -> str:
        return "PostgresControlRepository(connection=<redacted>, replay_storage=<redacted>)"

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None,
    ) -> tuple[ConnectorStatus, ...]:
        predicates = ["instance.site_id = %s"]
        params: list[Any] = [scope.site_id]
        if channel is not None:
            predicates.append("instance.connector = %s")
            params.append(channel)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_STATUS_COLUMNS}
                FROM observer.connector_instances AS instance
                JOIN observer.connector_checkpoints AS checkpoint
                  ON checkpoint.site_id = instance.site_id
                 AND checkpoint.connector = instance.connector
                 AND checkpoint.connector_instance_id =
                     instance.connector_instance_id
                WHERE {" AND ".join(predicates)}
                ORDER BY instance.connector ASC,
                         instance.connector_instance_id ASC
                """,
                tuple(params),
            )
            return tuple(_status_from_row(row) for row in cursor.fetchall())

    def resolve_connector(
        self,
        scope: TenantScope,
        *,
        instance_id: str,
    ) -> ConnectorKey:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            cursor.execute(
                """
                SELECT connector, connector_instance_id
                FROM observer.connector_instances
                WHERE site_id = %s
                  AND connector_instance_id = %s
                ORDER BY connector ASC
                LIMIT 2
                """,
                (scope.site_id, instance_id),
            )
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise ConnectorNotFound("instance_id is absent or ambiguous inside site")
        return ConnectorKey(str(rows[0][0]), str(rows[0][1]))

    def mutate_status(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        target_status: str,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        now: datetime,
    ) -> ConnectorControlResult:
        operation = "pause" if target_status == "paused" else "resume"
        database_status = "paused" if target_status == "paused" else "healthy"
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            existing = _lock_and_load_command(
                cursor,
                scope,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if existing is not None:
                return existing
            cursor.execute(
                """
                UPDATE observer.connector_instances
                SET status = %s,
                    control_revision = control_revision + 1,
                    updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND control_revision = %s
                RETURNING control_revision
                """,
                (
                    database_status,
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    expected_revision,
                ),
            )
            if cursor.fetchone() is None:
                _raise_control_miss(cursor, scope, key)
            cursor.execute(
                """
                UPDATE observer.connector_checkpoints
                SET status = %s,
                    last_error_code = CASE
                      WHEN %s = 'healthy' THEN NULL
                      ELSE last_error_code
                    END,
                    updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                """,
                (
                    database_status,
                    database_status,
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                ),
            )
            status = _load_status(cursor, scope, key)
            result = ConnectorControlResult(
                status=status,
                replayed_count=0,
                replayed=False,
            )
            _insert_command(
                cursor,
                scope,
                key,
                operation=operation,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                result=result,
                now=now,
            )
            return result

    def replay_failed(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        cutoff: datetime,
        limit: int,
        now: datetime,
    ) -> ConnectorControlResult:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, scope)
            existing = _lock_and_load_command(
                cursor,
                scope,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if existing is not None:
                return existing
            cursor.execute(
                """
                UPDATE observer.connector_instances
                SET control_revision = control_revision + 1,
                    updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND control_revision = %s
                RETURNING control_revision
                """,
                (
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    expected_revision,
                ),
            )
            if cursor.fetchone() is None:
                _raise_control_miss(cursor, scope, key)
            cursor.execute(
                """
                SELECT delivery.delivery_id
                FROM observer.inbound_deliveries AS delivery
                JOIN observer.connector_checkpoints AS checkpoint
                  ON checkpoint.site_id = delivery.site_id
                 AND checkpoint.connector = delivery.connector
                 AND checkpoint.connector_instance_id =
                     delivery.connector_instance_id
                WHERE delivery.site_id = %s
                  AND delivery.connector = %s
                  AND delivery.connector_instance_id = %s
                  AND delivery.processing_status = 'failed'
                  AND delivery.object_ref IS NOT NULL
                  AND delivery.byte_size IS NOT NULL
                  AND delivery.received_at >= %s
                  AND delivery.received_at >= (
                    %s - make_interval(secs => checkpoint.replay_window_seconds)
                  )
                  AND delivery.received_at <= %s
                ORDER BY delivery.received_at ASC, delivery.delivery_id ASC
                LIMIT %s
                FOR UPDATE OF delivery SKIP LOCKED
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    cutoff,
                    now,
                    now,
                    limit,
                ),
            )
            delivery_ids = tuple(str(row[0]) for row in cursor.fetchall())
            for delivery_id in delivery_ids:
                job_id = stable_ulid(
                    "connector-control-replay-job",
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    idempotency_key,
                    delivery_id,
                )
                self._replay_storage.replay_delivery(
                    scope,
                    key,
                    delivery_id=delivery_id,
                    job_id=job_id,
                    idempotency_key=f"replay:{job_id}",
                    now=now,
                    max_attempts=3,
                )
            status = _load_status(cursor, scope, key)
            result = ConnectorControlResult(
                status=status,
                replayed_count=len(delivery_ids),
                replayed=False,
            )
            _insert_command(
                cursor,
                scope,
                key,
                operation="replay",
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                result=result,
                now=now,
            )
            return result


class LocalPilotControlService:
    """Enforces CAS, idempotency and bounded replay before durable mutation."""

    __slots__ = ("_clock", "_repository")

    def __init__(
        self,
        *,
        repository: ControlRepository,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._clock = clock

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None = None,
    ) -> tuple[ConnectorStatus, ...]:
        if channel is not None and (not channel or len(channel) > 80):
            raise ValueError("invalid channel")
        return self._repository.list_status(scope, channel=channel)

    def resolve_instance(
        self,
        scope: TenantScope,
        *,
        instance_id: str,
    ) -> ConnectorKey:
        if (
            not isinstance(instance_id, str)
            or not instance_id
            or instance_id != instance_id.strip()
            or len(instance_id) > 256
        ):
            raise ValueError("invalid instance_id")
        return self._repository.resolve_connector(
            scope,
            instance_id=instance_id,
        )

    def pause(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ConnectorControlResult:
        return self._mutate(
            scope,
            key,
            operation="pause",
            target_status="paused",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def resume(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ConnectorControlResult:
        return self._mutate(
            scope,
            key,
            operation="resume",
            target_status="enabled",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def replay(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
        limit: int = _REPLAY_LIMIT,
    ) -> ConnectorControlResult:
        _validate_command(expected_revision, idempotency_key)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _REPLAY_LIMIT:
            raise ValueError("replay limit must be between 1 and 100")
        now = self._now()
        request_digest = _command_digest(
            scope,
            key,
            operation="replay",
            expected_revision=expected_revision,
            values={"limit": limit},
        )
        return self._repository.replay_failed(
            scope,
            key,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            cutoff=now - _REPLAY_RETENTION,
            limit=limit,
            now=now,
        )

    def _mutate(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        operation: str,
        target_status: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> ConnectorControlResult:
        _validate_command(expected_revision, idempotency_key)
        now = self._now()
        request_digest = _command_digest(
            scope,
            key,
            operation=operation,
            expected_revision=expected_revision,
            values={"target_status": target_status},
        )
        return self._repository.mutate_status(
            scope,
            key,
            target_status=target_status,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            now=now,
        )

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now


def _validate_command(expected_revision: int, idempotency_key: str) -> None:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be non-negative")
    if (
        not isinstance(idempotency_key, str)
        or idempotency_key != idempotency_key.strip()
        or not _COMMAND_KEY_MIN <= len(idempotency_key) <= _COMMAND_KEY_MAX
    ):
        raise ValueError("invalid idempotency_key")


def _command_digest(
    scope: TenantScope,
    key: ConnectorKey,
    *,
    operation: str,
    expected_revision: int,
    values: dict[str, object],
) -> str:
    document = {
        "site_id": scope.site_id,
        "processing_purpose": scope.processing_purpose,
        "connector": key.connector,
        "instance_id": key.instance_id,
        "operation": operation,
        "expected_revision": expected_revision,
        **values,
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_STATUS_COLUMNS = """
    instance.connector_instance_id,
    instance.connector,
    CASE
      WHEN instance.status = 'paused' OR checkpoint.status = 'paused'
        THEN 'paused'
      WHEN instance.status IN ('degraded', 'failed')
        OR checkpoint.status IN ('degraded', 'failed') THEN 'error'
      ELSE 'enabled'
    END,
    checkpoint.checkpoint_version,
    (
      SELECT count(*)
      FROM observer.processing_jobs AS job
      WHERE job.site_id = instance.site_id
        AND job.connector = instance.connector
        AND job.connector_instance_id = instance.connector_instance_id
        AND job.status IN ('queued', 'processing', 'retry_wait')
    ),
    checkpoint.last_success_at,
    checkpoint.last_error_code,
    CASE
      WHEN checkpoint.last_success_at IS NULL THEN 'unknown'
      WHEN checkpoint.last_success_at >= current_timestamp - interval '5 minutes'
        THEN 'fresh'
      ELSE 'stale'
    END,
    instance.control_revision
"""


def _set_site(cursor: Any, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))


def _status_from_row(row: tuple[Any, ...]) -> ConnectorStatus:
    return ConnectorStatus(
        instance_id=str(row[0]),
        channel=str(row[1]),
        status=str(row[2]),
        checkpoint_version=int(row[3]),
        backlog=int(row[4]),
        last_success_at=row[5],
        safe_error_code=None if row[6] is None else str(row[6]),
        freshness=str(row[7]),
        revision=int(row[8]),
    )


def _load_status(
    cursor: Any,
    scope: TenantScope,
    key: ConnectorKey,
) -> ConnectorStatus:
    cursor.execute(
        f"""
        SELECT {_STATUS_COLUMNS}
        FROM observer.connector_instances AS instance
        JOIN observer.connector_checkpoints AS checkpoint
          ON checkpoint.site_id = instance.site_id
         AND checkpoint.connector = instance.connector
         AND checkpoint.connector_instance_id = instance.connector_instance_id
        WHERE instance.site_id = %s
          AND instance.connector = %s
          AND instance.connector_instance_id = %s
        """,
        (scope.site_id, key.connector, key.instance_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ConnectorNotFound("connector instance does not exist")
    return _status_from_row(row)


def _lock_and_load_command(
    cursor: Any,
    scope: TenantScope,
    *,
    idempotency_key: str,
    request_digest: str,
) -> ConnectorControlResult | None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{scope.site_id}\x1f{idempotency_key}",),
    )
    cursor.execute(
        """
        SELECT request_digest, replayed_count, response_status
        FROM observer.connector_control_commands
        WHERE site_id = %s AND idempotency_key = %s
        """,
        (scope.site_id, idempotency_key),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    if str(row[0]) != request_digest:
        raise IdempotencyConflict("idempotency key was used for another command")
    payload = row[2]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid persisted connector status")
    last_success = payload.get("last_success_at")
    if isinstance(last_success, str):
        last_success = datetime.fromisoformat(last_success)
    status = ConnectorStatus(
        instance_id=str(payload["instance_id"]),
        channel=str(payload["channel"]),
        status=str(payload["status"]),
        checkpoint_version=int(payload["checkpoint_version"]),
        backlog=int(payload["backlog"]),
        last_success_at=last_success,
        safe_error_code=(
            None if payload.get("safe_error_code") is None else str(payload["safe_error_code"])
        ),
        freshness=str(payload["freshness"]),
        revision=int(payload["revision"]),
    )
    return ConnectorControlResult(
        status=status,
        replayed_count=int(row[1]),
        replayed=True,
    )


def _insert_command(
    cursor: Any,
    scope: TenantScope,
    key: ConnectorKey,
    *,
    operation: str,
    expected_revision: int,
    idempotency_key: str,
    request_digest: str,
    result: ConnectorControlResult,
    now: datetime,
) -> None:
    cursor.execute(
        """
        INSERT INTO observer.connector_control_commands (
          site_id, idempotency_key, request_digest, connector,
          connector_instance_id, operation, expected_revision,
          result_revision, replayed_count, response_status, created_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
        )
        """,
        (
            scope.site_id,
            idempotency_key,
            request_digest,
            key.connector,
            key.instance_id,
            operation,
            expected_revision,
            result.status.revision,
            result.replayed_count,
            json.dumps(
                result.status.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: value.isoformat(),
            ),
            now,
        ),
    )


def _raise_control_miss(
    cursor: Any,
    scope: TenantScope,
    key: ConnectorKey,
) -> None:
    cursor.execute(
        """
        SELECT control_revision
        FROM observer.connector_instances
        WHERE site_id = %s
          AND connector = %s
          AND connector_instance_id = %s
        """,
        (scope.site_id, key.connector, key.instance_id),
    )
    if cursor.fetchone() is None:
        raise ConnectorNotFound("connector instance does not exist")
    raise RevisionConflict("connector revision is stale")
