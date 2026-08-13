"""Durable fenced callback outbox for Observer email-material tombstones."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from .models import TenantScope

_PURPOSE = "email_draft_material"
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")
_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(value: object, field: str, *, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialRetentionCallback:
    callback_ref: str
    site_id: str
    purpose: str
    authority_receipt_ref: str
    evidence_ref: str
    observer_request_ref: str
    tombstone_receipt_ref: str
    deleted_at: datetime
    evidence_digest: str
    callback_payload_digest: str

    def __post_init__(self) -> None:
        if (
            _REF.fullmatch(self.callback_ref) is None
            or _SITE.fullmatch(self.site_id) is None
            or self.purpose != _PURPOSE
            or _REF.fullmatch(self.authority_receipt_ref) is None
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or _REF.fullmatch(self.observer_request_ref) is None
            or _REF.fullmatch(self.tombstone_receipt_ref) is None
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or _DIGEST.fullmatch(self.callback_payload_digest) is None
        ):
            raise ValueError("invalid email material retention callback")
        _aware(self.deleted_at, "deleted_at")

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> EmailMaterialRetentionCallback:
        if len(row) != 10:
            raise ValueError("invalid email material retention callback row")
        return cls(
            callback_ref=str(row[0]),
            site_id=str(row[1]),
            purpose=str(row[2]),
            authority_receipt_ref=str(row[3]),
            evidence_ref=str(row[4]),
            observer_request_ref=str(row[5]),
            tombstone_receipt_ref=str(row[6]),
            deleted_at=cast(datetime, row[7]),
            evidence_digest=str(row[8]),
            callback_payload_digest=str(row[9]),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": self.site_id,
            "purpose": self.purpose,
            "authority_receipt_ref": self.authority_receipt_ref,
            "evidence_ref": self.evidence_ref,
            "observer_request_ref": self.observer_request_ref,
            "tombstone_receipt_ref": self.tombstone_receipt_ref,
            "deleted_at": self.deleted_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "evidence_digest": self.evidence_digest,
            "callback_payload_digest": self.callback_payload_digest,
        }

    def __repr__(self) -> str:
        return (
            "EmailMaterialRetentionCallback("
            f"site_id={self.site_id!r}, purpose={self.purpose!r}, "
            "identifiers=<redacted>, digests=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialRetentionCallbackLease:
    callback: EmailMaterialRetentionCallback
    worker_id: str
    attempt: int
    lease_generation: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 256
            or "@" in self.worker_id
        ):
            raise ValueError("invalid email material callback lease")
        _positive_int(self.attempt, "attempt", maximum=5)
        _positive_int(self.lease_generation, "lease generation")
        _aware(self.lease_expires_at, "lease_expires_at")

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> EmailMaterialRetentionCallbackLease:
        if len(row) != 14:
            raise ValueError("invalid email material callback lease row")
        return cls(
            callback=EmailMaterialRetentionCallback.from_row(row[:10]),
            worker_id=str(row[10]),
            attempt=_positive_int(row[11], "attempt", maximum=5),
            lease_generation=_positive_int(row[12], "lease generation"),
            lease_expires_at=cast(datetime, row[13]),
        )

    def __repr__(self) -> str:
        return (
            "EmailMaterialRetentionCallbackLease("
            f"site_id={self.callback.site_id!r}, attempt={self.attempt}, "
            f"lease_generation={self.lease_generation}, identifiers=<redacted>)"
        )


class EmailMaterialRetentionCallbackRepository(Protocol):
    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> EmailMaterialRetentionCallbackLease | None: ...

    def heartbeat(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> datetime: ...

    def ack(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        callback_receipt_ref: str,
        now: datetime,
    ) -> None: ...

    def fail(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None: ...


class PostgresEmailMaterialRetentionCallbackRepository:
    def __init__(self, connection: object) -> None:
        self._connection = cast(Any, connection)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connection=<redacted>)"

    def preflight(self) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._bind_database_role(cursor)
            cursor.execute(
                """
                SELECT count(*) = 2,
                       bool_and(c.relrowsecurity),
                       bool_and(c.relforcerowsecurity),
                       bool_and(has_table_privilege(current_user, c.oid, 'SELECT')),
                       bool_or(
                           has_table_privilege(current_user, c.oid, 'INSERT')
                           OR has_table_privilege(current_user, c.oid, 'UPDATE')
                           OR has_table_privilege(current_user, c.oid, 'DELETE')
                       ),
                       bool_or(EXISTS (
                           SELECT 1
                             FROM aclexplode(COALESCE(
                                 c.relacl,
                                 acldefault('r', c.relowner)
                             )) AS privilege
                            WHERE privilege.grantee = 0
                              AND privilege.privilege_type = 'SELECT'
                       ))
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'observer'
                   AND c.relname IN (
                       'email_material_retention_callbacks',
                       'email_material_retention_callback_work'
                   )
                """
            )
            row = cursor.fetchone()
        if row != (True, True, True, True, False, False):
            raise ValueError("email material callback repository preflight failed")

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> EmailMaterialRetentionCallbackLease | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                "SELECT * FROM observer.claim_email_material_retention_callback(%s, %s, %s, %s)",
                (scope.site_id, worker_id, now, lease_until),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if not isinstance(row, tuple):
            raise ValueError("invalid email material callback claim")
        return EmailMaterialRetentionCallbackLease.from_row(row)

    def heartbeat(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> datetime:
        row = self._fenced_call(
            scope,
            "heartbeat_email_material_retention_callback",
            lease,
            (now, lease_until),
        )
        value = row[0]
        if not isinstance(value, datetime):
            raise ValueError("invalid email material callback heartbeat")
        return value

    def ack(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        callback_receipt_ref: str,
        now: datetime,
    ) -> None:
        if _REF.fullmatch(callback_receipt_ref) is None:
            raise ValueError("invalid gateway callback receipt")
        self._fenced_call(
            scope,
            "ack_email_material_retention_callback",
            lease,
            (callback_receipt_ref, now),
        )

    def fail(
        self,
        scope: TenantScope,
        lease: EmailMaterialRetentionCallbackLease,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        if _SAFE_CODE.fullmatch(safe_code) is None:
            raise ValueError("invalid email material callback safe code")
        self._fenced_call(
            scope,
            "fail_email_material_retention_callback",
            lease,
            (safe_code, next_attempt_at, now),
        )

    def _fenced_call(
        self,
        scope: TenantScope,
        function: str,
        lease: EmailMaterialRetentionCallbackLease,
        tail: tuple[object, ...],
    ) -> tuple[object, ...]:
        params = (
            scope.site_id,
            lease.callback.callback_ref,
            lease.worker_id,
            lease.attempt,
            lease.lease_generation,
            *tail,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                f"SELECT observer.{function}(%s, %s, %s, %s, %s, "
                + ", ".join("%s" for _ in tail)
                + ")",
                params,
            )
            row = cursor.fetchone()
        if (
            not isinstance(row, tuple)
            or row != (True,)
            and function != ("heartbeat_email_material_retention_callback")
        ):
            raise ValueError("email material callback lease fence conflict")
        if not isinstance(row, tuple):
            raise ValueError("email material callback lease fence conflict")
        return row

    def _set_site(self, cursor: Any, scope: TenantScope) -> None:
        self._bind_database_role(cursor)
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))

    @staticmethod
    def _bind_database_role(cursor: Any) -> None:
        cursor.execute("SELECT current_user")
        if cursor.fetchone() != ("gbos_observer_app",):
            raise ValueError("email material callback database role binding rejected")


__all__ = [
    "EmailMaterialRetentionCallback",
    "EmailMaterialRetentionCallbackLease",
    "EmailMaterialRetentionCallbackRepository",
    "PostgresEmailMaterialRetentionCallbackRepository",
]
