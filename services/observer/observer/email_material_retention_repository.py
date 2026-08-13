"""PostgreSQL repository for fenced Observer email-material retention."""

from __future__ import annotations

from typing import Any, cast

from .email_material_retention import (
    EmailMaterialDeletionLease,
    EmailMaterialRetentionRequest,
    EmailMaterialTombstoneReceipt,
    TerminalMaterialAuthority,
)
from .models import TenantScope


class PostgresEmailMaterialRetentionRepository:
    def __init__(self, connection: object) -> None:
        self._connection = cast(Any, connection)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connection=<redacted>)"

    def preflight(self) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) = 4,
                       bool_and(c.relrowsecurity),
                       bool_and(c.relforcerowsecurity),
                       bool_and(has_table_privilege(current_user, c.oid, 'SELECT')),
                       bool_or(
                           has_table_privilege(current_user, c.oid, 'INSERT')
                           OR has_table_privilege(current_user, c.oid, 'UPDATE')
                           OR has_table_privilege(current_user, c.oid, 'DELETE')
                       ),
                       bool_or(has_table_privilege('PUBLIC', c.oid, 'SELECT'))
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'observer'
                   AND c.relname IN (
                       'email_material_retention_requests',
                       'email_material_retention_work',
                       'email_material_tombstone_receipts',
                       'email_material_legal_hold_events'
                   )
                """
            )
            row = cursor.fetchone()
        if row != (True, True, True, True, False, False):
            raise ValueError("email material retention repository preflight failed")

    def register(
        self,
        scope: TenantScope,
        *,
        authority: TerminalMaterialAuthority,
        not_before: Any,
    ) -> EmailMaterialRetentionRequest:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT *
                  FROM observer.register_email_material_retention(
                      %s, %s, %s, %s, %s, %s, %s, %s, %s
                  )
                """,
                (
                    scope.site_id,
                    authority.purpose,
                    authority.evidence_ref,
                    authority.terminal_state,
                    authority.terminal_at,
                    not_before,
                    authority.authority_receipt_ref,
                    authority.draft_ref,
                    authority.draft_revision,
                ),
            )
            row = cursor.fetchone()
        if not isinstance(row, tuple):
            raise ValueError("email material retention registration failed")
        return EmailMaterialRetentionRequest.from_row(row)

    def claim_due(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: Any,
        lease_until: Any,
        limit: int,
    ) -> tuple[EmailMaterialDeletionLease, ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT *
                  FROM observer.claim_email_material_retention(%s, %s, %s, %s, %s)
                """,
                (scope.site_id, worker_id, now, lease_until, limit),
            )
            rows = cursor.fetchall()
        return tuple(EmailMaterialDeletionLease.from_row(row) for row in rows)

    def complete_deletion(
        self,
        scope: TenantScope,
        lease: EmailMaterialDeletionLease,
        *,
        receipt_ref: str,
        deleted_at: Any,
    ) -> EmailMaterialTombstoneReceipt:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT *
                  FROM observer.complete_email_material_retention(
                      %s, %s, %s, %s, %s, %s
                  )
                """,
                (
                    scope.site_id,
                    lease.request_ref,
                    lease.worker_id,
                    lease.lease_generation,
                    receipt_ref,
                    deleted_at,
                ),
            )
            row = cursor.fetchone()
        if not isinstance(row, tuple):
            raise ValueError("email material retention completion fence conflict")
        return EmailMaterialTombstoneReceipt.from_row(row)

    def resolve_receipt(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        tombstone_receipt_ref: str,
    ) -> EmailMaterialTombstoneReceipt | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT *
                  FROM observer.resolve_email_material_tombstone(%s, %s, %s)
                """,
                (scope.site_id, evidence_ref, tombstone_receipt_ref),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if not isinstance(row, tuple):
            raise ValueError("invalid email material tombstone receipt")
        return EmailMaterialTombstoneReceipt.from_row(row)

    def has_legal_hold(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        checked_at: Any,
    ) -> bool:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT observer.email_material_has_legal_hold(%s, %s, %s)
                """,
                (scope.site_id, evidence_ref, checked_at),
            )
            row = cursor.fetchone()
        if not isinstance(row, tuple) or len(row) != 1 or not isinstance(row[0], bool):
            raise ValueError("invalid email material legal hold result")
        return row[0]

    @staticmethod
    def _set_site(cursor: Any, scope: TenantScope) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))


__all__ = ["PostgresEmailMaterialRetentionRepository"]
