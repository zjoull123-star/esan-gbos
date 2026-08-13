"""Durable idempotency receipts and opaque CAS bindings for email draft material."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, cast

from .models import TenantScope


class EmailDraftMaterialReplayConflict(ValueError):
    """An idempotency key was already closed over a different request."""


class EmailDraftMaterialRepository(Protocol):
    def replay(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, object] | None: ...

    def commit_save(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]: ...

    def commit_finalize(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]: ...

    def resolve_draft(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None: ...

    def resolve_final(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None: ...


class PostgresEmailDraftMaterialRepository:
    """PostgreSQL implementation with site RLS and transaction-scoped replay locks."""

    def __init__(self, connection: object) -> None:
        self._connection = cast(Any, connection)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connection=<redacted>)"

    def preflight(self) -> None:
        """Fail before app construction if migration or least privileges are absent."""

        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) = 3,
                       bool_and(c.relrowsecurity), bool_and(c.relforcerowsecurity),
                       bool_and(has_table_privilege(current_user, c.oid, 'SELECT')),
                       bool_and(has_table_privilege(current_user, c.oid, 'INSERT')),
                       bool_or(has_table_privilege(current_user, c.oid, 'UPDATE')),
                       bool_or(has_table_privilege(current_user, c.oid, 'DELETE'))
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'observer'
                   AND c.relname IN (
                       'email_draft_material_receipts',
                       'email_draft_evidence_bindings',
                       'email_final_mime_evidence_bindings'
                   )
                """
            )
            row = cursor.fetchone()
        if row != (True, True, True, True, True, False, False):
            raise ValueError("email draft material repository preflight failed")

    def replay(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, object] | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            row = self._select_receipt(
                cursor,
                scope=scope,
                purpose=purpose,
                operation=operation,
                idempotency_key=idempotency_key,
            )
        return self._closed_replay(row, request_digest)

    def commit_save(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        return self._commit(
            scope,
            purpose=purpose,
            operation="save",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            receipt=receipt,
            binding=binding,
        )

    def commit_finalize(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        return self._commit(
            scope,
            purpose=purpose,
            operation="finalize",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            receipt=receipt,
            binding=binding,
        )

    def _commit(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{scope.site_id}\x1f{purpose}\x1f{operation}\x1f{idempotency_key}",),
            )
            existing = self._select_receipt(
                cursor,
                scope=scope,
                purpose=purpose,
                operation=operation,
                idempotency_key=idempotency_key,
            )
            replay = self._closed_replay(existing, request_digest)
            if replay is not None:
                return replay
            if operation == "save":
                self._insert_draft_binding(cursor, scope, purpose, binding)
            else:
                self._insert_final_binding(cursor, scope, purpose, binding)
            cursor.execute(
                """
                INSERT INTO observer.email_draft_material_receipts
                    (site_id, purpose, operation, idempotency_key,
                     request_digest, response, created_at)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    scope.site_id,
                    purpose,
                    operation,
                    idempotency_key,
                    request_digest,
                    _json(receipt),
                    binding["created_at"],
                ),
            )
        return dict(receipt)

    def resolve_draft(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT inbox_item_ref, draft_ref, draft_revision, evidence_ref,
                       object_ref, digest, media_type, byte_size, created_at
                  FROM observer.email_draft_evidence_bindings
                 WHERE site_id = %s AND purpose = %s AND evidence_ref = %s
                """,
                (scope.site_id, purpose, evidence_ref),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return dict(
            zip(
                (
                    "inbox_item_ref",
                    "draft_ref",
                    "draft_revision",
                    "evidence_ref",
                    "object_ref",
                    "digest",
                    "media_type",
                    "byte_size",
                    "created_at",
                ),
                row,
                strict=True,
            )
        )

    def resolve_final(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None:
        fields = (
            "inbox_item_ref",
            "draft_ref",
            "draft_revision",
            "evidence_ref",
            "object_ref",
            "digest",
            "media_type",
            "byte_size",
            "authorization_receipt_ref",
            "gateway_receipt_ref",
            "publication_ref",
            "message_ref",
            "mailbox_ref",
            "mailbox_config_revision",
            "observer_delivery_ref",
            "payload_digest",
            "participant_binding_digest",
            "evidence_binding_digest",
            "participant_roles_digest",
            "role_binding_digest",
            "source_draft_evidence_ref",
            "source_draft_digest",
            "created_at",
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, scope)
            cursor.execute(
                """
                SELECT inbox_item_ref, draft_ref, draft_revision, evidence_ref,
                       object_ref, digest, media_type, byte_size,
                       authorization_receipt_ref, gateway_receipt_ref,
                       publication_ref, message_ref, mailbox_ref,
                       mailbox_config_revision, observer_delivery_ref,
                       payload_digest, participant_binding_digest,
                       evidence_binding_digest, participant_roles_digest,
                       role_binding_digest, source_draft_evidence_ref,
                       source_draft_digest, created_at
                  FROM observer.email_final_mime_evidence_bindings
                 WHERE site_id = %s AND purpose = %s AND evidence_ref = %s
                """,
                (scope.site_id, purpose, evidence_ref),
            )
            row = cursor.fetchone()
        return None if row is None else dict(zip(fields, row, strict=True))

    @staticmethod
    def _set_site(cursor: Any, scope: TenantScope) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))

    @staticmethod
    def _select_receipt(
        cursor: Any,
        *,
        scope: TenantScope,
        purpose: str,
        operation: str,
        idempotency_key: str,
    ) -> tuple[object, object] | None:
        cursor.execute(
            """
            SELECT request_digest, response
              FROM observer.email_draft_material_receipts
             WHERE site_id = %s AND purpose = %s
               AND operation = %s AND idempotency_key = %s
            """,
            (scope.site_id, purpose, operation, idempotency_key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if not isinstance(row, tuple) or len(row) != 2:
            raise ValueError("stored draft material receipt is invalid")
        return row

    @staticmethod
    def _closed_replay(
        row: tuple[object, object] | None,
        request_digest: str,
    ) -> dict[str, object] | None:
        if row is None:
            return None
        if str(row[0]) != request_digest:
            raise EmailDraftMaterialReplayConflict("draft material replay drift")
        response = row[1]
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError:
                raise ValueError("stored draft material receipt is invalid") from None
        if not isinstance(response, Mapping):
            raise ValueError("stored draft material receipt is invalid")
        return {str(key): value for key, value in response.items()}

    @staticmethod
    def _insert_draft_binding(
        cursor: Any,
        scope: TenantScope,
        purpose: str,
        value: Mapping[str, object],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO observer.email_draft_evidence_bindings
                (site_id, purpose, inbox_item_ref, draft_ref, draft_revision,
                 evidence_ref, object_ref, digest, media_type, byte_size, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scope.site_id,
                purpose,
                value["inbox_item_ref"],
                value["draft_ref"],
                value["draft_revision"],
                value["evidence_ref"],
                value["object_ref"],
                value["digest"],
                value["media_type"],
                value["byte_size"],
                value["created_at"],
            ),
        )

    @staticmethod
    def _insert_final_binding(
        cursor: Any,
        scope: TenantScope,
        purpose: str,
        value: Mapping[str, object],
    ) -> None:
        fields = (
            "inbox_item_ref",
            "draft_ref",
            "draft_revision",
            "evidence_ref",
            "object_ref",
            "digest",
            "media_type",
            "byte_size",
            "authorization_receipt_ref",
            "gateway_receipt_ref",
            "publication_ref",
            "message_ref",
            "mailbox_ref",
            "mailbox_config_revision",
            "observer_delivery_ref",
            "payload_digest",
            "participant_binding_digest",
            "evidence_binding_digest",
            "participant_roles_digest",
            "role_binding_digest",
            "source_draft_evidence_ref",
            "source_draft_digest",
            "created_at",
        )
        cursor.execute(
            """
            INSERT INTO observer.email_final_mime_evidence_bindings
                (site_id, purpose, inbox_item_ref, draft_ref, draft_revision,
                 evidence_ref, object_ref, digest, media_type, byte_size,
                 authorization_receipt_ref, gateway_receipt_ref, publication_ref,
                 message_ref, mailbox_ref, mailbox_config_revision,
                 observer_delivery_ref, payload_digest,
                 participant_binding_digest, evidence_binding_digest,
                 participant_roles_digest, role_binding_digest,
                 source_draft_evidence_ref, source_draft_digest, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
            """,
            (scope.site_id, purpose, *(value[field] for field in fields)),
        )


def _json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except TypeError, ValueError:
        raise ValueError("draft material receipt is not closed JSON") from None


__all__ = [
    "EmailDraftMaterialReplayConflict",
    "EmailDraftMaterialRepository",
    "PostgresEmailDraftMaterialRepository",
]
