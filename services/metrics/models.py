from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

_METRIC_KEY = re.compile(r"^[a-z][a-z0-9_.]{2,79}$")
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_SOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
MAX_QUERY_WINDOW = timedelta(days=366)


class ValidationError(ValueError):
    """A metrics definition, projection, or query violates a governed boundary."""


class ImmutableConflict(ValidationError):
    """An immutable identifier was reused with different content."""


class SourceMode(StrEnum):
    SYNTHETIC = "synthetic"
    LIVE = "live"


class UnavailableReason(StrEnum):
    DEFINITION_UNAVAILABLE = "definition_unavailable"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STALE = "stale"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    RECONCILIATION_FAILED = "reconciliation_failed"
    UNGOVERNED_SOURCE = "ungoverned_source"


def require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationError(f"{name} must be UTC timezone-aware")


def require_identifier(value: str, name: str) -> None:
    if not value or len(value) > 256 or value.strip() != value:
        raise ValidationError(f"{name} is required and must not contain outer whitespace")


def require_metric_key(value: str) -> None:
    if _METRIC_KEY.fullmatch(value) is None:
        raise ValidationError("metric_key is not a registered exact governed key")


def require_site_id(value: str) -> None:
    if _SITE_ID.fullmatch(value) is None:
        raise ValidationError("site_id is invalid")


def require_window(start: datetime, end: datetime) -> None:
    require_utc(start, "window_start")
    require_utc(end, "window_end")
    if end < start:
        raise ValidationError("metric window end cannot precede its start")
    if end - start > MAX_QUERY_WINDOW:
        raise ValidationError("metric window exceeds the 366 day maximum")


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricDefinition:
    metric_key: str
    definition_version: str
    unit: str
    freshness_slo_seconds: int
    minimum_coverage: Decimal
    reconciliation_tolerance: Decimal
    window_type: str = "calendar"
    window_grain: str = "day"
    display_name: str | None = None

    def __post_init__(self) -> None:
        require_metric_key(self.metric_key)
        require_identifier(self.definition_version, "definition_version")
        require_identifier(self.unit, "unit")
        if self.display_name is not None:
            require_identifier(self.display_name, "display_name")
            if len(self.display_name) > 160:
                raise ValidationError("display_name exceeds 160 characters")
        if self.freshness_slo_seconds < 1:
            raise ValidationError("freshness_slo_seconds must be positive")
        if not Decimal(0) <= self.minimum_coverage <= Decimal(1):
            raise ValidationError("minimum_coverage must be between 0 and 1")
        if self.reconciliation_tolerance < 0:
            raise ValidationError("reconciliation_tolerance must be non-negative")
        if self.window_type not in {"rolling", "calendar", "point_in_time"}:
            raise ValidationError("window_type is not governed")
        if self.window_grain not in {
            "hour",
            "day",
            "week",
            "month",
            "quarter",
            "year",
            "instant",
        }:
            raise ValidationError("window_grain is not governed")


class MetricRegistry:
    """Immutable exact-key registry; it deliberately performs no normalization."""

    def __init__(
        self,
        definitions: tuple[MetricDefinition, ...],
        *,
        enabled_modes: frozenset[SourceMode] = frozenset({SourceMode.SYNTHETIC}),
    ) -> None:
        if not definitions:
            raise ValidationError("metric registry cannot be empty")
        keyed = {definition.metric_key: definition for definition in definitions}
        if len(keyed) != len(definitions):
            raise ValidationError("metric registry keys must be unique")
        if len(enabled_modes) != 1:
            raise ValidationError("exactly one registry source mode must be enabled")
        self._definitions = keyed
        self._enabled_modes = enabled_modes

    @property
    def metric_keys(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @property
    def enabled_modes(self) -> frozenset[SourceMode]:
        return self._enabled_modes

    def require(self, metric_key: str) -> MetricDefinition:
        try:
            return self._definitions[metric_key]
        except KeyError as exc:
            raise ValidationError("metric_key is not registered") from exc

    @classmethod
    def from_document(cls, document: object) -> MetricRegistry:
        if not isinstance(document, dict) or set(document) - {
            "registry_version",
            "gate",
            "official_interface",
            "runtime_enabled",
            "synthetic_only",
            "synthetic_mode_enabled",
            "live_mode_enabled",
            "llm_calculation_allowed",
            "arbitrary_query_allowed",
            "allowed_query_keys",
            "source_mode",
            "metrics",
        }:
            raise ValidationError("metric registry contains unsupported top-level fields")
        if document.get("runtime_enabled") is not True:
            raise ValidationError("metric registry runtime is disabled")
        if document.get("arbitrary_query_allowed") is not False:
            raise ValidationError("metric registry must disable arbitrary queries")
        allowed_query_keys = document.get("allowed_query_keys")
        if allowed_query_keys is not None and allowed_query_keys != [
            "metric_key",
            "window_start",
            "window_end",
        ]:
            raise ValidationError("metric registry query keys are not the governed exact set")
        if (
            document.get("synthetic_mode_enabled") is True
            and document.get("live_mode_enabled") is True
        ):
            raise ValidationError("synthetic and live registry modes are mutually exclusive")
        raw_metrics = document.get("metrics")
        if not isinstance(raw_metrics, list):
            raise ValidationError("metric registry metrics must be a list")
        definitions: list[MetricDefinition] = []
        for item in raw_metrics:
            if not isinstance(item, dict):
                raise ValidationError("metric registry entries must be objects")
            window = item.get("window", {})
            reconciliation = item.get("reconciliation", {})
            if not isinstance(window, dict) or not isinstance(reconciliation, dict):
                raise ValidationError("registry window and reconciliation must be objects")
            coverage = item.get(
                "minimum_coverage",
                item.get("coverage_threshold", item.get("minimum_coverage_ratio", 1)),
            )
            definitions.append(
                MetricDefinition(
                    metric_key=str(item.get("metric_key", "")),
                    definition_version=str(item.get("definition_version", "")),
                    unit=str(item.get("unit", "")),
                    display_name=str(item.get("display_name", item.get("metric_key", ""))),
                    freshness_slo_seconds=int(item.get("freshness_slo_seconds", 0)),
                    minimum_coverage=Decimal(str(coverage)),
                    reconciliation_tolerance=Decimal(
                        str(
                            item.get(
                                "reconciliation_tolerance",
                                reconciliation.get("tolerance", 0),
                            )
                        )
                    ),
                    window_type=str(window.get("type", "calendar")),
                    window_grain=str(window.get("grain", "day")),
                )
            )
        synthetic_enabled = document.get(
            "synthetic_mode_enabled", document.get("synthetic_only", True)
        )
        live_enabled = document.get("live_mode_enabled", False)
        enabled_modes = frozenset(
            mode
            for mode, enabled in (
                (SourceMode.SYNTHETIC, synthetic_enabled),
                (SourceMode.LIVE, live_enabled),
            )
            if enabled is True
        )
        return cls(tuple(definitions), enabled_modes=enabled_modes)


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricQuery:
    site_id: str
    metric_key: str
    window_start: datetime
    window_end: datetime
    queried_at: datetime
    request_id: str

    def __post_init__(self) -> None:
        require_site_id(self.site_id)
        require_metric_key(self.metric_key)
        require_window(self.window_start, self.window_end)
        require_utc(self.queried_at, "queried_at")
        require_identifier(self.request_id, "request_id")
        if self.window_start > self.queried_at:
            raise ValidationError("metric window cannot start after queried_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionRow:
    row_id: str
    metric_key: str
    window_start: datetime
    window_end: datetime
    as_of: datetime
    value: Decimal
    included_count: int
    total_count: int
    reconciliation_reference: str
    reconciliation_variance: Decimal
    reconciliation_checked_at: datetime
    source_record_refs: tuple[str, ...]
    governed: bool
    definition_version: str = "1.0.0"

    def __post_init__(self) -> None:
        require_identifier(self.row_id, "row_id")
        require_metric_key(self.metric_key)
        require_window(self.window_start, self.window_end)
        require_utc(self.as_of, "as_of")
        require_utc(self.reconciliation_checked_at, "reconciliation_checked_at")
        require_identifier(self.definition_version, "definition_version")
        require_identifier(self.reconciliation_reference, "reconciliation_reference")
        if not self.value.is_finite() or not self.reconciliation_variance.is_finite():
            raise ValidationError("metric and reconciliation values must be finite")
        if self.included_count < 0 or self.total_count < 0:
            raise ValidationError("coverage counts must be non-negative")
        if self.included_count > self.total_count:
            raise ValidationError("included_count cannot exceed total_count")
        if not self.source_record_refs or len(set(self.source_record_refs)) != len(
            self.source_record_refs
        ):
            raise ValidationError("source_record_refs must be non-empty and unique")
        for source_ref in self.source_record_refs:
            if _SOURCE_REF.fullmatch(source_ref) is None:
                raise ValidationError("source_record_ref is invalid")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionBatch:
    batch_id: str
    site_id: str
    source_mode: SourceMode
    checkpoint: str
    source_system: str
    transformation_version: str
    retrieved_at: datetime
    rows: tuple[ProjectionRow, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("batch_id", self.batch_id),
            ("checkpoint", self.checkpoint),
            ("source_system", self.source_system),
            ("transformation_version", self.transformation_version),
        ):
            require_identifier(value, name)
        require_site_id(self.site_id)
        require_utc(self.retrieved_at, "retrieved_at")
        if not self.rows:
            raise ValidationError("projection batch must contain rows")
        if len({row.row_id for row in self.rows}) != len(self.rows):
            raise ValidationError("projection row ids must be unique within a batch")
        source_is_synthetic = "synthetic" in self.source_system.lower()
        if source_is_synthetic != (self.source_mode is SourceMode.SYNTHETIC):
            raise ValidationError("synthetic and live projection modes are mutually exclusive")


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredProjection:
    batch: ProjectionBatch
    row: ProjectionRow


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryAudit:
    site_id: str
    audit_id: str
    request_id: str
    metric_key: str
    source_mode: SourceMode
    window_start: datetime
    window_end: datetime
    queried_at: datetime
    outcome: str
    reason: str | None
    batch_id: str | None
    row_id: str | None

    def __post_init__(self) -> None:
        require_site_id(self.site_id)
        require_identifier(self.audit_id, "audit_id")
        require_identifier(self.request_id, "request_id")
        require_metric_key(self.metric_key)
        require_window(self.window_start, self.window_end)
        require_utc(self.queried_at, "queried_at")
        if self.outcome not in {"available", "unavailable"}:
            raise ValidationError("query audit outcome is invalid")
        if (self.outcome == "available") != (self.reason is None):
            raise ValidationError("query audit reason must match outcome")


def utc_iso(value: datetime) -> str:
    require_utc(value, "timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
