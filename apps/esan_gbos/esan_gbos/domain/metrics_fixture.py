from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

_DASHBOARD_KEYS = {
    "schema_version",
    "site_id",
    "source_mode",
    "synthetic",
    "generated_at",
    "metrics",
}
_METRIC_KEYS = {
    "schema_version",
    "metric_key",
    "display_name",
    "definition_version",
    "site_id",
    "status",
    "value",
    "unit",
    "unavailable_reason",
    "as_of",
    "queried_at",
    "window",
    "freshness",
    "coverage",
    "reconciliation",
    "source_lineage",
    "source_mode",
    "synthetic",
    "governed_sources",
}
_UNAVAILABLE_REASONS = {
    "stale",
    "insufficient_coverage",
    "reconciliation_failed",
    "source_unavailable",
    "definition_unavailable",
    "ungoverned_source",
}


class MetricsFixtureError(ValueError):
    """Raised when a local Gate 5 dashboard could be mistaken for governed live data."""


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MetricsFixtureError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MetricsFixtureError(f"{field} must be a nonempty string")
    return value


def _validate_metric(
    raw: object,
    *,
    site_id: str,
    source_mode: str,
    synthetic: bool,
) -> dict[str, Any]:
    metric = dict(_mapping(raw, "metric"))
    if set(metric) - _METRIC_KEYS:
        raise MetricsFixtureError("metric contains unknown fields")
    required = _METRIC_KEYS - {"value", "unit", "unavailable_reason"}
    if required - set(metric):
        raise MetricsFixtureError("metric is missing required fields")
    if metric["schema_version"] != "3.0":
        raise MetricsFixtureError("metric schema_version must be 3.0")
    if metric["site_id"] != site_id:
        raise MetricsFixtureError("metric site does not match dashboard")
    if metric["source_mode"] != source_mode or metric["synthetic"] is not synthetic:
        raise MetricsFixtureError("metric source mode does not match dashboard")
    for field in ("metric_key", "display_name", "definition_version", "as_of", "queried_at"):
        _text(metric[field], field)

    freshness = _mapping(metric["freshness"], "freshness")
    coverage = _mapping(metric["coverage"], "coverage")
    reconciliation = _mapping(metric["reconciliation"], "reconciliation")
    lineage = metric["source_lineage"]
    if not isinstance(lineage, list) or not lineage:
        raise MetricsFixtureError("source_lineage must be nonempty")
    for item in lineage:
        source = _mapping(item, "source_lineage item")
        if synthetic and source.get("evidence_status") != "synthetic":
            raise MetricsFixtureError("synthetic metrics require synthetic lineage")

    status = metric["status"]
    if status == "available":
        value = metric.get("value")
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise MetricsFixtureError("available metric requires a numeric value")
        _text(metric.get("unit"), "unit")
        if "unavailable_reason" in metric:
            raise MetricsFixtureError("available metric cannot have unavailable_reason")
        if (
            freshness.get("status") != "fresh"
            or coverage.get("status") != "sufficient"
            or reconciliation.get("status") != "passed"
            or metric["governed_sources"] is not True
        ):
            raise MetricsFixtureError("available metric failed a quality gate")
    elif status == "unavailable":
        if metric.get("unavailable_reason") not in _UNAVAILABLE_REASONS:
            raise MetricsFixtureError("unavailable metric requires a governed reason")
        if "value" in metric or "unit" in metric:
            raise MetricsFixtureError("unavailable metric cannot expose value or unit")
    else:
        raise MetricsFixtureError("metric status is invalid")
    return metric


def validate_dashboard(raw: object) -> dict[str, Any]:
    dashboard = dict(_mapping(raw, "dashboard"))
    if set(dashboard) != _DASHBOARD_KEYS:
        raise MetricsFixtureError("dashboard fields do not match the closed contract")
    if dashboard["schema_version"] != "3.0":
        raise MetricsFixtureError("dashboard schema_version must be 3.0")
    site_id = _text(dashboard["site_id"], "site_id")
    _text(dashboard["generated_at"], "generated_at")
    source_mode = dashboard["source_mode"]
    synthetic = dashboard["synthetic"]
    if source_mode not in {"synthetic", "live"} or not isinstance(synthetic, bool):
        raise MetricsFixtureError("dashboard source mode is invalid")
    if (source_mode == "synthetic") is not synthetic:
        raise MetricsFixtureError("source_mode and synthetic disagree")
    metrics = dashboard["metrics"]
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= 20:
        raise MetricsFixtureError("dashboard metrics must contain 1 to 20 items")
    normalized = [
        _validate_metric(
            metric,
            site_id=site_id,
            source_mode=source_mode,
            synthetic=synthetic,
        )
        for metric in metrics
    ]
    keys = [str(metric["metric_key"]) for metric in normalized]
    if len(keys) != len(set(keys)):
        raise MetricsFixtureError("metric keys must be unique")
    dashboard["metrics"] = normalized
    return copy.deepcopy(dashboard)
