from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .models import (
    MetricDefinition,
    MetricQuery,
    MetricRegistry,
    SourceMode,
    StoredProjection,
    UnavailableReason,
    ValidationError,
    utc_iso,
)
from .repository import MetricsRepository


class MetricsService:
    def __init__(
        self,
        *,
        registry: MetricRegistry,
        repository: MetricsRepository,
        source_mode: SourceMode,
    ) -> None:
        if source_mode not in registry.enabled_modes:
            raise ValidationError("source mode is disabled by the governed registry")
        self._registry = registry
        self._repository = repository
        self._source_mode = source_mode

    @property
    def registry(self) -> MetricRegistry:
        return self._registry

    @property
    def source_mode(self) -> SourceMode:
        return self._source_mode

    def query(self, query: MetricQuery) -> dict[str, Any]:
        definition = self._registry.require(query.metric_key)
        is_instant = query.window_start == query.window_end
        if (definition.window_type == "point_in_time") != is_instant:
            raise ValidationError("query window does not match the registered window type")
        projection = self._repository.find_projection(
            site_id=query.site_id,
            metric_key=query.metric_key,
            source_mode=self._source_mode,
            window_start=query.window_start,
            window_end=query.window_end,
        )
        response, reason = self._evaluate(definition, query, projection)
        self._repository.append_audit(
            site_id=query.site_id,
            audit_id=f"metrics-query-{uuid4().hex}",
            request_id=query.request_id,
            metric_key=query.metric_key,
            source_mode=self._source_mode,
            window_start=query.window_start,
            window_end=query.window_end,
            queried_at=query.queried_at,
            outcome=str(response["status"]),
            reason=reason.value if reason else None,
            batch_id=projection.batch.batch_id if projection else None,
            row_id=projection.row.row_id if projection else None,
        )
        return response

    def _evaluate(
        self,
        definition: MetricDefinition,
        query: MetricQuery,
        projection: StoredProjection | None,
    ) -> tuple[dict[str, Any], UnavailableReason | None]:
        if projection is None:
            return self._unavailable(definition, query, None, UnavailableReason.SOURCE_UNAVAILABLE)
        row = projection.row
        age_seconds = max(
            0,
            math.ceil((query.queried_at - row.as_of).total_seconds()),
        )
        coverage_ratio = (
            Decimal(row.included_count) / Decimal(row.total_count)
            if row.total_count
            else Decimal(0)
        )
        reconciled = (
            abs(row.reconciliation_variance) <= definition.reconciliation_tolerance
            and row.reconciliation_checked_at <= query.queried_at
        )
        # This order is policy, not an accident: governance outranks data quality signals.
        reason: UnavailableReason | None = None
        if not row.governed:
            reason = UnavailableReason.UNGOVERNED_SOURCE
        elif row.definition_version != definition.definition_version:
            reason = UnavailableReason.DEFINITION_UNAVAILABLE
        elif row.as_of > query.queried_at or age_seconds > definition.freshness_slo_seconds:
            reason = UnavailableReason.STALE
        elif coverage_ratio < definition.minimum_coverage:
            reason = UnavailableReason.INSUFFICIENT_COVERAGE
        elif not reconciled:
            reason = UnavailableReason.RECONCILIATION_FAILED

        base = self._base_response(
            definition,
            query,
            projection,
            age_seconds=age_seconds,
            coverage_ratio=coverage_ratio,
            reconciled=reconciled,
        )
        if reason is not None:
            base.update(status="unavailable", unavailable_reason=reason.value)
            return base, reason
        base.update(status="available", value=float(row.value), unit=definition.unit)
        return base, None

    def _unavailable(
        self,
        definition: MetricDefinition,
        query: MetricQuery,
        projection: StoredProjection | None,
        reason: UnavailableReason,
    ) -> tuple[dict[str, Any], UnavailableReason]:
        base = self._base_response(
            definition,
            query,
            projection,
            age_seconds=0,
            coverage_ratio=Decimal(0),
            reconciled=False,
        )
        base.update(status="unavailable", unavailable_reason=reason.value)
        return base, reason

    def _base_response(
        self,
        definition: MetricDefinition,
        query: MetricQuery,
        projection: StoredProjection | None,
        *,
        age_seconds: int,
        coverage_ratio: Decimal,
        reconciled: bool,
    ) -> dict[str, Any]:
        source_refs: tuple[str, ...]
        if projection is None:
            as_of = query.queried_at
            retrieved_at = query.queried_at
            source_system = f"{self._source_mode.value}_projection_unavailable"
            transformation = "metrics-projection-v1"
            request_digest = hashlib.sha256(query.request_id.encode()).hexdigest()[:24]
            source_refs = (f"unavailable-{request_digest}",)
            included_count = total_count = 0
            checked_at = query.queried_at
            reference = "source-unavailable"
            variance = 0.0
        else:
            batch, row = projection.batch, projection.row
            as_of = row.as_of
            retrieved_at = batch.retrieved_at
            source_system = batch.source_system
            transformation = batch.transformation_version
            source_refs = row.source_record_refs
            included_count, total_count = row.included_count, row.total_count
            checked_at = row.reconciliation_checked_at
            reference = row.reconciliation_reference
            variance = float(row.reconciliation_variance)
        freshness_status = (
            "fresh"
            if projection is not None
            and projection.row.as_of <= query.queried_at
            and age_seconds <= definition.freshness_slo_seconds
            else ("stale" if projection is not None else "unknown")
        )
        coverage_status = (
            "sufficient"
            if projection is not None and coverage_ratio >= definition.minimum_coverage
            else ("insufficient" if projection is not None else "unknown")
        )
        reconciliation_status = (
            "passed" if reconciled else ("failed" if projection is not None else "not_run")
        )
        reconciliation: dict[str, Any] = {
            "status": reconciliation_status,
            "checked_at": utc_iso(checked_at),
            "reference": reference,
        }
        reconciliation["variance"] = variance
        return {
            "schema_version": "3.0",
            "metric_key": definition.metric_key,
            "display_name": definition.display_name or definition.metric_key,
            "definition_version": definition.definition_version,
            "site_id": query.site_id,
            "as_of": utc_iso(as_of),
            "queried_at": utc_iso(query.queried_at),
            "window": {
                "type": definition.window_type,
                "grain": definition.window_grain,
                "start": utc_iso(query.window_start),
                "end": utc_iso(query.window_end),
            },
            "freshness": {
                "status": freshness_status,
                "age_seconds": age_seconds,
                "slo_seconds": definition.freshness_slo_seconds,
            },
            "coverage": {
                "status": coverage_status,
                "ratio": float(coverage_ratio),
                "included_count": included_count,
                "total_count": total_count,
            },
            "reconciliation": reconciliation,
            "source_lineage": [
                {
                    "source_system": source_system,
                    "source_record_refs": list(source_refs),
                    "retrieved_at": utc_iso(retrieved_at),
                    "transformation_version": transformation,
                    "evidence_status": (
                        "synthetic" if self._source_mode is SourceMode.SYNTHETIC else "verified"
                    ),
                }
            ],
            "source_mode": self._source_mode.value,
            "synthetic": self._source_mode is SourceMode.SYNTHETIC,
            "governed_sources": bool(
                projection is not None
                and projection.row.governed
                and projection.row.definition_version == definition.definition_version
            ),
        }
