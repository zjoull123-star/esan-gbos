"""Append-only publication payloads with a generation-fenced delivery relay."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from .email_publication import EmailMessagePublication
from .models import _require_aware
from .storage import Connection

EMAIL_PUBLICATION_RELAY_STATES = frozenset(
    {"queued", "leased", "retry_wait", "delivered", "dead_letter"}
)


class EmailPublicationConflict(ValueError):
    def __init__(self, code: str = "publication_payload_conflict") -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class EmailPublicationRelayConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class EmailPublicationRelayFenceConflict(EmailPublicationRelayConflict):
    pass


@dataclass(frozen=True, slots=True)
class EmailPublicationReceipt:
    publication_id: str
    payload_sha256: str
    replayed: bool

    @property
    def transport_payload_digest(self) -> str:
        """Gateway wire digests are prefixed; the Observer DB stores bare char(64)."""

        return "sha256:" + self.payload_sha256


@dataclass(frozen=True, slots=True, repr=False)
class EmailPublicationRelayClaim:
    publication_id: str
    site_id: str
    payload: dict[str, Any]
    payload_sha256: str
    status: str
    attempt_count: int
    max_attempts: int
    generation: int
    lease_owner: str
    lease_expires_at: datetime

    @property
    def transport_payload_digest(self) -> str:
        return "sha256:" + self.payload_sha256

    def to_delivery_envelope(self) -> dict[str, object]:
        return {
            "publication": self.payload,
            "payload_digest": self.transport_payload_digest,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(publication_id={self.publication_id!r}, "
            f"site_id={self.site_id!r}, status={self.status!r}, "
            f"attempt_count={self.attempt_count}, max_attempts={self.max_attempts}, "
            f"generation={self.generation}, lease_owner={self.lease_owner!r}, "
            f"lease_expires_at={self.lease_expires_at.isoformat()!r}, "
            f"payload_digest={self.transport_payload_digest!r}, payload=<protected>)"
        )


@dataclass(frozen=True, slots=True)
class EmailPublicationDeliveryReceipt:
    publication_id: str
    receipt_ref: str
    receipt_sha256: str
    status: str
    delivered_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class EmailPublicationRelayRelease:
    publication_id: str
    status: str
    attempt_count: int
    max_attempts: int
    generation: int
    next_attempt_at: datetime


@dataclass(slots=True)
class _RelayRecord:
    publication: EmailMessagePublication
    status: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime
    generation: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    delivery_receipt: dict[str, object] | None = None
    delivery_receipt_sha256: str | None = None
    delivered_at: datetime | None = None


class InMemoryEmailPublicationOutbox:
    """Deterministic repository used by isolated tests and fake-provider pilots."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], _RelayRecord] = {}

    @property
    def records(self) -> tuple[EmailMessagePublication, ...]:
        return tuple(record.publication for record in self._records.values())

    def append(
        self,
        publication: EmailMessagePublication,
        *,
        max_attempts: int = 5,
    ) -> EmailPublicationReceipt:
        if not isinstance(publication, EmailMessagePublication):
            raise TypeError("invalid email publication")
        if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 5:
            raise ValueError("invalid max_attempts")
        key = self._key(publication)
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = _RelayRecord(
                publication=publication,
                status="queued",
                attempt_count=0,
                max_attempts=max_attempts,
                next_attempt_at=publication.received_at,
            )
            return EmailPublicationReceipt(
                publication_id=publication.publication_id,
                payload_sha256=publication.payload_sha256,
                replayed=False,
            )
        if existing.publication.payload_sha256 != publication.payload_sha256:
            raise EmailPublicationConflict()
        return EmailPublicationReceipt(
            publication_id=existing.publication.publication_id,
            payload_sha256=existing.publication.payload_sha256,
            replayed=True,
        )

    def claim(
        self,
        *,
        site_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> EmailPublicationRelayClaim | None:
        self._validate_relay_call(site_id, worker_id, now, lease_seconds)
        eligible = (
            record
            for record in self._records.values()
            if record.publication.site_id == site_id
            and record.attempt_count < record.max_attempts
            and (
                (record.status in {"queued", "retry_wait"} and record.next_attempt_at <= now)
                or (
                    record.status == "leased"
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= now
                )
            )
        )
        record = min(
            eligible,
            key=lambda value: (value.next_attempt_at, value.publication.publication_id),
            default=None,
        )
        if record is None:
            return None
        record.status = "leased"
        record.attempt_count += 1
        record.generation += 1
        record.lease_owner = worker_id
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.last_error_code = None
        return self._claim(record)

    def heartbeat(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> EmailPublicationRelayClaim:
        self._validate_relay_call(site_id, worker_id, now, lease_seconds)
        record = self._owned_lease(site_id, publication_id, worker_id, expected_generation, now)
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return self._claim(record)

    def release(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> EmailPublicationRelayRelease:
        _require_aware(next_attempt_at, "next_attempt_at")
        if next_attempt_at < now or not error_code or len(error_code) > 128:
            raise ValueError("invalid publication relay release")
        record = self._owned_lease(site_id, publication_id, worker_id, expected_generation, now)
        record.status = (
            "dead_letter" if record.attempt_count >= record.max_attempts else "retry_wait"
        )
        record.next_attempt_at = next_attempt_at
        record.last_error_code = error_code
        record.lease_owner = None
        record.lease_expires_at = None
        return EmailPublicationRelayRelease(
            publication_id=record.publication.publication_id,
            status=record.status,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            generation=record.generation,
            next_attempt_at=record.next_attempt_at,
        )

    def acknowledge(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        receipt: Mapping[str, object],
        now: datetime,
    ) -> EmailPublicationDeliveryReceipt:
        _require_aware(now, "now")
        canonical_receipt, receipt_sha256 = _canonical_receipt(receipt)
        record = self._find(site_id, publication_id)
        if record.status == "delivered":
            if not hmac_digest_equal(record.delivery_receipt_sha256, receipt_sha256):
                raise EmailPublicationRelayConflict("receipt_replay_drift")
            return self._delivery_receipt(record, replayed=True)
        record = self._owned_lease(site_id, publication_id, worker_id, expected_generation, now)
        expected_payload_digest = "sha256:" + record.publication.payload_sha256
        if canonical_receipt.get("payload_digest") != expected_payload_digest:
            raise EmailPublicationRelayConflict("receipt_payload_digest_mismatch")
        record.status = "delivered"
        record.delivery_receipt = canonical_receipt
        record.delivery_receipt_sha256 = receipt_sha256
        record.delivered_at = now
        record.lease_owner = None
        record.lease_expires_at = None
        return self._delivery_receipt(record, replayed=False)

    def _owned_lease(
        self,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        now: datetime,
    ) -> _RelayRecord:
        _require_aware(now, "now")
        record = self._find(site_id, publication_id)
        if (
            record.status != "leased"
            or record.lease_owner != worker_id
            or record.generation != expected_generation
            or record.lease_expires_at is None
            or record.lease_expires_at < now
        ):
            raise EmailPublicationRelayFenceConflict("relay_lease_fence_conflict")
        return record

    def _find(self, site_id: str, publication_id: str) -> _RelayRecord:
        for record in self._records.values():
            if (
                record.publication.site_id == site_id
                and record.publication.publication_id == publication_id
            ):
                return record
        raise EmailPublicationRelayConflict("publication_not_found")

    @staticmethod
    def _claim(
        record: _RelayRecord,
        *,
        lease_owner: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> EmailPublicationRelayClaim:
        owner = record.lease_owner if lease_owner is None else lease_owner
        expires = record.lease_expires_at if lease_expires_at is None else lease_expires_at
        if owner is None or expires is None:
            raise RuntimeError("relay claim snapshot requires lease metadata")
        return EmailPublicationRelayClaim(
            publication_id=record.publication.publication_id,
            site_id=record.publication.site_id,
            payload=record.publication.to_wire(),
            payload_sha256=record.publication.payload_sha256,
            status=record.status,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            generation=record.generation,
            lease_owner=owner,
            lease_expires_at=expires,
        )

    @staticmethod
    def _delivery_receipt(
        record: _RelayRecord, *, replayed: bool
    ) -> EmailPublicationDeliveryReceipt:
        if (
            record.delivery_receipt is None
            or record.delivery_receipt_sha256 is None
            or record.delivered_at is None
        ):
            raise RuntimeError("delivered relay record is incomplete")
        receipt_ref = record.delivery_receipt.get("receipt_ref")
        if not isinstance(receipt_ref, str) or not receipt_ref:
            raise EmailPublicationRelayConflict("receipt_ref_invalid")
        return EmailPublicationDeliveryReceipt(
            publication_id=record.publication.publication_id,
            receipt_ref=receipt_ref,
            receipt_sha256=record.delivery_receipt_sha256,
            status="delivered",
            delivered_at=record.delivered_at,
            replayed=replayed,
        )

    @staticmethod
    def _key(publication: EmailMessagePublication) -> tuple[str, str, str]:
        return (
            publication.site_id,
            publication.mailbox_id,
            publication.observer_delivery_ref,
        )

    @staticmethod
    def _validate_relay_call(
        site_id: str, worker_id: str, now: datetime, lease_seconds: int
    ) -> None:
        _require_aware(now, "now")
        if not site_id or not worker_id or len(worker_id) > 128:
            raise ValueError("invalid publication relay identity")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise ValueError("invalid publication relay lease")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(record_count={len(self._records)})"


def _canonical_receipt(receipt: Mapping[str, object]) -> tuple[dict[str, object], str]:
    if not isinstance(receipt, Mapping):
        raise TypeError("invalid gateway publication receipt")
    try:
        encoded = json.dumps(
            dict(receipt), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except TypeError, ValueError, UnicodeEncodeError:
        raise EmailPublicationRelayConflict("receipt_invalid") from None
    if not 2 <= len(encoded) <= 16_384:
        raise EmailPublicationRelayConflict("receipt_invalid")
    return dict(receipt), hashlib.sha256(encoded).hexdigest()


def hmac_digest_equal(left: str | None, right: str) -> bool:
    if left is None:
        return False
    return hmac.compare_digest(left, right)


class PostgresEmailPublicationRelay:
    """Durable claim/heartbeat/release/ack seam for the Gateway relay worker."""

    _CLAIM_COLUMNS = """
        publication_id, site_id, payload, payload_digest, relay_status,
        attempt_count, max_attempts, relay_generation, lease_owner,
        lease_expires_at
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def claim(
        self,
        *,
        site_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> EmailPublicationRelayClaim | None:
        InMemoryEmailPublicationOutbox._validate_relay_call(site_id, worker_id, now, lease_seconds)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT publication_id
                    FROM observer.email_message_publication_outbox
                    WHERE site_id = %s
                      AND attempt_count < max_attempts
                      AND (
                          (relay_status IN ('queued', 'retry_wait')
                              AND next_attempt_at <= %s)
                          OR (relay_status = 'leased' AND lease_expires_at <= %s)
                      )
                    ORDER BY next_attempt_at, publication_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE observer.email_message_publication_outbox AS outbox
                SET relay_status = 'leased',
                    attempt_count = outbox.attempt_count + 1,
                    relay_generation = outbox.relay_generation + 1,
                    lease_owner = %s,
                    lease_expires_at = %s + (%s * interval '1 second'),
                    last_error_code = NULL,
                    updated_at = %s
                FROM candidate
                WHERE outbox.site_id = %s
                  AND outbox.publication_id = candidate.publication_id
                RETURNING {self._CLAIM_COLUMNS}
                """,
                (site_id, now, now, worker_id, now, lease_seconds, now, site_id),
            )
            row = cursor.fetchone()
            return None if row is None else self._claim_from_row(row)

    def heartbeat(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> EmailPublicationRelayClaim:
        InMemoryEmailPublicationOutbox._validate_relay_call(site_id, worker_id, now, lease_seconds)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE observer.email_message_publication_outbox
                SET lease_expires_at = %s + (%s * interval '1 second'),
                    updated_at = %s
                WHERE site_id = %s
                  AND publication_id = %s
                  AND relay_status = 'leased'
                  AND lease_owner = %s
                  AND relay_generation = %s
                  AND lease_expires_at >= %s
                RETURNING {self._CLAIM_COLUMNS}
                """,
                (
                    now,
                    lease_seconds,
                    now,
                    site_id,
                    publication_id,
                    worker_id,
                    expected_generation,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise EmailPublicationRelayFenceConflict("relay_lease_fence_conflict")
            return self._claim_from_row(row)

    def release(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> EmailPublicationRelayRelease:
        _require_aware(now, "now")
        _require_aware(next_attempt_at, "next_attempt_at")
        if next_attempt_at < now or not error_code or len(error_code) > 128:
            raise ValueError("invalid publication relay release")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                UPDATE observer.email_message_publication_outbox
                SET relay_status = CASE
                        WHEN attempt_count >= max_attempts THEN 'dead_letter'
                        ELSE 'retry_wait'
                    END,
                    next_attempt_at = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND publication_id = %s
                  AND relay_status = 'leased'
                  AND lease_owner = %s
                  AND relay_generation = %s
                  AND lease_expires_at >= %s
                RETURNING relay_status, attempt_count, max_attempts,
                          relay_generation, next_attempt_at
                """,
                (
                    next_attempt_at,
                    error_code,
                    now,
                    site_id,
                    publication_id,
                    worker_id,
                    expected_generation,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise EmailPublicationRelayFenceConflict("relay_lease_fence_conflict")
            return EmailPublicationRelayRelease(
                publication_id=publication_id,
                status=str(row[0]),
                attempt_count=int(str(row[1])),
                max_attempts=int(str(row[2])),
                generation=int(str(row[3])),
                next_attempt_at=cast(datetime, row[4]),
            )

    def acknowledge(
        self,
        *,
        site_id: str,
        publication_id: str,
        worker_id: str,
        expected_generation: int,
        receipt: Mapping[str, object],
        now: datetime,
    ) -> EmailPublicationDeliveryReceipt:
        _require_aware(now, "now")
        canonical_receipt, receipt_sha256 = _canonical_receipt(receipt)
        receipt_ref = canonical_receipt.get("receipt_ref")
        if not isinstance(receipt_ref, str) or not receipt_ref:
            raise EmailPublicationRelayConflict("receipt_ref_invalid")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT payload_digest, relay_status, delivery_receipt_digest,
                       delivered_at
                FROM observer.email_message_publication_outbox
                WHERE site_id = %s AND publication_id = %s
                FOR UPDATE
                """,
                (site_id, publication_id),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise EmailPublicationRelayConflict("publication_not_found")
            if str(existing[1]) == "delivered":
                if not hmac.compare_digest(str(existing[2]), receipt_sha256):
                    raise EmailPublicationRelayConflict("receipt_replay_drift")
                return EmailPublicationDeliveryReceipt(
                    publication_id=publication_id,
                    receipt_ref=receipt_ref,
                    receipt_sha256=receipt_sha256,
                    status="delivered",
                    delivered_at=existing[3],
                    replayed=True,
                )
            if canonical_receipt.get("payload_digest") != "sha256:" + str(existing[0]):
                raise EmailPublicationRelayConflict("receipt_payload_digest_mismatch")
            cursor.execute(
                """
                UPDATE observer.email_message_publication_outbox
                SET relay_status = 'delivered',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error_code = NULL,
                    delivery_receipt = %s::jsonb,
                    delivery_receipt_digest = %s,
                    delivered_at = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND publication_id = %s
                  AND relay_status = 'leased'
                  AND lease_owner = %s
                  AND relay_generation = %s
                  AND lease_expires_at >= %s
                RETURNING delivered_at
                """,
                (
                    json.dumps(canonical_receipt, sort_keys=True, separators=(",", ":")),
                    receipt_sha256,
                    now,
                    now,
                    site_id,
                    publication_id,
                    worker_id,
                    expected_generation,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise EmailPublicationRelayFenceConflict("relay_lease_fence_conflict")
            return EmailPublicationDeliveryReceipt(
                publication_id=publication_id,
                receipt_ref=receipt_ref,
                receipt_sha256=receipt_sha256,
                status="delivered",
                delivered_at=row[0],
                replayed=False,
            )

    @classmethod
    def _claim_from_row(cls, row: tuple[object, ...]) -> EmailPublicationRelayClaim:
        payload = row[2]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise EmailPublicationRelayConflict("publication_payload_invalid")
        return EmailPublicationRelayClaim(
            publication_id=str(row[0]),
            site_id=str(row[1]),
            payload=payload,
            payload_sha256=str(row[3]),
            status=str(row[4]),
            attempt_count=int(str(row[5])),
            max_attempts=int(str(row[6])),
            generation=int(str(row[7])),
            lease_owner=str(row[8]),
            lease_expires_at=cast(datetime, row[9]),
        )

    @staticmethod
    def _set_site(cursor: object, site_id: str) -> None:
        cursor.execute(  # type: ignore[attr-defined]
            "SELECT set_config('app.site_id', %s, true)", (site_id,)
        )


__all__ = [
    "EMAIL_PUBLICATION_RELAY_STATES",
    "EmailPublicationConflict",
    "EmailPublicationDeliveryReceipt",
    "EmailPublicationReceipt",
    "EmailPublicationRelayClaim",
    "EmailPublicationRelayConflict",
    "EmailPublicationRelayFenceConflict",
    "EmailPublicationRelayRelease",
    "InMemoryEmailPublicationOutbox",
    "PostgresEmailPublicationRelay",
]
