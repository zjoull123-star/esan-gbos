from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import (
    MetricDefinition,
    MetricRegistry,
    ProjectionBatch,
    SourceMode,
    UnavailableReason,
    ValidationError,
    require_identifier,
    require_metric_key,
    require_site_id,
    require_utc,
    utc_iso,
)

_EXCEPTION_REASONS = frozenset(
    {
        UnavailableReason.STALE,
        UnavailableReason.INSUFFICIENT_COVERAGE,
        UnavailableReason.RECONCILIATION_FAILED,
        UnavailableReason.SOURCE_UNAVAILABLE,
        UnavailableReason.UNGOVERNED_SOURCE,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricException:
    exception_id: str
    reason: UnavailableReason
    site_id: str
    metric_key: str
    source_mode: SourceMode
    detected_at: datetime
    batch_id: str | None
    row_id: str | None
    details: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.exception_id, "exception_id")
        require_site_id(self.site_id)
        require_metric_key(self.metric_key)
        require_utc(self.detected_at, "detected_at")
        if self.reason not in _EXCEPTION_REASONS:
            raise ValidationError("metric exception reason is not a deterministic Gate 5 policy")
        if (self.batch_id is None) != (self.row_id is None):
            raise ValidationError("metric exception batch_id and row_id must be supplied together")
        if self.batch_id is not None:
            require_identifier(self.batch_id, "batch_id")
            require_identifier(self.row_id or "", "row_id")
        if len({key for key, _value in self.details}) != len(self.details):
            raise ValidationError("metric exception detail keys must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class PromotionResult:
    batch: ProjectionBatch | None
    exceptions: tuple[MetricException, ...]

    def __post_init__(self) -> None:
        if (self.batch is None) != bool(self.exceptions):
            raise ValidationError("promotion must contain either a batch or exceptions")

    @property
    def promoted(self) -> bool:
        return self.batch is not None


class ProjectionPromoter:
    """Deterministic quality gates; it persists nothing and performs no I/O."""

    def __init__(self, *, registry: MetricRegistry) -> None:
        self._registry = registry

    def promote(self, batch: ProjectionBatch, *, evaluated_at: datetime) -> PromotionResult:
        require_utc(evaluated_at, "evaluated_at")
        exceptions: list[MetricException] = []
        for row in batch.rows:
            definition = self._registry.require(row.metric_key)
            reason = (
                UnavailableReason.UNGOVERNED_SOURCE
                if batch.source_mode not in self._registry.enabled_modes
                else self._reason(definition, row=row, evaluated_at=evaluated_at)
            )
            if reason is not None:
                exceptions.append(
                    self._exception(
                        reason=reason,
                        site_id=batch.site_id,
                        metric_key=row.metric_key,
                        source_mode=batch.source_mode,
                        detected_at=evaluated_at,
                        batch_id=batch.batch_id,
                        row_id=row.row_id,
                        details=(
                            ("checkpoint", batch.checkpoint),
                            ("included_count", str(row.included_count)),
                            ("total_count", str(row.total_count)),
                            ("reconciliation_variance", str(row.reconciliation_variance)),
                        ),
                    )
                )
        if exceptions:
            return PromotionResult(batch=None, exceptions=tuple(exceptions))
        return PromotionResult(batch=batch, exceptions=())

    def source_unavailable(
        self,
        *,
        site_id: str,
        metric_key: str,
        source_mode: SourceMode,
        request_id: str,
        reason_code: str,
        detected_at: datetime,
    ) -> PromotionResult:
        self._registry.require(metric_key)
        require_identifier(request_id, "request_id")
        require_identifier(reason_code, "reason_code")
        exception = self._exception(
            reason=UnavailableReason.SOURCE_UNAVAILABLE,
            site_id=site_id,
            metric_key=metric_key,
            source_mode=source_mode,
            detected_at=detected_at,
            batch_id=None,
            row_id=None,
            details=(("reason_code", reason_code), ("request_id", request_id)),
        )
        return PromotionResult(batch=None, exceptions=(exception,))

    @staticmethod
    def _reason(
        definition: MetricDefinition, *, row: object, evaluated_at: datetime
    ) -> UnavailableReason | None:
        from .models import ProjectionRow

        if not isinstance(row, ProjectionRow):
            raise TypeError("row must be a ProjectionRow")
        age_seconds = max(0, math.ceil((evaluated_at - row.as_of).total_seconds()))
        coverage = (
            Decimal(row.included_count) / Decimal(row.total_count)
            if row.total_count
            else Decimal(0)
        )
        if not row.governed or row.definition_version != definition.definition_version:
            return UnavailableReason.UNGOVERNED_SOURCE
        if row.as_of > evaluated_at or age_seconds > definition.freshness_slo_seconds:
            return UnavailableReason.STALE
        if coverage < definition.minimum_coverage:
            return UnavailableReason.INSUFFICIENT_COVERAGE
        if (
            row.reconciliation_checked_at > evaluated_at
            or abs(row.reconciliation_variance) > definition.reconciliation_tolerance
        ):
            return UnavailableReason.RECONCILIATION_FAILED
        return None

    @staticmethod
    def _exception(
        *,
        reason: UnavailableReason,
        site_id: str,
        metric_key: str,
        source_mode: SourceMode,
        detected_at: datetime,
        batch_id: str | None,
        row_id: str | None,
        details: tuple[tuple[str, str], ...],
    ) -> MetricException:
        canonical_details = tuple(sorted(details))
        identity = {
            "reason": reason.value,
            "site_id": site_id,
            "metric_key": metric_key,
            "source_mode": source_mode.value,
            "detected_at": utc_iso(detected_at),
            "batch_id": batch_id,
            "row_id": row_id,
            "details": canonical_details,
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
        return MetricException(
            exception_id=f"metric-exception-{digest}",
            reason=reason,
            site_id=site_id,
            metric_key=metric_key,
            source_mode=source_mode,
            detected_at=detected_at,
            batch_id=batch_id,
            row_id=row_id,
            details=canonical_details,
        )
