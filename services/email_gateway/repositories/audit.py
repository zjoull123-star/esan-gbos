from __future__ import annotations

from threading import RLock

from ..models import AuditEvent, IdempotencyConflict, TenantScope, require_scope
from ..postgres import Connection, redacted_database_errors, site_transaction


class InMemoryAuditRepository:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], AuditEvent] = {}
        self._idempotency: dict[tuple[str, str], AuditEvent] = {}
        self._lock = RLock()

    def append(self, scope: TenantScope, event: AuditEvent) -> AuditEvent:
        require_scope(scope, site_id=event.site_id)
        key = (scope.site_id, event.idempotency_key)
        with self._lock:
            replay = self._idempotency.get(key)
            if replay is not None:
                if replay.payload_digest != event.payload_digest or replay != event:
                    raise IdempotencyConflict("audit idempotency drift")
                return replay
            self._events[(scope.site_id, event.audit_ref)] = event
            self._idempotency[key] = event
            return event


class PostgresAuditRepository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __repr__(self) -> str:
        return "PostgresAuditRepository(connection=<redacted>)"

    def append(self, scope: TenantScope, event: AuditEvent) -> AuditEvent:
        require_scope(scope, site_id=event.site_id)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT audit_ref, site_id, actor_ref, event_type, subject_ref,
                       request_id, idempotency_key, payload_digest, occurred_at
                  FROM email_gateway.audit_events
                 WHERE site_id = %s
                   AND (idempotency_key = %s OR audit_ref = %s)
                 ORDER BY CASE WHEN idempotency_key = %s THEN 0 ELSE 1 END
                 LIMIT 1
                """,
                (
                    scope.site_id,
                    event.idempotency_key,
                    event.audit_ref,
                    event.idempotency_key,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                replay = _event_from_row(row)
                if replay != event:
                    raise IdempotencyConflict("audit idempotency drift")
                return replay
            cursor.execute(
                """
                INSERT INTO email_gateway.audit_events (
                    site_id, audit_ref, actor_ref, event_type, subject_ref,
                    request_id, idempotency_key, payload_digest, occurred_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    event.site_id,
                    event.audit_ref,
                    event.actor_ref,
                    event.event_type,
                    event.subject_ref,
                    event.request_id,
                    event.idempotency_key,
                    event.payload_digest,
                    event.occurred_at,
                ),
            )
            cursor.execute(
                """
                SELECT audit_ref, site_id, actor_ref, event_type, subject_ref,
                       request_id, idempotency_key, payload_digest, occurred_at
                  FROM email_gateway.audit_events
                 WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, event.idempotency_key),
            )
            durable_row = cursor.fetchone()
            if durable_row is None:
                raise IdempotencyConflict("audit persistence conflict")
            durable = _event_from_row(durable_row)
            if durable != event:
                raise IdempotencyConflict("audit idempotency drift")
            return durable


def _event_from_row(row: tuple[object, ...]) -> AuditEvent:
    if len(row) != 9:
        raise IdempotencyConflict("invalid audit database row")
    return AuditEvent(
        audit_ref=str(row[0]),
        site_id=str(row[1]),
        actor_ref=str(row[2]),
        event_type=str(row[3]),
        subject_ref=str(row[4]),
        request_id=str(row[5]),
        idempotency_key=str(row[6]),
        payload_digest=str(row[7]),
        occurred_at=row[8],  # type: ignore[arg-type]
    )


__all__ = ["InMemoryAuditRepository", "PostgresAuditRepository"]
