"""PostgreSQL repository for terminal email-material authority and callbacks."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from ..models import IdempotencyConflict, TenantScope, ValidationError
from ..postgres import Connection, redacted_database_errors, site_transaction
from ..terminal_retention import (
    EmailMaterialTombstoneCallback,
    GatewayTombstoneCallbackReceipt,
    HumanDiscardAuthorityReceipt,
    ObserverRegistrationReceipt,
    TerminalAuthorityRegistrationLease,
    TerminalMaterialAuthority,
    terminal_authority_from_row,
)

_APP_ROLE = "gbos_email_gateway_app"
_SEND_ROLE = "gbos_email_send_worker"
_RETENTION_ROLE = "gbos_email_gateway_retention_worker"
_ROLES = frozenset({_APP_ROLE, _SEND_ROLE, _RETENTION_ROLE})


class PostgresTerminalRetentionRepository:
    def __init__(self, connection: Connection, *, actual_database_role: str) -> None:
        if actual_database_role not in _ROLES:
            raise ValidationError("database role binding rejected")
        self._connection = connection
        self._actual_database_role = actual_database_role

    def __repr__(self) -> str:
        return "PostgresTerminalRetentionRepository(connection=<redacted>, role=<redacted>)"

    def preflight(self) -> None:
        with (
            redacted_database_errors(),
            site_transaction(
                self._connection,
                TenantScope("preflight.invalid", "audit_compliance"),
            ) as db,
        ):
            self._bind_database_role(db)
            db.execute(
                """
                SELECT count(*) = 3,
                       bool_and(c.relrowsecurity),
                       bool_and(c.relforcerowsecurity),
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
                 WHERE n.nspname = 'email_gateway'
                   AND c.relname IN (
                       'email_material_terminal_authorities',
                       'email_material_terminal_authority_state',
                       'email_material_tombstone_callbacks'
                   )
                """
            )
            row = db.fetchone()
        if row != (True, True, True, False, False):
            raise ValidationError("terminal retention repository preflight failed")

    def create_sent_authorities(
        self,
        scope: TenantScope,
        *,
        provider_receipt_record_ref: str,
    ) -> tuple[TerminalMaterialAuthority, ...]:
        self._require_role(_SEND_ROLE)
        rows = self._all(
            scope,
            "SELECT * FROM email_gateway.create_sent_email_material_authorities(%s, %s)",
            (scope.site_id, provider_receipt_record_ref),
        )
        return tuple(terminal_authority_from_row(row) for row in rows)

    def create_discard_authority(
        self,
        scope: TenantScope,
        *,
        receipt: HumanDiscardAuthorityReceipt,
    ) -> TerminalMaterialAuthority:
        self._require_role(_APP_ROLE)
        row = self._one(
            scope,
            """
            SELECT *
              FROM email_gateway.create_discarded_email_material_authority(
                  %s, %s, %s, %s, %s, %s, %s, %s
              )
            """,
            (
                scope.site_id,
                receipt.authority_receipt_ref,
                receipt.draft_ref,
                receipt.draft_revision,
                receipt.evidence_ref,
                receipt.evidence_digest,
                receipt.terminal_at,
                receipt.payload_digest,
            ),
        )
        if row is None:
            raise IdempotencyConflict("discard terminal authority conflict")
        return terminal_authority_from_row(row)

    def resolve_terminal(
        self,
        scope: TenantScope,
        authority_receipt_ref: str,
    ) -> TerminalMaterialAuthority:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            "SELECT * FROM email_gateway.resolve_email_material_terminal_authority(%s, %s)",
            (scope.site_id, authority_receipt_ref),
        )
        if row is None:
            raise ValidationError("terminal authority is unavailable")
        return terminal_authority_from_row(row)

    def claim_registration(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> TerminalAuthorityRegistrationLease | None:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            """
            SELECT *
              FROM email_gateway.claim_email_material_authority_registration(
                  %s, %s, %s, %s
              )
            """,
            (scope.site_id, worker_id, now, lease_until),
        )
        if row is None:
            return None
        if len(row) != 18:
            raise ValidationError("invalid terminal authority registration lease row")
        return TerminalAuthorityRegistrationLease(
            authority=terminal_authority_from_row(row[:13]),
            registration_request_ref=str(row[13]),
            worker_id=str(row[14]),
            attempt=self._positive_int(row[15], "attempt", maximum=5),
            lease_generation=self._positive_int(row[16], "lease generation", maximum=2_147_483_647),
            lease_expires_at=cast(datetime, row[17]),
        )

    def heartbeat_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> datetime:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            """
            SELECT email_gateway.heartbeat_email_material_authority_registration(
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            self._lease_params(scope, lease) + (now, lease_until),
        )
        if row is None or len(row) != 1 or not isinstance(row[0], datetime):
            raise IdempotencyConflict("registration lease fence conflict")
        return row[0]

    def ack_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        receipt: ObserverRegistrationReceipt,
        now: datetime,
    ) -> ObserverRegistrationReceipt:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            """
            SELECT email_gateway.ack_email_material_authority_registration(
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            self._lease_params(scope, lease)
            + (receipt.request_ref, receipt.evidence_ref, receipt.not_before, now),
        )
        self._require_true(row, "registration lease fence conflict")
        return receipt

    def fail_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            """
            SELECT email_gateway.fail_email_material_authority_registration(
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            self._lease_params(scope, lease) + (safe_code, next_attempt_at, now),
        )
        self._require_true(row, "registration lease fence conflict")

    def accept_tombstone_callback(
        self,
        scope: TenantScope,
        *,
        callback: EmailMaterialTombstoneCallback,
        now: datetime,
    ) -> GatewayTombstoneCallbackReceipt:
        self._require_role(_RETENTION_ROLE)
        row = self._one(
            scope,
            """
            SELECT email_gateway.accept_email_material_tombstone_callback(
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                scope.site_id,
                callback.authority_receipt_ref,
                callback.evidence_ref,
                callback.observer_request_ref,
                callback.tombstone_receipt_ref,
                callback.deleted_at,
                callback.evidence_digest,
                callback.callback_payload_digest,
                now,
            ),
        )
        if row is None or len(row) != 1:
            raise IdempotencyConflict("tombstone callback conflict")
        return GatewayTombstoneCallbackReceipt(
            callback_receipt_ref=str(row[0]),
            site_id=scope.site_id,
            authority_receipt_ref=callback.authority_receipt_ref,
            tombstone_receipt_ref=callback.tombstone_receipt_ref,
        )

    def _one(
        self,
        scope: TenantScope,
        query: str,
        params: tuple[object, ...],
    ) -> tuple[Any, ...] | None:
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            self._bind_database_role(db)
            db.execute(query, params)
            return db.fetchone()

    def _all(
        self,
        scope: TenantScope,
        query: str,
        params: tuple[object, ...],
    ) -> list[tuple[Any, ...]]:
        with redacted_database_errors(), site_transaction(self._connection, scope) as db:
            self._bind_database_role(db)
            db.execute(query, params)
            return db.fetchall()

    def _bind_database_role(self, db: Any) -> None:
        db.execute("SELECT current_user")
        row = db.fetchone()
        if row != (self._actual_database_role,):
            raise ValidationError("database role binding rejected")

    def _require_role(self, expected: str) -> None:
        if self._actual_database_role != expected:
            raise ValidationError("database role binding rejected")

    @staticmethod
    def _lease_params(
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
    ) -> tuple[object, ...]:
        return (
            scope.site_id,
            lease.authority.authority_receipt_ref,
            lease.worker_id,
            lease.attempt,
            lease.lease_generation,
        )

    @staticmethod
    def _require_true(row: tuple[Any, ...] | None, message: str) -> None:
        if row != (True,):
            raise IdempotencyConflict(message)

    @staticmethod
    def _positive_int(value: object, field: str, *, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValidationError(f"invalid {field}")
        return value


__all__ = ["PostgresTerminalRetentionRepository"]
