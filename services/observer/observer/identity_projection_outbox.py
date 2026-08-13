"""Closed email identity projections with a durable generation-fenced relay."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from .models import _require_aware
from .storage import Connection

_PURPOSES = frozenset(
    {
        "business_operations",
        "observation_processing",
        "entity_resolution",
        "customer_service",
        "sales_follow_up",
        "procurement_coordination",
        "product_sample_management",
        "risk_review",
        "metric_reporting",
        "audit_compliance",
    }
)
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PAYLOAD_FIELDS = frozenset(
    {
        "site_id",
        "processing_purpose",
        "opaque_address_ref",
        "external_identity_ref",
        "external_identity_revision",
        "identity_type",
        "team_ref",
        "status",
        "projection_receipt",
        "observed_at",
    }
)
_RECEIPT_FIELDS = _PAYLOAD_FIELDS - {"projection_receipt"}


class IdentityProjectionRelayConflict(ValueError):
    """The immutable payload or delivery state was reused inconsistently."""


class IdentityProjectionRelayFenceConflict(IdentityProjectionRelayConflict):
    """The caller no longer owns the exact outbox lease generation."""


class _Resolution(Protocol):
    @property
    def site_id(self) -> str: ...

    @property
    def identity_provider(self) -> str: ...

    @property
    def external_subject_ref(self) -> str: ...

    @property
    def mapping_ref(self) -> str: ...

    @property
    def mapping_revision(self) -> int: ...

    @property
    def team_ref(self) -> str: ...

    @property
    def target_type(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def recorded_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _SeedResolution:
    site_id: str
    identity_provider: str
    external_subject_ref: str
    mapping_ref: str
    mapping_revision: int
    team_ref: str
    target_type: str
    status: str
    recorded_at: datetime


def projection_receipt(value: dict[str, object]) -> str:
    if set(value) != _RECEIPT_FIELDS:
        raise ValueError("invalid identity projection receipt fields")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_identity_projection_payload(
    resolution: _Resolution,
    processing_purpose: str,
) -> dict[str, object]:
    """Freeze only contract-approved email identity fields; target refs never cross."""

    if resolution.identity_provider != "email":
        raise ValueError("identity projection relay accepts email resolutions only")
    if processing_purpose not in _PURPOSES:
        raise ValueError("invalid identity projection purpose")
    observed_at = resolution.recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    fields: dict[str, object] = {
        "site_id": resolution.site_id,
        "processing_purpose": processing_purpose,
        "opaque_address_ref": resolution.external_subject_ref,
        "external_identity_ref": resolution.mapping_ref,
        "external_identity_revision": resolution.mapping_revision,
        "identity_type": resolution.target_type,
        "team_ref": resolution.team_ref,
        "status": resolution.status,
        "observed_at": observed_at,
    }
    return {**fields, "projection_receipt": projection_receipt(fields)}


def payload_digest(payload: dict[str, object]) -> str:
    if set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("invalid identity projection payload")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class IdentityProjectionRelayClaim:
    site_id: str
    processing_purpose: str
    projection_receipt: str
    payload: dict[str, object]
    payload_digest: str
    attempt: int
    max_attempts: int
    generation: int
    lease_owner: str
    lease_expires_at: datetime

    @property
    def item_ref(self) -> str:
        return self.projection_receipt

    @property
    def request_id(self) -> str:
        return f"identity-projection:{self.projection_receipt.removeprefix('sha256:')}"

    @property
    def fence_token(self) -> str:
        return f"generation:{self.generation}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(site_id={self.site_id!r}, "
            f"processing_purpose={self.processing_purpose!r}, "
            f"projection_receipt={self.projection_receipt!r}, attempt={self.attempt}, "
            f"max_attempts={self.max_attempts}, generation={self.generation}, "
            f"lease_owner={self.lease_owner!r}, payload=<redacted>)"
        )


@dataclass(slots=True)
class _Record:
    payload: dict[str, object]
    digest: str
    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: datetime
    generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    delivered_at: datetime | None = None


class InMemoryIdentityProjectionOutbox:
    """Deterministic relay repository for restart, retry, and fence tests."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], _Record] = {}

    def append(self, payload: dict[str, object], *, queued_at: datetime) -> bool:
        _require_aware(queued_at, "queued_at")
        digest = payload_digest(payload)
        receipt = payload.get("projection_receipt")
        if not isinstance(receipt, str) or not hmac.compare_digest(
            receipt,
            projection_receipt({key: payload[key] for key in _RECEIPT_FIELDS}),
        ):
            raise ValueError("identity projection receipt mismatch")
        key = (str(payload["site_id"]), str(payload["processing_purpose"]), receipt)
        existing = self._records.get(key)
        if existing is not None:
            if existing.payload != payload or existing.digest != digest:
                raise IdentityProjectionRelayConflict("identity projection payload conflict")
            return False
        self._records[key] = _Record(
            payload=dict(payload),
            digest=digest,
            status="queued",
            attempt=0,
            max_attempts=5,
            next_attempt_at=queued_at,
        )
        return True

    def claim(
        self,
        *,
        site_id: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityProjectionRelayClaim | None:
        _validate_relay_call(site_id, worker_id, now, lease_duration)
        eligible = [
            record
            for (record_site, _purpose, _receipt), record in self._records.items()
            if record_site == site_id
            and record.attempt < record.max_attempts
            and (
                (record.status in {"queued", "retry"} and record.next_attempt_at <= now)
                or (
                    record.status == "leased"
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now
                )
            )
        ]
        if not eligible:
            return None
        record = min(
            eligible,
            key=lambda item: (item.next_attempt_at, str(item.payload["projection_receipt"])),
        )
        record.status = "leased"
        record.attempt += 1
        record.generation += 1
        record.lease_owner = worker_id
        record.lease_expires_at = now + lease_duration
        record.last_error_code = None
        return _claim(record)

    def heartbeat(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        _validate_relay_call(claim.site_id, worker_id, now, lease_duration)
        record = self._owned(claim, worker_id=worker_id, now=now)
        record.lease_expires_at = now + lease_duration

    def acknowledge(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        receipt_ref: str,
    ) -> None:
        _require_aware(now, "now")
        record = self._owned(claim, worker_id=worker_id, now=now)
        if not hmac.compare_digest(receipt_ref, claim.projection_receipt):
            raise IdentityProjectionRelayConflict("identity projection receipt rejected")
        record.status = "delivered"
        record.lease_owner = None
        record.lease_expires_at = None
        record.delivered_at = now

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
        _require_aware(retry_at, "retry_at")
        record = self._owned(claim, worker_id=worker_id, now=now)
        if retry_at < now or not error_code or len(error_code) > 128:
            raise ValueError("invalid identity projection failure")
        dead = not retryable or record.attempt >= record.max_attempts
        record.status = "dead_letter" if dead else "retry"
        record.next_attempt_at = retry_at
        record.last_error_code = error_code
        record.lease_owner = None
        record.lease_expires_at = None
        return record.status

    def _owned(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
    ) -> _Record:
        _require_aware(now, "now")
        key = (claim.site_id, claim.processing_purpose, claim.projection_receipt)
        record = self._records.get(key)
        if (
            record is None
            or record.status != "leased"
            or record.generation != claim.generation
            or record.lease_owner != worker_id
            or record.lease_expires_at is None
            or record.lease_expires_at <= now
        ):
            raise IdentityProjectionRelayFenceConflict("identity projection lease lost")
        return record


def _claim(record: _Record) -> IdentityProjectionRelayClaim:
    assert record.lease_owner is not None and record.lease_expires_at is not None
    return IdentityProjectionRelayClaim(
        site_id=str(record.payload["site_id"]),
        processing_purpose=str(record.payload["processing_purpose"]),
        projection_receipt=str(record.payload["projection_receipt"]),
        payload=dict(record.payload),
        payload_digest=record.digest,
        attempt=record.attempt,
        max_attempts=record.max_attempts,
        generation=record.generation,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
    )


def _validate_relay_call(
    site_id: str,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> None:
    _require_aware(now, "now")
    if (
        not site_id
        or not worker_id
        or worker_id != worker_id.strip()
        or not timedelta(0) < lease_duration <= timedelta(minutes=5)
    ):
        raise ValueError("invalid identity projection relay call")


def enqueue_resolution_projections(cursor: Any, resolution: _Resolution) -> None:
    """Enqueue current team purposes inside the caller's resolution transaction."""

    if resolution.identity_provider != "email":
        return
    _lock_seed_scope(cursor, site_id=resolution.site_id, team_ref=resolution.team_ref)
    cursor.execute(
        """
        SELECT DISTINCT latest.business_purpose
          FROM (
                SELECT DISTINCT ON (mailbox_id)
                       mailbox_id, business_purpose, team_ref, inbound_enabled,
                       projection_revision
                  FROM observer.email_connector_config_projections
                 WHERE site_id = %s
                 ORDER BY mailbox_id, projection_revision DESC
               ) AS latest
         WHERE latest.team_ref = %s AND latest.inbound_enabled
         ORDER BY latest.business_purpose
        """,
        (resolution.site_id, resolution.team_ref),
    )
    for row in cursor.fetchall():
        _insert_payload(cursor, build_identity_projection_payload(resolution, str(row[0])))


def seed_current_resolutions_for_config(
    cursor: Any,
    *,
    site_id: str,
    team_ref: str,
    processing_purpose: str,
) -> None:
    """Seed only each email subject's latest resolution when a config arrives later."""

    _lock_seed_scope(cursor, site_id=site_id, team_ref=team_ref)
    cursor.execute(
        """
        SELECT DISTINCT ON (external_subject_ref)
               site_id, identity_provider, external_subject_ref, mapping_ref,
               mapping_revision, team_ref, target_type, status, recorded_at
          FROM observer.participant_identity_resolutions
         WHERE site_id = %s AND identity_provider = 'email' AND team_ref = %s
         ORDER BY external_subject_ref, mapping_revision DESC
        """,
        (site_id, team_ref),
    )
    for row in cursor.fetchall():
        resolution = _SeedResolution(*row)
        _insert_payload(cursor, build_identity_projection_payload(resolution, processing_purpose))


def _lock_seed_scope(cursor: Any, *, site_id: str, team_ref: str) -> None:
    """Serialize config and resolution arrival so neither commit can miss the other."""

    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"identity-projection-seed:{site_id}:{team_ref}",),
    )


def _insert_payload(cursor: Any, payload: dict[str, object]) -> None:
    digest = payload_digest(payload)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    cursor.execute(
        """
        INSERT INTO observer.identity_projection_outbox (
            site_id, processing_purpose, opaque_address_ref,
            external_identity_revision, projection_receipt, payload,
            payload_digest, relay_status, attempt_count, max_attempts,
            next_attempt_at, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s,
                  'queued', 0, 5, %s, %s, %s)
        ON CONFLICT (
            site_id, processing_purpose, opaque_address_ref,
            external_identity_revision
        ) DO NOTHING
        RETURNING projection_receipt, payload_digest, payload
        """,
        (
            payload["site_id"],
            payload["processing_purpose"],
            payload["opaque_address_ref"],
            payload["external_identity_revision"],
            payload["projection_receipt"],
            encoded,
            digest,
            cast(str, payload["observed_at"]),
            cast(str, payload["observed_at"]),
            cast(str, payload["observed_at"]),
        ),
    )
    durable = cursor.fetchone()
    if durable is None:
        cursor.execute(
            """
            SELECT projection_receipt, payload_digest, payload
              FROM observer.identity_projection_outbox
             WHERE site_id = %s AND processing_purpose = %s
               AND opaque_address_ref = %s
               AND external_identity_revision = %s
            """,
            (
                payload["site_id"],
                payload["processing_purpose"],
                payload["opaque_address_ref"],
                payload["external_identity_revision"],
            ),
        )
        durable = cursor.fetchone()
    if (
        durable is None
        or len(durable) != 3
        or not isinstance(durable[2], dict)
        or not hmac.compare_digest(str(durable[0]), str(payload["projection_receipt"]))
        or not hmac.compare_digest(str(durable[1]), digest)
        or durable[2] != payload
    ):
        raise IdentityProjectionRelayConflict("identity projection outbox conflict")


class PostgresIdentityProjectionOutbox:
    """Least-privilege relay access to the Observer-owned frozen outbox."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresIdentityProjectionOutbox(connection=<redacted>)"

    def claim(
        self,
        *,
        site_id: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> IdentityProjectionRelayClaim | None:
        _validate_relay_call(site_id, worker_id, now, lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, site_id)
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT site_id, processing_purpose, opaque_address_ref,
                           external_identity_revision
                      FROM observer.identity_projection_outbox
                     WHERE site_id = %s
                       AND attempt_count < max_attempts
                       AND (
                            (relay_status IN ('queued', 'retry') AND next_attempt_at <= %s)
                            OR (relay_status = 'leased' AND lease_expires_at <= %s)
                       )
                     ORDER BY next_attempt_at, projection_receipt
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE observer.identity_projection_outbox AS item
                   SET relay_status = 'leased', attempt_count = item.attempt_count + 1,
                       lease_owner = %s, lease_expires_at = %s,
                       lease_generation = item.lease_generation + 1,
                       last_error_code = NULL, updated_at = %s
                  FROM candidate
                 WHERE item.site_id = candidate.site_id
                   AND item.processing_purpose = candidate.processing_purpose
                   AND item.opaque_address_ref = candidate.opaque_address_ref
                   AND item.external_identity_revision = candidate.external_identity_revision
                RETURNING item.site_id, item.processing_purpose,
                          item.projection_receipt, item.payload, item.payload_digest,
                          item.attempt_count, item.max_attempts, item.lease_generation,
                          item.lease_owner, item.lease_expires_at
                """,
                (site_id, now, now, worker_id, now + lease_duration, now),
            )
            row = cursor.fetchone()
            return None if row is None else _claim_from_row(row)

    def heartbeat(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        _validate_relay_call(claim.site_id, worker_id, now, lease_duration)
        self._fenced_update(
            claim,
            worker_id=worker_id,
            now=now,
            set_sql="lease_expires_at = %s, updated_at = %s",
            set_params=(now + lease_duration, now),
        )

    def acknowledge(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        receipt_ref: str,
    ) -> None:
        if not hmac.compare_digest(receipt_ref, claim.projection_receipt):
            raise IdentityProjectionRelayConflict("identity projection receipt rejected")
        self._fenced_update(
            claim,
            worker_id=worker_id,
            now=now,
            set_sql=(
                "relay_status = 'delivered', lease_owner = NULL, lease_expires_at = NULL, "
                "delivery_receipt = %s, delivered_at = %s, updated_at = %s"
            ),
            set_params=(receipt_ref, now, now),
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
        _require_aware(retry_at, "retry_at")
        if retry_at < now or not error_code or len(error_code) > 128:
            raise ValueError("invalid identity projection failure")
        state = "retry" if retryable and claim.attempt < claim.max_attempts else "dead_letter"
        self._fenced_update(
            claim,
            worker_id=worker_id,
            now=now,
            set_sql=(
                "relay_status = %s, lease_owner = NULL, lease_expires_at = NULL, "
                "next_attempt_at = %s, last_error_code = %s, updated_at = %s"
            ),
            set_params=(state, retry_at, error_code, now),
        )
        return state

    def _fenced_update(
        self,
        claim: IdentityProjectionRelayClaim,
        *,
        worker_id: str,
        now: datetime,
        set_sql: str,
        set_params: tuple[object, ...],
    ) -> None:
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, claim.site_id)
            cursor.execute(
                f"""
                UPDATE observer.identity_projection_outbox
                   SET {set_sql}
                 WHERE site_id = %s AND processing_purpose = %s
                   AND projection_receipt = %s AND relay_status = 'leased'
                   AND lease_owner = %s AND lease_generation = %s
                   AND lease_expires_at > %s
                RETURNING projection_receipt
                """,
                (
                    *set_params,
                    claim.site_id,
                    claim.processing_purpose,
                    claim.projection_receipt,
                    worker_id,
                    claim.generation,
                    now,
                ),
            )
            if cursor.fetchone() is None:
                raise IdentityProjectionRelayFenceConflict("identity projection lease lost")


def _set_site(cursor: Any, site_id: str) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))


def _claim_from_row(row: tuple[Any, ...]) -> IdentityProjectionRelayClaim:
    if len(row) != 10 or not isinstance(row[3], dict):
        raise IdentityProjectionRelayConflict("identity projection outbox row rejected")
    payload = cast(dict[str, object], row[3])
    digest = payload_digest(payload)
    if not hmac.compare_digest(str(row[4]), digest):
        raise IdentityProjectionRelayConflict("identity projection outbox digest drift")
    return IdentityProjectionRelayClaim(
        site_id=str(row[0]),
        processing_purpose=str(row[1]),
        projection_receipt=str(row[2]),
        payload=dict(payload),
        payload_digest=digest,
        attempt=int(row[5]),
        max_attempts=int(row[6]),
        generation=int(row[7]),
        lease_owner=str(row[8]),
        lease_expires_at=row[9],
    )


__all__ = [
    "IdentityProjectionRelayClaim",
    "IdentityProjectionRelayConflict",
    "IdentityProjectionRelayFenceConflict",
    "InMemoryIdentityProjectionOutbox",
    "PostgresIdentityProjectionOutbox",
    "build_identity_projection_payload",
    "enqueue_resolution_projections",
    "payload_digest",
    "projection_receipt",
    "seed_current_resolutions_for_config",
]
