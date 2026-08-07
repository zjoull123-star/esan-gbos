from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .connectors.serialization import canonical_observation_event_v11
from .models import (
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
    _require_aware,
    stable_ulid,
)
from .storage import Connection, Cursor

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_LEASE_SECONDS = 86_400
_MAX_ATTEMPTS = 100

_CONNECTOR_COLUMNS = """
    site_id, connector, connector_instance_id, status, registered_at, updated_at
"""
_DELIVERY_COLUMNS = """
    site_id, connector, connector_instance_id, delivery_id, exact_body_sha256,
    object_ref, byte_size, media_type, received_at, processing_status,
    attempt_count, correlation_id, last_attempt_at, last_error_code, created_at,
    updated_at
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
_JOB_COLUMNS = """
    site_id, job_id, connector, connector_instance_id, delivery_id, stage,
    status, attempt_count, max_attempts, idempotency_key, generation,
    lease_owner, lease_expires_at, lease_generation, next_retry_at,
    last_error_code, created_at, updated_at
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


class JobConflict(ValueError):
    """A processing job idempotency, lease, or state transition was rejected."""


class NormalizedBatchConflict(ValueError):
    """A provider event identifier was reused for a different normalized payload."""


class IngressExpired(ValueError):
    """An authenticated nonce or delivery fell outside its acceptance window."""


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
    object_ref: str
    byte_size: int
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
class ProcessingJobMetadata:
    site_id: str
    job_id: str
    connector: str
    connector_instance_id: str
    delivery_id: str
    stage: str
    status: str
    attempt_count: int
    max_attempts: int
    idempotency_key: str
    generation: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    next_retry_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedIngressMetadata:
    disposition: str

    def __post_init__(self) -> None:
        if self.disposition not in {"accepted", "duplicate"}:
            raise ValueError("invalid authenticated ingress disposition")


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


@dataclass(frozen=True, slots=True)
class PersistedNormalizedObservation:
    provider_event_id: str
    event_id: str
    outbox_id: str
    payload_sha256: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class PersistedNormalizedBatch:
    observations: tuple[PersistedNormalizedObservation, ...]


@dataclass(frozen=True, slots=True)
class _NormalizedCandidate:
    provider_event_id: str
    event_id: str
    outbox_id: str
    payload_sha256: str
    evidence_ids: tuple[str, ...]
    document: str
    item: ConnectorItem
    normalized: NormalizedObservationInput


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
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
    ) -> InboundDeliveryMetadata: ...

    def accept_and_enqueue_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        job_id: str,
        idempotency_key: str,
        max_attempts: int,
    ) -> tuple[InboundDeliveryMetadata, ProcessingJobMetadata]: ...

    def accept_authenticated_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
        job_id: str,
        max_attempts: int,
    ) -> AuthenticatedIngressMetadata: ...

    def get_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
    ) -> InboundDeliveryMetadata: ...

    def claim_processing_job(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata | None: ...

    def heartbeat_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata: ...

    def complete_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        provider_event_ids: tuple[str, ...],
    ) -> ProcessingJobMetadata: ...

    def persist_normalized_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch: ...

    def retry_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        next_retry_at: datetime,
        error_code: str,
    ) -> ProcessingJobMetadata: ...

    def quarantine_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        reason_code: str,
    ) -> ProcessingJobMetadata: ...

    def replay_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        job_id: str,
        idempotency_key: str,
        now: datetime,
        max_attempts: int,
    ) -> ProcessingJobMetadata: ...

    def set_connector_status(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        now: datetime,
    ) -> ConnectorInstanceMetadata: ...

    def update_checkpoint_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        now: datetime,
        last_success_at: datetime | None = None,
        error_code: str | None = None,
    ) -> ConnectorCheckpointMetadata: ...

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
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
    ) -> InboundDeliveryMetadata:
        _validate_delivery_input(
            scope,
            key,
            delivery_id=delivery_id,
            exact_body_sha256=exact_body_sha256,
            object_ref=object_ref,
            byte_size=byte_size,
            media_type=media_type,
            received_at=received_at,
            correlation_id=correlation_id,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            return self._accept_inbound_delivery(
                cursor,
                scope,
                key,
                delivery_id=delivery_id,
                exact_body_sha256=exact_body_sha256,
                object_ref=object_ref,
                byte_size=byte_size,
                media_type=media_type,
                received_at=received_at,
                correlation_id=correlation_id,
                queued=False,
            )

    def accept_and_enqueue_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        job_id: str,
        idempotency_key: str,
        max_attempts: int,
    ) -> tuple[InboundDeliveryMetadata, ProcessingJobMetadata]:
        _validate_delivery_input(
            scope,
            key,
            delivery_id=delivery_id,
            exact_body_sha256=exact_body_sha256,
            object_ref=object_ref,
            byte_size=byte_size,
            media_type=media_type,
            received_at=received_at,
            correlation_id=correlation_id,
        )
        _require_identifier(job_id, "job_id")
        _require_identifier(idempotency_key, "idempotency_key")
        _require_attempts(max_attempts)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            delivery = self._accept_inbound_delivery(
                cursor,
                scope,
                key,
                delivery_id=delivery_id,
                exact_body_sha256=exact_body_sha256,
                object_ref=object_ref,
                byte_size=byte_size,
                media_type=media_type,
                received_at=received_at,
                correlation_id=correlation_id,
                queued=True,
            )
            job = self._enqueue_processing_job(
                cursor,
                scope,
                key,
                delivery_id=delivery_id,
                job_id=job_id,
                idempotency_key=idempotency_key,
                generation=0,
                now=received_at,
                max_attempts=max_attempts,
            )
            return delivery, job

    def accept_authenticated_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
        job_id: str,
        max_attempts: int,
    ) -> AuthenticatedIngressMetadata:
        _validate_delivery_input(
            scope,
            key,
            delivery_id=delivery_id,
            exact_body_sha256=exact_body_sha256,
            object_ref=object_ref,
            byte_size=byte_size,
            media_type=media_type,
            received_at=received_at,
            correlation_id=correlation_id,
        )
        _require_identifier(nonce, "nonce", maximum=512)
        _require_aware(nonce_expires_at, "nonce_expires_at")
        _require_aware(now, "now")
        if nonce_expires_at <= now:
            raise IngressExpired("authenticated nonce is expired")
        _require_identifier(job_id, "job_id")
        _require_attempts(max_attempts)
        identity_ref = _authenticated_identity_ref(key)
        nonce_sha256 = hashlib.sha256(nonce.encode()).hexdigest()
        idempotency_key = _authenticated_job_idempotency_key(
            scope,
            key,
            nonce=nonce,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT replay_window_seconds
                FROM observer.connector_checkpoints
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                FOR SHARE
                """,
                (scope.site_id, key.connector, key.instance_id),
            )
            checkpoint_row = cursor.fetchone()
            if checkpoint_row is None:
                raise DeliveryConflict("connector checkpoint not found in site scope")
            replay_window_seconds = _as_int(
                checkpoint_row[0],
                "replay_window_seconds",
            )
            if received_at < now - timedelta(seconds=replay_window_seconds):
                raise IngressExpired("delivery is outside the configured replay window")

            cursor.execute(
                """
                INSERT INTO observer.persistent_nonces (
                    site_id, identity_ref, nonce_sha256, consumed_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (site_id, identity_ref, nonce_sha256) DO NOTHING
                RETURNING consumed_at, expires_at
                """,
                (
                    scope.site_id,
                    identity_ref,
                    nonce_sha256,
                    now,
                    nonce_expires_at,
                ),
            )
            nonce_inserted = cursor.fetchone() is not None
            existing_job: ProcessingJobMetadata | None = None
            if not nonce_inserted:
                cursor.execute(
                    """
                    SELECT consumed_at, expires_at
                    FROM observer.persistent_nonces
                    WHERE site_id = %s
                      AND identity_ref = %s
                      AND nonce_sha256 = %s
                    """,
                    (scope.site_id, identity_ref, nonce_sha256),
                )
                persisted_nonce = cursor.fetchone()
                if persisted_nonce is None:
                    raise NonceReplay("authenticated nonce replay state is unavailable")
                persisted_expiry = persisted_nonce[1]
                if not isinstance(persisted_expiry, datetime):
                    raise RuntimeError("invalid persisted nonce expiry")
                _require_aware(persisted_expiry, "persisted nonce expiry")
                if persisted_expiry <= now:
                    raise IngressExpired("persisted nonce is expired")
                cursor.execute(
                    f"""
                    SELECT {_JOB_COLUMNS}
                    FROM observer.processing_jobs
                    WHERE site_id = %s
                      AND idempotency_key = %s
                    """,
                    (scope.site_id, idempotency_key),
                )
                existing_job_row = cursor.fetchone()
                if existing_job_row is None:
                    raise NonceReplay("authenticated nonce has no matching delivery job")
                existing_job = _job_from_row(existing_job_row)
                if (
                    existing_job.connector != key.connector
                    or existing_job.connector_instance_id != key.instance_id
                    or existing_job.delivery_id != delivery_id
                ):
                    raise NonceReplay("authenticated nonce was reused for a different delivery")
            delivery = self._accept_inbound_delivery(
                cursor,
                scope,
                key,
                delivery_id=delivery_id,
                exact_body_sha256=exact_body_sha256,
                object_ref=object_ref,
                byte_size=byte_size,
                media_type=media_type,
                received_at=received_at,
                correlation_id=correlation_id,
                queued=True,
            )
            if nonce_inserted:
                job = self._enqueue_processing_job(
                    cursor,
                    scope,
                    key,
                    delivery_id=delivery_id,
                    job_id=job_id,
                    idempotency_key=idempotency_key,
                    generation=0,
                    now=now,
                    max_attempts=max_attempts,
                )
            else:
                if existing_job is None:
                    raise RuntimeError("authenticated duplicate job state is missing")
                job = existing_job
            if job.delivery_id != delivery.delivery_id:
                raise NonceReplay("authenticated nonce was reused for a different delivery")
            return AuthenticatedIngressMetadata(
                disposition="accepted" if nonce_inserted else "duplicate",
            )

    def get_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
    ) -> InboundDeliveryMetadata:
        _validate_scope_key(scope, key)
        _require_identifier(delivery_id, "delivery_id", maximum=512)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
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
                raise DeliveryConflict("delivery not found in connector scope")
            return _delivery_from_row(row)

    def persist_normalized_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch:
        _validate_normalized_batch(scope, key, job, items, normalized)
        candidates = tuple(
            _normalized_candidate(
                scope=scope,
                key=key,
                job=job,
                item=item,
                normalized=value,
            )
            for item, value in zip(items, normalized, strict=True)
        )
        if not candidates:
            return PersistedNormalizedBatch(observations=())

        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                SELECT {_qualified_columns(_DELIVERY_COLUMNS, "delivery")}
                FROM observer.processing_jobs AS job
                JOIN observer.inbound_deliveries AS delivery
                  ON delivery.site_id = job.site_id
                 AND delivery.connector = job.connector
                 AND delivery.connector_instance_id = job.connector_instance_id
                 AND delivery.delivery_id = job.delivery_id
                WHERE job.site_id = %s
                  AND job.job_id = %s
                  AND job.connector = %s
                  AND job.connector_instance_id = %s
                  AND job.delivery_id = %s
                  AND job.status = 'processing'
                  AND job.lease_owner = %s
                  AND job.attempt_count = %s
                  AND job.lease_generation = %s
                FOR UPDATE OF job, delivery
                """,
                (
                    scope.site_id,
                    job.job_id,
                    key.connector,
                    key.instance_id,
                    job.delivery_id,
                    job.lease_owner,
                    job.attempt_count,
                    job.lease_generation,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise JobConflict("normalized batch processing job lease is stale")
            delivery = _delivery_from_row(row)
            if delivery.processing_status != "processing":
                raise JobConflict("normalized batch delivery is not processing")
            for value in normalized:
                if all(artifact.reference != delivery.object_ref for artifact in value.evidence):
                    raise NormalizedBatchConflict(
                        "normalized payload omits the durable delivery evidence reference"
                    )

            provider_event_ids = tuple(item.provider_event_id for item in items)
            normalized_lock_keys = [
                hashlib.sha256(
                    "\x1f".join(
                        (
                            scope.site_id,
                            key.connector,
                            key.instance_id,
                            provider_event_id,
                        )
                    ).encode()
                ).hexdigest()
                for provider_event_id in provider_event_ids
            ]
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(hashtextextended(lock_key, 0))
                FROM UNNEST(%s::text[]) AS lock_key
                ORDER BY lock_key
                """,
                (normalized_lock_keys,),
            )
            cursor.fetchall()
            cursor.execute(
                """
                SELECT
                    event.provider_event_id,
                    event.event_id,
                    event.normalized_payload_sha256,
                    outbox.outbox_id
                FROM observer.observation_events AS event
                LEFT JOIN observer.context_publication_outbox AS outbox
                  ON outbox.site_id = event.site_id
                 AND outbox.observation_event_id = event.event_id
                WHERE event.site_id = %s
                  AND event.connector = %s
                  AND event.connector_instance_id = %s
                  AND event.provider_event_id = ANY(%s::text[])
                ORDER BY event.provider_event_id
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    list(provider_event_ids),
                ),
            )
            existing = {str(existing_row[0]): existing_row for existing_row in cursor.fetchall()}
            replayed: dict[str, PersistedNormalizedObservation] = {}
            for candidate in candidates:
                existing_row = existing.get(candidate.provider_event_id)
                if existing_row is None:
                    continue
                if (
                    str(existing_row[1]) != candidate.event_id
                    or str(existing_row[2]) != candidate.payload_sha256
                    or str(existing_row[3]) != candidate.outbox_id
                ):
                    raise NormalizedBatchConflict("provider event normalized payload conflict")
                replayed[candidate.provider_event_id] = PersistedNormalizedObservation(
                    provider_event_id=candidate.provider_event_id,
                    event_id=candidate.event_id,
                    outbox_id=candidate.outbox_id,
                    payload_sha256=candidate.payload_sha256,
                    replayed=True,
                )

            new_candidates = tuple(
                candidate for candidate in candidates if candidate.provider_event_id not in replayed
            )
            raw_object_id: str | None = None
            retention_until = job.created_at + timedelta(days=30)
            if new_candidates:
                raw_object_id = stable_ulid(
                    "normalized-raw-object",
                    scope.site_id,
                    delivery.exact_body_sha256,
                )
                cursor.execute(
                    """
                    INSERT INTO observer.raw_objects (
                        site_id, object_id, object_ref, sha256, media_type,
                        byte_size, retention_class, retention_until, created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        'R1-operational', %s, %s
                    )
                    ON CONFLICT (site_id, sha256)
                    DO NOTHING
                    RETURNING object_id, object_ref
                    """,
                    (
                        scope.site_id,
                        raw_object_id,
                        delivery.object_ref,
                        delivery.exact_body_sha256,
                        delivery.media_type,
                        delivery.byte_size,
                        retention_until,
                        job.created_at,
                    ),
                )
                object_row = cursor.fetchone()
                if object_row is None:
                    cursor.execute(
                        """
                        SELECT object_id, object_ref
                        FROM observer.raw_objects
                        WHERE site_id = %s AND sha256 = %s
                        """,
                        (scope.site_id, delivery.exact_body_sha256),
                    )
                    object_row = cursor.fetchone()
                if object_row is None:
                    raise RuntimeError("normalized source object is missing")
                raw_object_id = str(object_row[0])
                if str(object_row[1]) != delivery.object_ref:
                    raise NormalizedBatchConflict(
                        "source object digest conflicts with another object reference"
                    )

            persisted = dict(replayed)
            for candidate in new_candidates:
                assert raw_object_id is not None
                cursor.execute(
                    """
                    INSERT INTO observer.observation_events (
                        site_id, event_id, job_id, processing_job_id,
                        raw_object_id, delivery_id, provider_event_id,
                        connector, connector_instance_id, channel,
                        processing_purpose, consent_basis, data_classification,
                        retention_class, retention_until, correlation_id,
                        occurred_at, ingested_at, document, raw_sha256,
                        occurred_minute, team_ref, party_ref,
                        normalized_payload_sha256
                    ) VALUES (
                        %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s,
                        date_trunc('minute', %s::timestamptz), NULL, NULL, %s
                    )
                    """,
                    (
                        scope.site_id,
                        candidate.event_id,
                        job.job_id,
                        raw_object_id,
                        job.delivery_id,
                        candidate.provider_event_id,
                        key.connector,
                        key.instance_id,
                        candidate.normalized.channel,
                        scope.processing_purpose,
                        candidate.normalized.consent_basis,
                        candidate.normalized.data_classification,
                        candidate.normalized.retention_class,
                        retention_until,
                        candidate.normalized.correlation_id,
                        candidate.item.occurred_at,
                        job.created_at,
                        candidate.document,
                        delivery.exact_body_sha256,
                        candidate.item.occurred_at,
                        candidate.payload_sha256,
                    ),
                )
                for index, participant in enumerate(candidate.normalized.participants):
                    participant_id = stable_ulid(
                        "normalized-participant",
                        scope.site_id,
                        candidate.event_id,
                        str(index),
                        participant.identity_ref,
                    )
                    cursor.execute(
                        """
                        INSERT INTO observer.participants (
                            site_id, event_id, participant_id, role,
                            identity_ref, display_name
                        ) VALUES (%s, %s, %s, %s, %s, NULL)
                        """,
                        (
                            scope.site_id,
                            candidate.event_id,
                            participant_id,
                            participant.role,
                            participant.identity_ref,
                        ),
                    )
                for index, artifact in enumerate(candidate.normalized.evidence):
                    evidence_id = candidate.evidence_ids[index]
                    cursor.execute(
                        """
                        INSERT INTO observer.evidence_refs (
                            site_id, evidence_id, event_id, raw_object_id,
                            raw_sha256, media_type, locator, created_at,
                            content_object_ref
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                        """,
                        (
                            scope.site_id,
                            evidence_id,
                            candidate.event_id,
                            raw_object_id,
                            delivery.exact_body_sha256,
                            artifact.media_type,
                            json.dumps(
                                {
                                    "locator": artifact.locator,
                                    "role": artifact.role,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            job.created_at,
                            artifact.reference,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO observer.event_evidence (
                            site_id, event_id, evidence_id, evidence_ordinal
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            scope.site_id,
                            candidate.event_id,
                            evidence_id,
                            index,
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO observer.context_publication_outbox (
                        site_id, outbox_id, observation_event_id,
                        idempotency_key, payload_digest, status, attempt_count,
                        max_attempts, next_retry_at, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'queued', 0, %s, %s, %s, %s
                    )
                    """,
                    (
                        scope.site_id,
                        candidate.outbox_id,
                        candidate.event_id,
                        f"context-normalized:{candidate.event_id}",
                        candidate.payload_sha256,
                        job.max_attempts,
                        job.created_at,
                        job.created_at,
                        job.created_at,
                    ),
                )
                persisted[candidate.provider_event_id] = PersistedNormalizedObservation(
                    provider_event_id=candidate.provider_event_id,
                    event_id=candidate.event_id,
                    outbox_id=candidate.outbox_id,
                    payload_sha256=candidate.payload_sha256,
                    replayed=False,
                )
            return PersistedNormalizedBatch(
                observations=tuple(persisted[item.provider_event_id] for item in items)
            )

    def claim_processing_job(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata | None:
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
                    SELECT job.site_id, job.job_id
                    FROM observer.processing_jobs AS job
                    WHERE job.site_id = %s
                      AND job.delivery_id IS NOT NULL
                      AND job.idempotency_key IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM observer.connector_instances AS instance
                          WHERE instance.site_id = job.site_id
                            AND instance.connector = job.connector
                            AND instance.connector_instance_id =
                                job.connector_instance_id
                            AND instance.status <> 'paused'
                      )
                      AND job.attempt_count < job.max_attempts
                      AND (
                          (
                              job.status IN ('queued', 'retry_wait')
                              AND (
                                  job.next_retry_at IS NULL
                                  OR job.next_retry_at <= %s
                              )
                          )
                          OR (
                              job.status = 'processing'
                              AND job.lease_expires_at <= %s
                          )
                      )
                    ORDER BY
                        COALESCE(job.next_retry_at, job.created_at),
                        job.created_at,
                        job.job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                ),
                claimed AS (
                    UPDATE observer.processing_jobs AS job
                    SET status = 'processing',
                        attempt_count = job.attempt_count + 1,
                        lease_owner = %s,
                        lease_expires_at = %s,
                        lease_generation = job.lease_generation + 1,
                        last_error_code = NULL,
                        updated_at = %s
                    FROM candidate
                    WHERE job.site_id = candidate.site_id
                      AND job.job_id = candidate.job_id
                    RETURNING {_qualified_columns(_JOB_COLUMNS, "job")}
                ),
                delivery_state AS (
                    UPDATE observer.inbound_deliveries AS delivery
                    SET processing_status = 'processing',
                        attempt_count = claimed.attempt_count,
                        last_attempt_at = %s,
                        last_error_code = NULL,
                        updated_at = %s
                    FROM claimed
                    WHERE delivery.site_id = claimed.site_id
                      AND delivery.connector = claimed.connector
                      AND delivery.connector_instance_id =
                          claimed.connector_instance_id
                      AND delivery.delivery_id = claimed.delivery_id
                      AND delivery.processing_status IN (
                          'received', 'authenticated', 'queued', 'processing'
                      )
                    RETURNING delivery.delivery_id
                )
                SELECT {_qualified_columns(_JOB_COLUMNS, "claimed")}
                FROM claimed
                """,
                (
                    scope.site_id,
                    now,
                    now,
                    worker_id,
                    lease_expires_at,
                    now,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            return None if row is None else _job_from_row(row)

    def heartbeat_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata:
        _validate_job_lease_input(
            scope,
            job_id=job_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_lease_generation=expected_lease_generation,
            now=now,
        )
        _require_lease_seconds(lease_seconds)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.processing_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE site_id = %s
                  AND job_id = %s
                  AND status = 'processing'
                  AND lease_owner = %s
                  AND attempt_count = %s
                  AND lease_generation = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    lease_expires_at,
                    now,
                    scope.site_id,
                    job_id,
                    worker_id,
                    expected_attempt,
                    expected_lease_generation,
                    now,
                ),
            )
            return self._leased_job_result(cursor, "job heartbeat lease transition rejected")

    def complete_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        provider_event_ids: tuple[str, ...],
    ) -> ProcessingJobMetadata:
        _validate_job_lease_input(
            scope,
            job_id=job_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_lease_generation=expected_lease_generation,
            now=now,
        )
        _validate_provider_event_ids(provider_event_ids, allow_empty=True)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            if provider_event_ids:
                cursor.execute(
                    """
                    INSERT INTO observer.inbound_delivery_events (
                        site_id, connector, connector_instance_id,
                        delivery_id, provider_event_id, linked_at
                    )
                    SELECT
                        job.site_id,
                        job.connector,
                        job.connector_instance_id,
                        job.delivery_id,
                        provider_event_id,
                        %s
                    FROM observer.processing_jobs AS job
                    CROSS JOIN UNNEST(%s::text[]) AS provider_event_id
                    WHERE job.site_id = %s
                      AND job.job_id = %s
                      AND job.status = 'processing'
                      AND job.lease_owner = %s
                      AND job.attempt_count = %s
                      AND job.lease_generation = %s
                      AND job.lease_expires_at > %s
                    ON CONFLICT (
                        site_id, connector, connector_instance_id,
                        provider_event_id
                    ) DO NOTHING
                    """,
                    (
                        now,
                        list(provider_event_ids),
                        scope.site_id,
                        job_id,
                        worker_id,
                        expected_attempt,
                        expected_lease_generation,
                        now,
                    ),
                )
            cursor.execute(
                """
                UPDATE observer.inbound_deliveries AS delivery
                SET processing_status = 'succeeded',
                    last_error_code = NULL,
                    updated_at = %s
                FROM observer.processing_jobs AS job
                WHERE job.site_id = %s
                  AND job.job_id = %s
                  AND job.status = 'processing'
                  AND job.lease_owner = %s
                  AND job.attempt_count = %s
                  AND job.lease_generation = %s
                  AND job.lease_expires_at > %s
                  AND delivery.site_id = job.site_id
                  AND delivery.connector = job.connector
                  AND delivery.connector_instance_id =
                      job.connector_instance_id
                  AND delivery.delivery_id = job.delivery_id
                  AND delivery.processing_status = 'processing'
                """,
                (
                    now,
                    scope.site_id,
                    job_id,
                    worker_id,
                    expected_attempt,
                    expected_lease_generation,
                    now,
                ),
            )
            cursor.execute(
                f"""
                UPDATE observer.processing_jobs
                SET status = 'succeeded',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    updated_at = %s
                WHERE site_id = %s
                  AND job_id = %s
                  AND status = 'processing'
                  AND lease_owner = %s
                  AND attempt_count = %s
                  AND lease_generation = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    now,
                    scope.site_id,
                    job_id,
                    worker_id,
                    expected_attempt,
                    expected_lease_generation,
                    now,
                ),
            )
            return self._leased_job_result(cursor, "job completion lease transition rejected")

    def retry_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        next_retry_at: datetime,
        error_code: str,
    ) -> ProcessingJobMetadata:
        _validate_job_lease_input(
            scope,
            job_id=job_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_lease_generation=expected_lease_generation,
            now=now,
        )
        _require_aware(next_retry_at, "next_retry_at")
        if next_retry_at <= now:
            raise ValueError("next_retry_at must be in the future")
        _require_identifier(error_code, "error_code", maximum=80)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.processing_jobs
                SET status = CASE
                        WHEN attempt_count >= max_attempts THEN 'dead_letter'
                        ELSE 'retry_wait'
                    END,
                    next_retry_at = CASE
                        WHEN attempt_count >= max_attempts THEN NULL
                        ELSE %s
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND job_id = %s
                  AND status = 'processing'
                  AND lease_owner = %s
                  AND attempt_count = %s
                  AND lease_generation = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    next_retry_at,
                    error_code,
                    now,
                    scope.site_id,
                    job_id,
                    worker_id,
                    expected_attempt,
                    expected_lease_generation,
                    now,
                ),
            )
            metadata = self._leased_job_result(cursor, "job retry lease transition rejected")
            if metadata.status == "dead_letter":
                self._record_dead_letter(cursor, metadata, error_code=error_code, now=now)
                self._mark_delivery_terminal(
                    cursor,
                    metadata,
                    status="failed",
                    error_code=error_code,
                    now=now,
                )
            else:
                self._record_delivery_error(cursor, metadata, error_code=error_code, now=now)
            return metadata

    def quarantine_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        reason_code: str,
    ) -> ProcessingJobMetadata:
        _validate_job_lease_input(
            scope,
            job_id=job_id,
            worker_id=worker_id,
            expected_attempt=expected_attempt,
            expected_lease_generation=expected_lease_generation,
            now=now,
        )
        _require_identifier(reason_code, "reason_code", maximum=80)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.processing_jobs
                SET status = 'quarantined',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    next_retry_at = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND job_id = %s
                  AND status = 'processing'
                  AND lease_owner = %s
                  AND attempt_count = %s
                  AND lease_generation = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    reason_code,
                    now,
                    scope.site_id,
                    job_id,
                    worker_id,
                    expected_attempt,
                    expected_lease_generation,
                    now,
                ),
            )
            metadata = self._leased_job_result(cursor, "job quarantine lease transition rejected")
            quarantine_id = hashlib.sha256(
                f"{scope.site_id}\x1f{job_id}\x1f{expected_lease_generation}".encode()
            ).hexdigest()
            cursor.execute(
                """
                INSERT INTO observer.local_pilot_quarantine (
                    site_id, quarantine_id, job_id, delivery_id,
                    reason_code, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (site_id, quarantine_id) DO NOTHING
                """,
                (
                    scope.site_id,
                    quarantine_id,
                    metadata.job_id,
                    metadata.delivery_id,
                    reason_code,
                    now,
                ),
            )
            self._mark_delivery_terminal(
                cursor,
                metadata,
                status="quarantined",
                error_code=reason_code,
                now=now,
            )
            return metadata

    def replay_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        job_id: str,
        idempotency_key: str,
        now: datetime,
        max_attempts: int,
    ) -> ProcessingJobMetadata:
        _validate_scope_key(scope, key)
        _require_identifier(delivery_id, "delivery_id", maximum=512)
        _require_identifier(job_id, "job_id")
        _require_identifier(idempotency_key, "idempotency_key")
        _require_aware(now, "now")
        _require_attempts(max_attempts)
        oldest_eligible_at = now - timedelta(days=30)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            replay_lock_key = hashlib.sha256(
                "\x1f".join(
                    (
                        scope.site_id,
                        key.connector,
                        key.instance_id,
                        idempotency_key,
                    )
                ).encode()
            ).hexdigest()
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (replay_lock_key,),
            )
            cursor.fetchone()
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM observer.processing_jobs
                WHERE site_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (
                    scope.site_id,
                    idempotency_key,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                metadata = _job_from_row(row)
                if (
                    metadata.delivery_id != delivery_id
                    or metadata.connector != key.connector
                    or metadata.connector_instance_id != key.instance_id
                ):
                    raise JobConflict("replay idempotency key conflicts with another delivery")
                return metadata

            cursor.execute(
                """
                UPDATE observer.inbound_deliveries
                SET processing_status = 'queued',
                    last_error_code = NULL,
                    updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND delivery_id = %s
                  AND processing_status = 'failed'
                  AND received_at >= %s
                  AND received_at <= %s
                  AND object_ref IS NOT NULL
                  AND byte_size IS NOT NULL
                RETURNING
                    delivery_id, exact_body_sha256, object_ref, byte_size
                """,
                (
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    delivery_id,
                    oldest_eligible_at,
                    now,
                ),
            )
            delivery_row = cursor.fetchone()
            if delivery_row is None:
                raise DeliveryConflict(
                    "replay requires an eligible failed delivery in connector scope"
                )
            cursor.execute(
                """
                SELECT COALESCE(MAX(generation), -1) + 1
                FROM observer.processing_jobs
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                  AND delivery_id = %s
                """,
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    delivery_id,
                ),
            )
            generation_row = cursor.fetchone()
            if generation_row is None or not isinstance(generation_row[0], int):
                raise RuntimeError("replay generation query returned no value")
            return self._enqueue_processing_job(
                cursor,
                scope,
                key,
                delivery_id=delivery_id,
                job_id=job_id,
                idempotency_key=idempotency_key,
                generation=generation_row[0],
                now=now,
                max_attempts=max_attempts,
            )

    def set_connector_status(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        now: datetime,
    ) -> ConnectorInstanceMetadata:
        _validate_scope_key(scope, key)
        _require_connector_status(status)
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.connector_instances
                SET status = %s, updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                RETURNING {_CONNECTOR_COLUMNS}
                """,
                (status, now, scope.site_id, key.connector, key.instance_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict("connector status transition rejected")
            return _connector_from_row(row)

    def update_checkpoint_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        now: datetime,
        last_success_at: datetime | None = None,
        error_code: str | None = None,
    ) -> ConnectorCheckpointMetadata:
        _validate_scope_key(scope, key)
        _require_connector_status(status)
        _require_aware(now, "now")
        if last_success_at is not None:
            _require_aware(last_success_at, "last_success_at")
        if error_code is not None:
            _require_identifier(error_code, "error_code", maximum=80)
        if status == "healthy" and error_code is not None:
            raise ValueError("healthy checkpoint cannot include an error code")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"""
                UPDATE observer.connector_checkpoints
                SET status = %s,
                    last_success_at = COALESCE(%s, last_success_at),
                    last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND connector = %s
                  AND connector_instance_id = %s
                RETURNING {_CHECKPOINT_COLUMNS}
                """,
                (
                    status,
                    last_success_at,
                    error_code,
                    now,
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise CheckpointConflict("checkpoint health transition rejected")
            return _checkpoint_from_row(row)

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
                        JOIN observer.observation_events AS event
                          ON event.site_id = outbox.site_id
                         AND event.event_id = outbox.observation_event_id
                        WHERE outbox.site_id = instance.site_id
                          AND event.connector = instance.connector
                          AND event.connector_instance_id =
                              instance.connector_instance_id
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

    def _accept_inbound_delivery(
        self,
        cursor: Cursor,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        queued: bool,
    ) -> InboundDeliveryMetadata:
        processing_status = "queued" if queued else "received"
        cursor.execute(
            f"""
            INSERT INTO observer.inbound_deliveries (
                site_id, connector, connector_instance_id, delivery_id,
                exact_body_sha256, object_ref, byte_size, media_type,
                received_at, processing_status, attempt_count, correlation_id,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s
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
                object_ref,
                byte_size,
                media_type,
                received_at,
                processing_status,
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
        actual = (
            delivery.exact_body_sha256,
            delivery.object_ref,
            delivery.byte_size,
            delivery.media_type,
        )
        expected = (exact_body_sha256, object_ref, byte_size, media_type)
        if actual != expected:
            raise DeliveryConflict(
                "delivery identifier was reused with a different body or content metadata"
            )
        return delivery

    def _enqueue_processing_job(
        self,
        cursor: Cursor,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        job_id: str,
        idempotency_key: str,
        generation: int,
        now: datetime,
        max_attempts: int,
    ) -> ProcessingJobMetadata:
        cursor.execute(
            f"""
            INSERT INTO observer.processing_jobs (
                site_id, job_id, connector, connector_instance_id, delivery_id,
                stage, status, attempt_count, max_attempts, idempotency_key,
                generation, lease_generation, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, 'normalize', 'queued', 0, %s, %s, %s,
                0, %s, %s
            )
            ON CONFLICT (site_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            DO NOTHING
            RETURNING {_JOB_COLUMNS}
            """,
            (
                scope.site_id,
                job_id,
                key.connector,
                key.instance_id,
                delivery_id,
                max_attempts,
                idempotency_key,
                generation,
                now,
                now,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM observer.processing_jobs
                WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                raise JobConflict("processing job idempotency conflict")
        metadata = _job_from_row(row)
        if (
            metadata.delivery_id != delivery_id
            or metadata.connector != key.connector
            or metadata.connector_instance_id != key.instance_id
            or metadata.generation != generation
        ):
            raise JobConflict("processing job idempotency key was reused")
        return metadata

    @staticmethod
    def _leased_job_result(cursor: Cursor, message: str) -> ProcessingJobMetadata:
        row = cursor.fetchone()
        if row is None:
            raise JobConflict(message)
        return _job_from_row(row)

    @staticmethod
    def _record_delivery_error(
        cursor: Cursor,
        job: ProcessingJobMetadata,
        *,
        error_code: str,
        now: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE observer.inbound_deliveries
            SET last_error_code = %s, updated_at = %s
            WHERE site_id = %s
              AND connector = %s
              AND connector_instance_id = %s
              AND delivery_id = %s
              AND processing_status = 'processing'
            """,
            (
                error_code,
                now,
                job.site_id,
                job.connector,
                job.connector_instance_id,
                job.delivery_id,
            ),
        )

    @staticmethod
    def _mark_delivery_terminal(
        cursor: Cursor,
        job: ProcessingJobMetadata,
        *,
        status: str,
        error_code: str,
        now: datetime,
    ) -> None:
        cursor.execute(
            """
            UPDATE observer.inbound_deliveries
            SET processing_status = %s, last_error_code = %s, updated_at = %s
            WHERE site_id = %s
              AND connector = %s
              AND connector_instance_id = %s
              AND delivery_id = %s
              AND processing_status = 'processing'
            """,
            (
                status,
                error_code,
                now,
                job.site_id,
                job.connector,
                job.connector_instance_id,
                job.delivery_id,
            ),
        )

    @staticmethod
    def _record_dead_letter(
        cursor: Cursor,
        job: ProcessingJobMetadata,
        *,
        error_code: str,
        now: datetime,
    ) -> None:
        dead_letter_id = hashlib.sha256(
            f"{job.site_id}\x1f{job.job_id}\x1f{job.lease_generation}".encode()
        ).hexdigest()
        cursor.execute(
            """
            INSERT INTO observer.local_pilot_dead_letter (
                site_id, dead_letter_id, job_id, reason_code,
                attempt_count, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (site_id, dead_letter_id) DO NOTHING
            """,
            (
                job.site_id,
                dead_letter_id,
                job.job_id,
                error_code,
                job.attempt_count,
                now,
            ),
        )

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
        object_ref=str(row[5]),
        byte_size=_as_int(row[6], "byte_size"),
        media_type=str(row[7]),
        received_at=row[8],  # type: ignore[arg-type]
        processing_status=str(row[9]),
        attempt_count=_as_int(row[10], "attempt_count"),
        correlation_id=str(row[11]),
        last_attempt_at=row[12],  # type: ignore[arg-type]
        last_error_code=None if row[13] is None else str(row[13]),
        created_at=row[14],  # type: ignore[arg-type]
        updated_at=row[15],  # type: ignore[arg-type]
    )


def _job_from_row(row: tuple[object, ...]) -> ProcessingJobMetadata:
    delivery_id = row[4]
    idempotency_key = row[9]
    if delivery_id is None or idempotency_key is None:
        raise RuntimeError("ingestion processing job is missing durable identity")
    return ProcessingJobMetadata(
        site_id=str(row[0]),
        job_id=str(row[1]),
        connector=str(row[2]),
        connector_instance_id=str(row[3]),
        delivery_id=str(delivery_id),
        stage=str(row[5]),
        status=str(row[6]),
        attempt_count=_as_int(row[7], "attempt_count"),
        max_attempts=_as_int(row[8], "max_attempts"),
        idempotency_key=str(idempotency_key),
        generation=_as_int(row[10], "generation"),
        lease_owner=None if row[11] is None else str(row[11]),
        lease_expires_at=row[12],  # type: ignore[arg-type]
        lease_generation=_as_int(row[13], "lease_generation"),
        next_retry_at=row[14],  # type: ignore[arg-type]
        last_error_code=None if row[15] is None else str(row[15]),
        created_at=row[16],  # type: ignore[arg-type]
        updated_at=row[17],  # type: ignore[arg-type]
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


def _validate_normalized_batch(
    scope: TenantScope,
    key: ConnectorKey,
    job: ProcessingJobMetadata,
    items: tuple[ConnectorItem, ...],
    normalized: tuple[NormalizedObservationInput, ...],
) -> None:
    _validate_scope_key(scope, key)
    if key.connector == "manual_import":
        raise ValueError("normalized connector jobs cannot use manual_import")
    if (
        not isinstance(job, ProcessingJobMetadata)
        or job.site_id != scope.site_id
        or job.connector != key.connector
        or job.connector_instance_id != key.instance_id
    ):
        raise ValueError("processing job scope does not match normalized batch")
    if (
        job.status != "processing"
        or job.lease_owner is None
        or job.lease_expires_at is None
        or job.delivery_id is None
    ):
        raise ValueError("processing job is not actively leased")
    if (
        not isinstance(items, tuple)
        or not isinstance(normalized, tuple)
        or len(items) != len(normalized)
        or len(items) > 1_000
    ):
        raise ValueError("normalized batch shape is invalid")
    if not all(isinstance(item, ConnectorItem) for item in items) or not all(
        isinstance(value, NormalizedObservationInput) for value in normalized
    ):
        raise TypeError("normalized batch contains invalid values")
    provider_event_ids = tuple(item.provider_event_id for item in items)
    if len(provider_event_ids) != len(set(provider_event_ids)):
        raise ValueError("normalized batch provider event identifiers must be unique")
    for value in normalized:
        if (
            value.consent_basis != "pilot_deferred_review"
            or value.retention_class != "R1-operational"
            or value.data_classification != "Restricted"
            or value.original_language != "und"
        ):
            raise ValueError("normalized batch policy fields are invalid")
        if not 1 <= len(value.participants) <= 100:
            raise ValueError("normalized batch participant count is invalid")
        for participant in value.participants:
            if (
                not participant.identity_ref.startswith("unresolved:delivery:")
                or participant.display_name is not None
            ):
                raise ValueError("normalized participants must remain unresolved")
        if not 1 <= len(value.evidence) <= 1_000:
            raise ValueError("normalized batch evidence count is invalid")
        for artifact in value.evidence:
            if artifact.content is not None or artifact.reference is None:
                raise ValueError("normalized evidence references are required")
            _require_identifier(
                artifact.reference,
                "normalized evidence reference",
                maximum=512,
            )
            if (
                artifact.locator
                not in {
                    "delivery",
                    "decrypted-message",
                    "message",
                }
                and re.fullmatch(r"attachment:[1-9][0-9]{0,3}", artifact.locator) is None
            ):
                raise ValueError("normalized evidence locator is invalid")
            if artifact.role not in {"source", "attachment"}:
                raise ValueError("normalized evidence role is invalid")


def _normalized_candidate(
    *,
    scope: TenantScope,
    key: ConnectorKey,
    job: ProcessingJobMetadata,
    item: ConnectorItem,
    normalized: NormalizedObservationInput,
) -> _NormalizedCandidate:
    event_id = stable_ulid(
        "normalized-observation-event",
        scope.site_id,
        key.connector,
        key.instance_id,
        item.provider_event_id,
    )
    evidence_ids = tuple(
        stable_ulid(
            "normalized-evidence",
            scope.site_id,
            event_id,
            str(index),
            str(artifact.reference),
        )
        for index, artifact in enumerate(normalized.evidence)
    )
    document_value = canonical_observation_event_v11(
        scope=scope,
        connector_key=key,
        item=item,
        normalized=normalized,
        event_id=event_id,
        evidence_ids=evidence_ids,
        ingested_at=job.created_at,
    )
    document = json.dumps(
        document_value,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_sha256 = hashlib.sha256(document.encode()).hexdigest()
    outbox_id = stable_ulid(
        "normalized-context-outbox",
        scope.site_id,
        event_id,
    )
    return _NormalizedCandidate(
        provider_event_id=item.provider_event_id,
        event_id=event_id,
        outbox_id=outbox_id,
        payload_sha256=payload_sha256,
        evidence_ids=evidence_ids,
        document=document,
        item=item,
        normalized=normalized,
    )


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


def _validate_delivery_input(
    scope: TenantScope,
    key: ConnectorKey,
    *,
    delivery_id: str,
    exact_body_sha256: str,
    object_ref: str,
    byte_size: int,
    media_type: str,
    received_at: datetime,
    correlation_id: str,
) -> None:
    _validate_scope_key(scope, key)
    _require_identifier(delivery_id, "delivery_id", maximum=512)
    if not _SHA256.fullmatch(exact_body_sha256):
        raise ValueError("exact_body_sha256 must be lowercase hexadecimal sha256")
    _require_identifier(object_ref, "object_ref", maximum=512)
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
        raise ValueError("byte_size must be a non-negative integer")
    _require_identifier(media_type, "media_type", maximum=255)
    _require_aware(received_at, "received_at")
    _require_identifier(correlation_id, "correlation_id")


def _validate_job_lease_input(
    scope: TenantScope,
    *,
    job_id: str,
    worker_id: str,
    expected_attempt: int,
    expected_lease_generation: int,
    now: datetime,
) -> None:
    _validate_scope(scope)
    _require_identifier(job_id, "job_id")
    _require_identifier(worker_id, "worker_id")
    if not isinstance(expected_attempt, int) or expected_attempt < 1:
        raise ValueError("expected_attempt must be a positive integer")
    if not isinstance(expected_lease_generation, int) or expected_lease_generation < 1:
        raise ValueError("expected_lease_generation must be a positive integer")
    _require_aware(now, "now")


def _validate_provider_event_ids(
    provider_event_ids: tuple[str, ...],
    *,
    allow_empty: bool,
) -> None:
    if not isinstance(provider_event_ids, tuple):
        raise TypeError("provider_event_ids must be a tuple")
    if not allow_empty and not provider_event_ids:
        raise ValueError("provider_event_ids must not be empty")
    if len(provider_event_ids) != len(set(provider_event_ids)):
        raise ValueError("provider_event_ids must be unique")
    for provider_event_id in provider_event_ids:
        _require_identifier(provider_event_id, "provider_event_id", maximum=512)


def _require_connector_status(status: str) -> None:
    if status not in {"healthy", "paused", "degraded", "failed"}:
        raise ValueError("invalid connector status")


def _authenticated_identity_ref(key: ConnectorKey) -> str:
    instance_digest = hashlib.sha256(key.instance_id.encode()).hexdigest()
    return f"connector:{key.connector}:{instance_digest}"


def _authenticated_job_idempotency_key(
    scope: TenantScope,
    key: ConnectorKey,
    *,
    nonce: str,
) -> str:
    material = "\x1f".join(
        (
            scope.site_id,
            key.connector,
            key.instance_id,
            nonce,
        )
    ).encode()
    return f"authenticated:{hashlib.sha256(material).hexdigest()}"
