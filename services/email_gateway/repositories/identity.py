from __future__ import annotations

from threading import RLock
from typing import Any

from ..models import (
    IdempotencyConflict,
    IdentityProjection,
    RevisionConflict,
    TenantScope,
    require_scope,
)
from ..postgres import Connection, redacted_database_errors, site_transaction


class InMemoryIdentityProjectionRepository:
    def __init__(self) -> None:
        self._projections: dict[tuple[str, str, str], IdentityProjection] = {}
        self._lock = RLock()

    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None:
        return self._projections.get((scope.site_id, scope.processing_purpose, opaque_address_ref))

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection:
        require_scope(
            scope,
            site_id=projection.site_id,
            processing_purpose=projection.processing_purpose,
        )
        key = (scope.site_id, scope.processing_purpose, projection.opaque_address_ref)
        with self._lock:
            current = self._projections.get(key)
            if current is not None:
                if projection.external_identity_revision < current.external_identity_revision:
                    raise RevisionConflict("stale identity projection")
                if projection.external_identity_revision == current.external_identity_revision:
                    if projection.payload_digest != current.payload_digest or projection != current:
                        raise IdempotencyConflict("identity projection revision drift")
                    return current
            self._projections[key] = projection
            return projection


class PostgresIdentityProjectionRepository:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __repr__(self) -> str:
        return "PostgresIdentityProjectionRepository(connection=<redacted>)"

    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT site_id, processing_purpose, opaque_address_ref,
                       external_identity_ref, external_identity_revision,
                       identity_type, team_ref, status, projection_receipt_ref,
                       observed_at, payload_digest
                  FROM email_gateway.identity_projection_receipts
                 WHERE site_id = %s AND processing_purpose = %s
                   AND opaque_address_ref = %s
                 ORDER BY external_identity_revision DESC, created_at DESC
                 LIMIT 1
                """,
                (scope.site_id, scope.processing_purpose, opaque_address_ref),
            )
            row = cursor.fetchone()
            return None if row is None else _projection_from_row(row)

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection:
        require_scope(
            scope,
            site_id=projection.site_id,
            processing_purpose=projection.processing_purpose,
        )
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended(%s || chr(31) || %s || chr(31) || %s, 0)
                )
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    projection.opaque_address_ref,
                ),
            )
            cursor.execute(
                """
                SELECT site_id, processing_purpose, opaque_address_ref,
                       external_identity_ref, external_identity_revision,
                       identity_type, team_ref, status, projection_receipt_ref,
                       observed_at, payload_digest
                  FROM email_gateway.identity_projection_receipts
                 WHERE site_id = %s AND processing_purpose = %s
                   AND opaque_address_ref = %s
                 ORDER BY external_identity_revision DESC, created_at DESC
                 LIMIT 1
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    projection.opaque_address_ref,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                current = _projection_from_row(row)
                if projection.external_identity_revision < current.external_identity_revision:
                    raise RevisionConflict("stale identity projection")
                if projection.external_identity_revision == current.external_identity_revision:
                    if projection != current:
                        raise IdempotencyConflict("identity projection revision drift")
                    return current
            cursor.execute(
                """
                INSERT INTO email_gateway.identity_projection_receipts (
                    site_id, processing_purpose, opaque_address_ref,
                    external_identity_ref, external_identity_revision, identity_type,
                    team_ref, status, projection_receipt_ref, observed_at, payload_digest
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    site_id, processing_purpose, opaque_address_ref,
                    external_identity_revision
                )
                DO NOTHING
                """,
                (
                    projection.site_id,
                    projection.processing_purpose,
                    projection.opaque_address_ref,
                    projection.external_identity_ref,
                    projection.external_identity_revision,
                    projection.identity_type,
                    projection.team_ref,
                    projection.status,
                    projection.projection_receipt_ref,
                    projection.observed_at,
                    projection.payload_digest,
                ),
            )
            cursor.execute(
                """
                SELECT site_id, processing_purpose, opaque_address_ref,
                       external_identity_ref, external_identity_revision,
                       identity_type, team_ref, status, projection_receipt_ref,
                       observed_at, payload_digest
                  FROM email_gateway.identity_projection_receipts
                 WHERE site_id = %s AND processing_purpose = %s
                   AND opaque_address_ref = %s AND external_identity_revision = %s
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    projection.opaque_address_ref,
                    projection.external_identity_revision,
                ),
            )
            durable_row = cursor.fetchone()
            if durable_row is None:
                raise IdempotencyConflict("identity projection persistence conflict")
            durable = _projection_from_row(durable_row)
            if durable != projection:
                raise IdempotencyConflict("identity projection revision drift")
            return durable


def _projection_from_row(row: tuple[Any, ...]) -> IdentityProjection:
    if len(row) != 11:
        raise IdempotencyConflict("invalid identity projection database row")
    return IdentityProjection(
        site_id=str(row[0]),
        processing_purpose=str(row[1]),
        opaque_address_ref=str(row[2]),
        external_identity_ref=str(row[3]),
        external_identity_revision=int(row[4]),
        identity_type=str(row[5]),
        team_ref=str(row[6]),
        status=str(row[7]),
        projection_receipt_ref=str(row[8]),
        observed_at=row[9],
        payload_digest=str(row[10]),
    )


__all__ = [
    "InMemoryIdentityProjectionRepository",
    "PostgresIdentityProjectionRepository",
]
