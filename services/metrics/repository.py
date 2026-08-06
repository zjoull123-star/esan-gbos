from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Protocol, runtime_checkable

from .models import (
    ImmutableConflict,
    ProjectionBatch,
    QueryAudit,
    SourceMode,
    StoredProjection,
    ValidationError,
)


@runtime_checkable
class MetricsRepository(Protocol):
    def append_batch(self, batch: ProjectionBatch) -> ProjectionBatch: ...

    def find_projection(
        self,
        *,
        site_id: str,
        metric_key: str,
        source_mode: SourceMode,
        window_start: datetime,
        window_end: datetime,
    ) -> StoredProjection | None: ...

    def checkpoint(self, site_id: str, source_mode: SourceMode) -> str | None: ...

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
    ) -> QueryAudit: ...


class InMemoryMetricsRepository:
    """Process-local repository with the same append-only semantics as PostgreSQL."""

    def __init__(self) -> None:
        self._batches: dict[tuple[str, str], ProjectionBatch] = {}
        self._row_ids: dict[tuple[str, str], tuple[str, object]] = {}
        self._checkpoints: dict[tuple[str, SourceMode], str] = {}
        self._checkpoint_batches: dict[tuple[str, SourceMode, str], str] = {}
        self._audits: dict[tuple[str, str], QueryAudit] = {}
        self._lock = RLock()

    def append_batch(self, batch: ProjectionBatch) -> ProjectionBatch:
        key = (batch.site_id, batch.batch_id)
        mode_key = (batch.site_id, batch.source_mode)
        with self._lock:
            existing = self._batches.get(key)
            if existing is not None:
                if existing != batch:
                    raise ImmutableConflict("batch_id was reused with different immutable content")
                return existing
            if any(
                existing_batch.site_id == batch.site_id
                and existing_batch.source_mode is not batch.source_mode
                for existing_batch in self._batches.values()
            ):
                raise ValidationError(
                    "synthetic and live projection modes are mutually exclusive per site"
                )
            checkpoint_key = (batch.site_id, batch.source_mode, batch.checkpoint)
            if checkpoint_key in self._checkpoint_batches:
                raise ImmutableConflict("checkpoint was already recorded by another batch")
            for row in batch.rows:
                row_key = (batch.site_id, row.row_id)
                if row_key in self._row_ids:
                    raise ImmutableConflict("row_id was already used by another immutable batch")
            self._batches[key] = batch
            for row in batch.rows:
                self._row_ids[(batch.site_id, row.row_id)] = (batch.batch_id, row)
            self._checkpoints[mode_key] = batch.checkpoint
            self._checkpoint_batches[checkpoint_key] = batch.batch_id
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
        with self._lock:
            candidates = [
                StoredProjection(batch=batch, row=row)
                for batch in self._batches.values()
                if batch.site_id == site_id and batch.source_mode is source_mode
                for row in batch.rows
                if row.metric_key == metric_key
                and row.window_start == window_start
                and row.window_end == window_end
            ]
            if not candidates:
                return None
            return max(
                candidates,
                key=lambda item: (item.batch.retrieved_at, item.batch.batch_id, item.row.row_id),
            )

    def checkpoint(self, site_id: str, source_mode: SourceMode) -> str | None:
        with self._lock:
            return self._checkpoints.get((site_id, source_mode))

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
        key = (site_id, audit_id)
        with self._lock:
            existing = self._audits.get(key)
            if existing is not None:
                if existing != audit:
                    raise ImmutableConflict("audit_id was reused with different immutable content")
                return existing
            self._audits[key] = audit
            return audit

    def audits(self, site_id: str) -> tuple[QueryAudit, ...]:
        with self._lock:
            return tuple(audit for audit in self._audits.values() if audit.site_id == site_id)
