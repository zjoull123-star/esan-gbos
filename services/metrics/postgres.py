from __future__ import annotations

import hashlib
import json
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .models import (
    ImmutableConflict,
    ProjectionBatch,
    ProjectionRow,
    QueryAudit,
    SourceMode,
    StoredProjection,
    ValidationError,
    require_identifier,
    require_metric_key,
    require_window,
)


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresMetricsRepository:
    """Fixed-query PostgreSQL repository for immutable governed projections."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def append_batch(self, batch: ProjectionBatch) -> ProjectionBatch:
        batch_digest = _digest(_batch_document(batch))
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, batch.site_id)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (batch.site_id,),
            )
            cursor.execute(
                """
                SELECT source_mode
                FROM metrics.projection_batches
                WHERE site_id = %s
                ORDER BY source_mode
                LIMIT 1
                """,
                (batch.site_id,),
            )
            existing_mode = cursor.fetchone()
            if (
                existing_mode is not None
                and SourceMode(str(existing_mode[0])) is not batch.source_mode
            ):
                raise ValidationError(
                    "synthetic and live projection modes are mutually exclusive per site"
                )
            cursor.execute(
                """
                SELECT payload_digest
                FROM metrics.projection_batches
                WHERE site_id = %s AND batch_id = %s
                """,
                (batch.site_id, batch.batch_id),
            )
            existing_batch = cursor.fetchone()
            if existing_batch is not None:
                if existing_batch[0] != batch_digest:
                    raise ImmutableConflict("batch_id was reused with different immutable content")
                return batch
            cursor.execute(
                """
                SELECT batch_id
                FROM metrics.checkpoints
                WHERE site_id = %s AND source_mode = %s AND checkpoint = %s
                """,
                (batch.site_id, batch.source_mode.value, batch.checkpoint),
            )
            existing_checkpoint = cursor.fetchone()
            if existing_checkpoint is not None:
                raise ImmutableConflict("checkpoint was already recorded by another batch")
            cursor.execute(
                """
                INSERT INTO metrics.projection_batches (
                    site_id, batch_id, source_mode, checkpoint, source_system,
                    transformation_version, retrieved_at, payload_digest
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (site_id, batch_id) DO NOTHING
                RETURNING payload_digest
                """,
                (
                    batch.site_id,
                    batch.batch_id,
                    batch.source_mode.value,
                    batch.checkpoint,
                    batch.source_system,
                    batch.transformation_version,
                    batch.retrieved_at,
                    batch_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT payload_digest
                    FROM metrics.projection_batches
                    WHERE site_id = %s AND batch_id = %s
                    """,
                    (batch.site_id, batch.batch_id),
                )
                existing = cursor.fetchone()
                if existing is not None and existing[0] != batch_digest:
                    raise ImmutableConflict("batch_id was reused with different immutable content")
            for row in batch.rows:
                lineage = {
                    "source_system": batch.source_system,
                    "source_record_refs": list(row.source_record_refs),
                    "retrieved_at": batch.retrieved_at.isoformat(),
                    "transformation_version": batch.transformation_version,
                    "evidence_status": (
                        "synthetic" if batch.source_mode is SourceMode.SYNTHETIC else "verified"
                    ),
                }
                freshness = {"as_of": row.as_of.isoformat()}
                coverage = {
                    "included_count": row.included_count,
                    "total_count": row.total_count,
                }
                reconciliation = {
                    "reference": row.reconciliation_reference,
                    "variance": str(row.reconciliation_variance),
                    "checked_at": row.reconciliation_checked_at.isoformat(),
                }
                cursor.execute(
                    """
                    INSERT INTO metrics.projection_rows (
                        site_id, row_id, batch_id, metric_key, definition_version,
                        window_start, window_end, as_of, value, included_count,
                        total_count, reconciliation_reference,
                        reconciliation_variance, reconciliation_checked_at,
                        source_record_refs, governed, source_lineage, freshness,
                        coverage, reconciliation, payload_digest
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s::jsonb, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s
                    )
                    ON CONFLICT (site_id, row_id) DO NOTHING
                    RETURNING payload_digest
                    """,
                    (
                        batch.site_id,
                        row.row_id,
                        batch.batch_id,
                        row.metric_key,
                        row.definition_version,
                        row.window_start,
                        row.window_end,
                        row.as_of,
                        row.value,
                        row.included_count,
                        row.total_count,
                        row.reconciliation_reference,
                        row.reconciliation_variance,
                        row.reconciliation_checked_at,
                        json.dumps(row.source_record_refs),
                        row.governed,
                        json.dumps(lineage, sort_keys=True),
                        json.dumps(freshness, sort_keys=True),
                        json.dumps(coverage, sort_keys=True),
                        json.dumps(reconciliation, sort_keys=True),
                        _digest(_row_document(row)),
                    ),
                )
                inserted_row = cursor.fetchone()
                if inserted_row is None:
                    cursor.execute(
                        """
                        SELECT batch_id, payload_digest
                        FROM metrics.projection_rows
                        WHERE site_id = %s AND row_id = %s
                        """,
                        (batch.site_id, row.row_id),
                    )
                    existing_row = cursor.fetchone()
                    expected_digest = _digest(_row_document(row))
                    if existing_row is not None and (
                        existing_row[0] != batch.batch_id or existing_row[1] != expected_digest
                    ):
                        raise ImmutableConflict(
                            "row_id was reused with different immutable content"
                        )
            cursor.execute(
                """
                INSERT INTO metrics.checkpoints (
                    site_id, source_mode, checkpoint, batch_id, recorded_at
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (site_id, source_mode, checkpoint) DO NOTHING
                """,
                (
                    batch.site_id,
                    batch.source_mode.value,
                    batch.checkpoint,
                    batch.batch_id,
                    batch.retrieved_at,
                ),
            )
        return batch

    def find_projection(
        self,
        *,
        site_id: str,
        metric_key: str,
        source_mode: SourceMode,
        window_start: datetime,
        window_end: datetime,
    ) -> StoredProjection | None:
        require_identifier(site_id, "site_id")
        require_metric_key(metric_key)
        require_window(window_start, window_end)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT
                    batch.batch_id, batch.site_id, batch.source_mode,
                    batch.checkpoint, batch.source_system,
                    batch.transformation_version, batch.retrieved_at,
                    row.row_id, row.metric_key, row.window_start, row.window_end,
                    row.as_of, row.value, row.included_count, row.total_count,
                    row.reconciliation_reference, row.reconciliation_variance,
                    row.reconciliation_checked_at, row.source_record_refs,
                    row.governed, row.definition_version
                FROM metrics.projection_rows AS row
                JOIN metrics.projection_batches AS batch
                  ON batch.site_id = row.site_id AND batch.batch_id = row.batch_id
                WHERE row.site_id = %s
                  AND row.metric_key = %s
                  AND batch.source_mode = %s
                  AND row.window_start = %s
                  AND row.window_end = %s
                ORDER BY batch.retrieved_at DESC, batch.batch_id DESC, row.row_id DESC
                LIMIT 1
                """,
                (site_id, metric_key, source_mode.value, window_start, window_end),
            )
            record = cursor.fetchone()
        if record is None:
            return None
        refs = record[18]
        if isinstance(refs, str):
            refs = json.loads(refs)
        batch = ProjectionBatch(
            batch_id=str(record[0]),
            site_id=str(record[1]),
            source_mode=SourceMode(str(record[2])),
            checkpoint=str(record[3]),
            source_system=str(record[4]),
            transformation_version=str(record[5]),
            retrieved_at=record[6],
            rows=(
                ProjectionRow(
                    row_id=str(record[7]),
                    metric_key=str(record[8]),
                    window_start=record[9],
                    window_end=record[10],
                    as_of=record[11],
                    value=Decimal(str(record[12])),
                    included_count=int(record[13]),
                    total_count=int(record[14]),
                    reconciliation_reference=str(record[15]),
                    reconciliation_variance=Decimal(str(record[16])),
                    reconciliation_checked_at=record[17],
                    source_record_refs=tuple(str(ref) for ref in refs),
                    governed=bool(record[19]),
                    definition_version=str(record[20]),
                ),
            ),
        )
        return StoredProjection(batch=batch, row=batch.rows[0])

    def checkpoint(self, site_id: str, source_mode: SourceMode) -> str | None:
        require_identifier(site_id, "site_id")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT checkpoint
                FROM metrics.checkpoints
                WHERE site_id = %s AND source_mode = %s
                ORDER BY recorded_at DESC, checkpoint DESC
                LIMIT 1
                """,
                (site_id, source_mode.value),
            )
            row = cursor.fetchone()
        return str(row[0]) if row is not None else None

    def append_audit(
        self,
        *,
        site_id: str,
        audit_id: str,
        request_id: str,
        metric_key: str,
        source_mode: SourceMode,
        window_start: datetime,
        window_end: datetime,
        queried_at: datetime,
        outcome: str,
        reason: str | None,
        batch_id: str | None,
        row_id: str | None,
    ) -> QueryAudit:
        audit = QueryAudit(
            site_id=site_id,
            audit_id=audit_id,
            request_id=request_id,
            metric_key=metric_key,
            source_mode=source_mode,
            window_start=window_start,
            window_end=window_end,
            queried_at=queried_at,
            outcome=outcome,
            reason=reason,
            batch_id=batch_id,
            row_id=row_id,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                INSERT INTO metrics.query_audit (
                    site_id, audit_id, request_id, metric_key, source_mode,
                    window_start, window_end, queried_at, outcome, reason,
                    batch_id, row_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    site_id,
                    audit_id,
                    request_id,
                    metric_key,
                    source_mode.value,
                    window_start,
                    window_end,
                    queried_at,
                    outcome,
                    reason,
                    batch_id,
                    row_id,
                ),
            )
        return audit

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))


def _digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _batch_document(batch: ProjectionBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.batch_id,
        "site_id": batch.site_id,
        "source_mode": batch.source_mode.value,
        "checkpoint": batch.checkpoint,
        "source_system": batch.source_system,
        "transformation_version": batch.transformation_version,
        "retrieved_at": batch.retrieved_at.isoformat(),
        "rows": [_row_document(row) for row in batch.rows],
    }


def _row_document(row: ProjectionRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "metric_key": row.metric_key,
        "definition_version": row.definition_version,
        "window_start": row.window_start.isoformat(),
        "window_end": row.window_end.isoformat(),
        "as_of": row.as_of.isoformat(),
        "value": str(row.value),
        "included_count": row.included_count,
        "total_count": row.total_count,
        "reconciliation_reference": row.reconciliation_reference,
        "reconciliation_variance": str(row.reconciliation_variance),
        "reconciliation_checked_at": row.reconciliation_checked_at.isoformat(),
        "source_record_refs": list(row.source_record_refs),
        "governed": row.governed,
    }
