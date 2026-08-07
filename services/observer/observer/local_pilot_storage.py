from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .models import ConnectorKey, TenantScope, _require_aware
from .storage import Connection, Cursor

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_LEASE_SECONDS = 86_400
_MAX_ATTEMPTS = 100

_CONNECTOR_COLUMNS = """
    site_id, connector, connector_instance_id, status, registered_at, updated_at
"""
_DELIVERY_COLUMNS = """
    site_id, connector, connector_instance_id, delivery_id, exact_body_sha256,
    media_type, received_at, processing_status, attempt_count, correlation_id,
    last_attempt_at, last_error_code, created_at, updated_at
"""
_CHECKPOINT_COLUMNS = """
    site_id, connector, connector_instance_id, checkpoint_id, cursor_value,
    checkpoint_version, replay_window_seconds, lease_owner, lease_expires_at,
    last_success_at, last_error_code, status, updated_at
"""
_OUTBOX_COLUMNS = """
    site_id, outbox_id, observation_event_id, idempotency_key, payload_digest,
    status, attempt_count, max_attempts, next_retry_at, lease_owner,
    lease_expires_at, last_error_code, created_at, updated_at
"""


class DeliveryConflict(ValueError):
    """A delivery identifier was reused with different exact-body metadata."""


class CheckpointConflict(ValueError):
    """A checkpoint compare-and-swap or version transition was rejected."""


class LeaseConflict(ValueError):
    """A connector lease is held by another live owner."""


class NonceReplay(ValueError):
    """A persistent request nonce was already consumed."""


class OutboxConflict(ValueError):
    """A context outbox idempotency or lease transition was rejected."""


@dataclass(frozen=True, slots=True)
class ConnectorInstanceMetadata:
    site_id: str
    connector: str
    connector_instance_id: str
    status: str
    registered_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InboundDeliveryMetadata:
    site_id: str
    connector: str
    connector_instance_id: str
    delivery_id: str
    exact_body_sha256: str
    media_type: str
    received_at: datetime
    processing_status: str
    attempt_count: int
    correlation_id: str
    last_attempt_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectorCheckpointMetadata:
    site_id: str
    connector: str
    connector_instance_id: str
    checkpoint_id: str
    cursor: str | None
    checkpoint_version: int
    replay_window_seconds: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    status: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NonceReceipt:
    site_id: str
    identity_ref: str
    nonce_sha256: str
    consumed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ContextOutboxMetadata:
    site_id: str
    outbox_id: str
    observation_event_id: str
    idempotency_key: str
    payload_digest: str
    status: str
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectorHealthMetadata:
    site_id: str
    connector: str
    connector_instance_id: str
    status: str
    checkpoint_version: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    pending_jobs: int
    pending_outbox: int


class LocalPilotStorage(Protocol):
    """Provider-neutral durable storage boundary for local connector pilots."""

    def register_connector_instance(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        now: datetime,
        replay_window_seconds: int = 0,
    ) -> ConnectorInstanceMetadata: ...

    def get_connector_instance(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> ConnectorInstanceMetadata | None: ...

    def list_connector_instances(
        self,
        scope: TenantScope,
    ) -> tuple[ConnectorInstanceMetadata, ...]: ...

    def accept_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
    ) -> InboundDeliveryMetadata: ...

    def link_delivery_events(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        provider_event_ids: tuple[str, ...],
        linked_at: datetime,
    ) -> None: ...

    def acquire_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ConnectorCheckpointMetadata: ...

    def renew_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ConnectorCheckpointMetadata: ...

    def release_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> ConnectorCheckpointMetadata: ...

    def compare_and_swap_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        next_version: int,
        now: datetime,
    ) -> ConnectorCheckpointMetadata: ...

    def consume_nonce(
        self,
        scope: TenantScope,
        *,
        identity_ref: str,
        nonce: str,
        now: datetime,
        expires_at: datetime,
    ) -> NonceReceipt: ...

    def enqueue_context_outbox(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        observation_event_id: str,
        idempotency_key: str,
        payload_digest: str,
        now: datetime,
        max_attempts: int,
    ) -> ContextOutboxMetadata: ...

    def claim_context_outbox(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ContextOutboxMetadata | None: ...

    def mark_context_outbox(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        worker_id: str,
        now: datetime,
        published: bool,
        error_code: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> ContextOutboxMetadata: ...

    def get_connector_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> ConnectorHealthMetadata | None: ...


class PostgresLocalPilotStorage:
    """PostgreSQL implementation of the local-pilot metadata store."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def register_connector_instance(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        now: datetime,
        replay_window_seconds: int = 0,
    ) -> ConnectorInstanceMetadata:
        _validate_scope_key(scope, key)
        _require_aware(now, "now")
        if not 0 <= replay_window_seconds <= 31_536_000:
            raise ValueError("replay_window_seconds is outside the valid range")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                INSERT INTO observer.connector_instances (
                    site_id, connector, connector_instance_id, status,
                    registered_at, updated_at
                ) VALUES (%s, %s, %s, 'healthy', %s, %s)
                ON CONFLICT (site_id, connector, connector_instance_id)
                DO UPDATE SET updated_at = EXCLUDED.updated_at
                RETURNING {_CONNECTOR_COLUMNS}
                """,
                (scope.site_id, key.connector, key.instance_id, now, now),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("connector instance metadata insert returned no row")
            cursor.execute(
                """
                INSERT INTO observer.connector_checkpoints (
                    site_id, connector, connector_instance_id, checkpoint_id,
                    checkpoint_version, replay_window_seconds, status, updated_at
                ) VALUES (%s, %s, %s, %s, 0, %s, 'healthy', %s)
                ON CONFLICT (site_id, connector, connector_instance_id) DO NOTHING
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    f"{key.connector}:{key.instance_id}",
                    replay_window_seconds,
                    now,
                ),
            )
            return _connector_from_row(row)

    def get_connector_instance(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> ConnectorInstanceMetadata | None:
        _validate_scope_key(scope, key)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_CONNECTOR_COLUMNS}
                FROM observer.connector_instances
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                """,
                (scope.site_id, key.connector, key.instance_id),
            )
            row = cursor.fetchone()
            return None if row is None else _connector_from_row(row)

    def list_connector_instances(
        self,
        scope: TenantScope,
    ) -> tuple[ConnectorInstanceMetadata, ...]:
        _validate_scope(scope)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_CONNECTOR_COLUMNS}
                FROM observer.connector_instances
                WHERE site_id = %s
                ORDER BY connector, connector_instance_id
                """,
                (scope.site_id,),
            )
            return tuple(_connector_from_row(row) for row in cursor.fetchall())

    def accept_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
    ) -> InboundDeliveryMetadata:
        _validate_scope_key(scope, key)
        _require_identifier(delivery_id, "delivery_id", maximum=512)
        if not _SHA256.fullmatch(exact_body_sha256):
            raise ValueError("exact_body_sha256 must be lowercase hexadecimal sha256")
        _require_identifier(media_type, "media_type", maximum=255)
        _require_aware(received_at, "received_at")
        _require_identifier(correlation_id, "correlation_id")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                INSERT INTO observer.inbound_deliveries (
                    site_id, connector, connector_instance_id, delivery_id,
                    exact_body_sha256, media_type, received_at, processing_status,
                    attempt_count, correlation_id, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 'received', 0, %s, %s, %s
                )
                ON CONFLICT (
                    site_id, connector, connector_instance_id, delivery_id
                ) DO NOTHING
                RETURNING {_DELIVERY_COLUMNS}
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    delivery_id,
                    exact_body_sha256,
                    media_type,
                    received_at,
                    correlation_id,
                    received_at,
                    received_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""
                    SELECT {_DELIVERY_COLUMNS}
                    FROM observer.inbound_deliveries
                    WHERE site_id = %s
                      AND connector = %s
                      AND connector_instance_id = %s
                      AND delivery_id = %s
                    """,
                    (scope.site_id, key.connector, key.instance_id, delivery_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("inbound delivery metadata is missing")
            delivery = _delivery_from_row(row)
            if delivery.exact_body_sha256 != exact_body_sha256:
                raise DeliveryConflict("delivery identifier was reused with a different body")
            return delivery

    def link_delivery_events(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        provider_event_ids: tuple[str, ...],
        linked_at: datetime,
    ) -> None:
        _validate_scope_key(scope, key)
        _require_identifier(delivery_id, "delivery_id", maximum=512)
        _require_aware(linked_at, "linked_at")
        if (
            not isinstance(provider_event_ids, tuple)
            or not provider_event_ids
            or len(provider_event_ids) != len(set(provider_event_ids))
        ):
            raise ValueError("provider_event_ids must be a non-empty unique tuple")
        for provider_event_id in provider_event_ids:
            _require_identifier(provider_event_id, "provider_event_id", maximum=512)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                INSERT INTO observer.inbound_delivery_events (
                    site_id, connector, connector_instance_id,
                    delivery_id, provider_event_id, linked_at
                )
                SELECT %s, %s, %s, %s, provider_event_id, %s
                FROM UNNEST(%s::text[]) AS provider_event_id
                ON CONFLICT (
                    site_id, connector, connector_instance_id, provider_event_id
                ) DO NOTHING
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    delivery_id,
                    linked_at,
                    provider_event_ids,
                ),
            )

    def acquire_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ConnectorCheckpointMetadata:
        return self._change_lease(
            scope,
            key,
            owner=owner,
            now=now,
            lease_seconds=lease_seconds,
            operation="acquire",
        )

    def renew_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ConnectorCheckpointMetadata:
        return self._change_lease(
            scope,
            key,
            owner=owner,
            now=now,
            lease_seconds=lease_seconds,
            operation="renew",
        )

    def release_connector_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> ConnectorCheckpointMetadata:
        _validate_scope_key(scope, key)
        _require_identifier(owner, "owner")
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.connector_checkpoints
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND lease_owner = %s
                RETURNING {_CHECKPOINT_COLUMNS}
                """,
                (now, scope.site_id, key.connector, key.instance_id, owner),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict("connector lease release rejected")
            return _checkpoint_from_row(row)

    def compare_and_swap_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        next_version: int,
        now: datetime,
    ) -> ConnectorCheckpointMetadata:
        _validate_scope_key(scope, key)
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if next_version != expected_version + 1:
            raise ValueError("next_version must increment expected_version by one")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
            raise ValueError("cursor must be an opaque string up to 4096 characters")
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as db_cursor:
            self._set_site(db_cursor, scope)
            db_cursor.execute(
                f"""
                UPDATE observer.connector_checkpoints
                SET cursor_value = %s, checkpoint_version = %s, updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND checkpoint_version = %s
                RETURNING {_CHECKPOINT_COLUMNS}
                """,
                (
                    cursor,
                    next_version,
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    expected_version,
                ),
            )
            row = db_cursor.fetchone()
            if row is None:
                raise CheckpointConflict("stale checkpoint version")
            return _checkpoint_from_row(row)

    def consume_nonce(
        self,
        scope: TenantScope,
        *,
        identity_ref: str,
        nonce: str,
        now: datetime,
        expires_at: datetime,
    ) -> NonceReceipt:
        _validate_scope(scope)
        _require_identifier(identity_ref, "identity_ref")
        _require_identifier(nonce, "nonce", maximum=512)
        _require_aware(now, "now")
        _require_aware(expires_at, "expires_at")
        if expires_at <= now:
            raise ValueError("nonce expiry must be in the future")
        nonce_sha256 = hashlib.sha256(nonce.encode()).hexdigest()
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                INSERT INTO observer.persistent_nonces (
                    site_id, identity_ref, nonce_sha256, consumed_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (site_id, identity_ref, nonce_sha256) DO NOTHING
                RETURNING consumed_at, expires_at
                """,
                (scope.site_id, identity_ref, nonce_sha256, now, expires_at),
            )
            row = cursor.fetchone()
            if row is None:
                raise NonceReplay("nonce replay rejected")
            return NonceReceipt(
                site_id=scope.site_id,
                identity_ref=identity_ref,
                nonce_sha256=nonce_sha256,
                consumed_at=row[0],
                expires_at=row[1],
            )

    def enqueue_context_outbox(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        observation_event_id: str,
        idempotency_key: str,
        payload_digest: str,
        now: datetime,
        max_attempts: int,
    ) -> ContextOutboxMetadata:
        _validate_scope(scope)
        _require_identifier(outbox_id, "outbox_id")
        _require_identifier(observation_event_id, "observation_event_id")
        _require_identifier(idempotency_key, "idempotency_key")
        if not _SHA256.fullmatch(payload_digest):
            raise ValueError("payload_digest must be lowercase hexadecimal sha256")
        _require_aware(now, "now")
        _require_attempts(max_attempts)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                INSERT INTO observer.context_publication_outbox (
                    site_id, outbox_id, observation_event_id, idempotency_key,
                    payload_digest, status, attempt_count, max_attempts,
                    next_retry_at, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s
                )
                ON CONFLICT (site_id, idempotency_key) DO NOTHING
                RETURNING {_OUTBOX_COLUMNS}
                """,
                (
                    scope.site_id,
                    outbox_id,
                    observation_event_id,
                    idempotency_key,
                    payload_digest,
                    max_attempts,
                    now,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""
                    SELECT {_OUTBOX_COLUMNS}
                    FROM observer.context_publication_outbox
                    WHERE site_id = %s AND idempotency_key = %s
                    """,
                    (scope.site_id, idempotency_key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise OutboxConflict("outbox identifier conflicts with another request")
            metadata = _outbox_from_row(row)
            if (
                metadata.payload_digest != payload_digest
                or metadata.observation_event_id != observation_event_id
            ):
                raise OutboxConflict("outbox idempotency key was reused for a different payload")
            return metadata

    def claim_context_outbox(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ContextOutboxMetadata | None:
        _validate_scope(scope)
        _require_identifier(worker_id, "worker_id")
        _require_aware(now, "now")
        _require_lease_seconds(lease_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT site_id, outbox_id
                    FROM observer.context_publication_outbox
                    WHERE site_id = %s
                      AND attempt_count < max_attempts
                      AND (
                          (
                              status IN ('queued', 'retry_wait')
                              AND next_retry_at <= %s
                          )
                          OR (
                              status = 'leased'
                              AND lease_expires_at <= %s
                          )
                      )
                    ORDER BY next_retry_at, created_at, outbox_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE observer.context_publication_outbox AS outbox
                SET status = 'leased',
                    attempt_count = outbox.attempt_count + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    updated_at = %s
                FROM candidate
                WHERE outbox.site_id = candidate.site_id
                  AND outbox.outbox_id = candidate.outbox_id
                RETURNING {_qualified_columns(_OUTBOX_COLUMNS, "outbox")}
                """,
                (
                    scope.site_id,
                    now,
                    now,
                    worker_id,
                    lease_expires_at,
                    now,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else _outbox_from_row(row)

    def mark_context_outbox(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        worker_id: str,
        now: datetime,
        published: bool,
        error_code: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> ContextOutboxMetadata:
        _validate_scope(scope)
        _require_identifier(outbox_id, "outbox_id")
        _require_identifier(worker_id, "worker_id")
        _require_aware(now, "now")
        if published:
            if error_code is not None or next_retry_at is not None:
                raise ValueError("published outbox rows cannot include retry metadata")
        else:
            _require_identifier(error_code, "error_code", maximum=80)
            if next_retry_at is None:
                raise ValueError("next_retry_at is required for a failed publication")
            _require_aware(next_retry_at, "next_retry_at")
            if next_retry_at <= now:
                raise ValueError("next_retry_at must be in the future")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.context_publication_outbox
                SET status = CASE
                        WHEN %s THEN 'published'
                        WHEN attempt_count >= max_attempts THEN 'dead_letter'
                        ELSE 'retry_wait'
                    END,
                    next_retry_at = CASE
                        WHEN %s THEN next_retry_at
                        ELSE %s
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND outbox_id = %s
                  AND status = 'leased'
                  AND lease_owner = %s
                  AND lease_expires_at > %s
                RETURNING {_OUTBOX_COLUMNS}
                """,
                (
                    published,
                    published,
                    next_retry_at,
                    error_code,
                    now,
                    scope.site_id,
                    outbox_id,
                    worker_id,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise OutboxConflict("outbox lease transition rejected")
            metadata = _outbox_from_row(row)
            if metadata.status == "dead_letter":
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
                        metadata.attempt_count,
                        now,
                    ),
                )
            return metadata

    def get_connector_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> ConnectorHealthMetadata | None:
        _validate_scope_key(scope, key)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT
                    instance.site_id,
                    instance.connector,
                    instance.connector_instance_id,
                    instance.status,
                    checkpoint.checkpoint_version,
                    checkpoint.lease_owner,
                    checkpoint.lease_expires_at,
                    checkpoint.last_success_at,
                    checkpoint.last_error_code,
                    (
                        SELECT count(*)
                        FROM observer.processing_jobs AS job
                        WHERE job.site_id = instance.site_id
                          AND job.connector = instance.connector
                          AND job.connector_instance_id =
                              instance.connector_instance_id
                          AND job.status IN ('queued', 'processing', 'retry_wait')
                    ) AS pending_jobs,
                    (
                        SELECT count(*)
                        FROM observer.context_publication_outbox AS outbox
                        WHERE outbox.site_id = instance.site_id
                          AND outbox.status IN ('queued', 'leased', 'retry_wait')
                    ) AS pending_outbox
                FROM observer.connector_instances AS instance
                JOIN observer.connector_checkpoints AS checkpoint
                  ON checkpoint.site_id = instance.site_id
                 AND checkpoint.connector = instance.connector
                 AND checkpoint.connector_instance_id =
                     instance.connector_instance_id
                WHERE instance.site_id = %s
                  AND instance.connector = %s
                  AND instance.connector_instance_id = %s
                """,
                (scope.site_id, key.connector, key.instance_id),
            )
            row = cursor.fetchone()
            return None if row is None else _health_from_row(row)

    def _change_lease(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
        operation: str,
    ) -> ConnectorCheckpointMetadata:
        _validate_scope_key(scope, key)
        _require_identifier(owner, "owner")
        _require_aware(now, "now")
        _require_lease_seconds(lease_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        if operation == "acquire":
            lease_predicate = "(lease_owner IS NULL OR lease_expires_at <= %s OR lease_owner = %s)"
            predicate_params: tuple[object, ...] = (now, owner)
        elif operation == "renew":
            lease_predicate = "lease_owner = %s AND lease_expires_at > %s"
            predicate_params = (owner, now)
        else:
            raise RuntimeError("unknown lease operation")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.connector_checkpoints
                SET lease_owner = %s, lease_expires_at = %s, updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND {lease_predicate}
                RETURNING {_CHECKPOINT_COLUMNS}
                """,
                (
                    owner,
                    lease_expires_at,
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    *predicate_params,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict(f"connector lease {operation} rejected")
            return _checkpoint_from_row(row)

    @staticmethod
    def _set_site(cursor: Cursor, scope: TenantScope) -> None:
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (scope.site_id,),
        )


def _connector_from_row(row: tuple[object, ...]) -> ConnectorInstanceMetadata:
    return ConnectorInstanceMetadata(
        site_id=str(row[0]),
        connector=str(row[1]),
        connector_instance_id=str(row[2]),
        status=str(row[3]),
        registered_at=row[4],  # type: ignore[arg-type]
        updated_at=row[5],  # type: ignore[arg-type]
    )


def _delivery_from_row(row: tuple[object, ...]) -> InboundDeliveryMetadata:
    return InboundDeliveryMetadata(
        site_id=str(row[0]),
        connector=str(row[1]),
        connector_instance_id=str(row[2]),
        delivery_id=str(row[3]),
        exact_body_sha256=str(row[4]),
        media_type=str(row[5]),
        received_at=row[6],  # type: ignore[arg-type]
        processing_status=str(row[7]),
        attempt_count=_as_int(row[8], "attempt_count"),
        correlation_id=str(row[9]),
        last_attempt_at=row[10],  # type: ignore[arg-type]
        last_error_code=None if row[11] is None else str(row[11]),
        created_at=row[12],  # type: ignore[arg-type]
        updated_at=row[13],  # type: ignore[arg-type]
    )


def _checkpoint_from_row(row: tuple[object, ...]) -> ConnectorCheckpointMetadata:
    return ConnectorCheckpointMetadata(
        site_id=str(row[0]),
        connector=str(row[1]),
        connector_instance_id=str(row[2]),
        checkpoint_id=str(row[3]),
        cursor=None if row[4] is None else str(row[4]),
        checkpoint_version=_as_int(row[5], "checkpoint_version"),
        replay_window_seconds=_as_int(row[6], "replay_window_seconds"),
        lease_owner=None if row[7] is None else str(row[7]),
        lease_expires_at=row[8],  # type: ignore[arg-type]
        last_success_at=row[9],  # type: ignore[arg-type]
        last_error_code=None if row[10] is None else str(row[10]),
        status=str(row[11]),
        updated_at=row[12],  # type: ignore[arg-type]
    )


def _outbox_from_row(row: tuple[object, ...]) -> ContextOutboxMetadata:
    return ContextOutboxMetadata(
        site_id=str(row[0]),
        outbox_id=str(row[1]),
        observation_event_id=str(row[2]),
        idempotency_key=str(row[3]),
        payload_digest=str(row[4]),
        status=str(row[5]),
        attempt_count=_as_int(row[6], "attempt_count"),
        max_attempts=_as_int(row[7], "max_attempts"),
        next_retry_at=row[8],  # type: ignore[arg-type]
        lease_owner=None if row[9] is None else str(row[9]),
        lease_expires_at=row[10],  # type: ignore[arg-type]
        last_error_code=None if row[11] is None else str(row[11]),
        created_at=row[12],  # type: ignore[arg-type]
        updated_at=row[13],  # type: ignore[arg-type]
    )


def _health_from_row(row: tuple[object, ...]) -> ConnectorHealthMetadata:
    return ConnectorHealthMetadata(
        site_id=str(row[0]),
        connector=str(row[1]),
        connector_instance_id=str(row[2]),
        status=str(row[3]),
        checkpoint_version=_as_int(row[4], "checkpoint_version"),
        lease_owner=None if row[5] is None else str(row[5]),
        lease_expires_at=row[6],  # type: ignore[arg-type]
        last_success_at=row[7],  # type: ignore[arg-type]
        last_error_code=None if row[8] is None else str(row[8]),
        pending_jobs=_as_int(row[9], "pending_jobs"),
        pending_outbox=_as_int(row[10], "pending_outbox"),
    )


def _qualified_columns(columns: str, alias: str) -> str:
    return ", ".join(
        f"{alias}.{column.strip()}"
        for column in columns.replace("\n", " ").split(",")
        if column.strip()
    )


def _as_int(value: object, field_name: str) -> int:
    if not isinstance(value, int):
        raise RuntimeError(f"invalid persisted {field_name}")
    return value


def _validate_scope(scope: TenantScope) -> None:
    if not isinstance(scope, TenantScope):
        raise TypeError("scope must be TenantScope")


def _validate_scope_key(scope: TenantScope, key: ConnectorKey) -> None:
    _validate_scope(scope)
    if not isinstance(key, ConnectorKey):
        raise TypeError("key must be ConnectorKey")


def _require_identifier(value: str | None, field_name: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {field_name}")


def _require_lease_seconds(lease_seconds: int) -> None:
    if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be a positive bounded integer")


def _require_attempts(max_attempts: int) -> None:
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= _MAX_ATTEMPTS:
        raise ValueError("max_attempts must be a positive bounded integer")
