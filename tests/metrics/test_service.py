from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from services.metrics import (
    InMemoryMetricsRepository,
    MetricDefinition,
    MetricQuery,
    MetricRegistry,
    MetricsService,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
    ValidationError,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
START = NOW - timedelta(days=1)


def definition() -> MetricDefinition:
    return MetricDefinition(
        metric_key="sales.order_value",
        definition_version="1.0.0",
        unit="CNY",
        freshness_slo_seconds=3600,
        minimum_coverage=Decimal("0.95"),
        reconciliation_tolerance=Decimal("0.01"),
    )


def row(
    *,
    governed: bool = True,
    definition_version: str = "1.0.0",
    as_of: datetime | None = None,
    included_count: int = 100,
    total_count: int = 100,
    reconciliation_variance: Decimal = Decimal("0.00"),
) -> ProjectionRow:
    return ProjectionRow(
        row_id="row-1",
        metric_key="sales.order_value",
        definition_version=definition_version,
        window_start=START,
        window_end=NOW,
        as_of=as_of or NOW - timedelta(minutes=5),
        value=Decimal("125.50"),
        included_count=included_count,
        total_count=total_count,
        reconciliation_reference="control-total-1",
        reconciliation_variance=reconciliation_variance,
        reconciliation_checked_at=NOW - timedelta(minutes=4),
        source_record_refs=("sales-order-1",),
        governed=governed,
    )


def service_with(
    projection: ProjectionRow | None,
) -> tuple[MetricsService, InMemoryMetricsRepository]:
    repository = InMemoryMetricsRepository()
    if projection is not None:
        repository.append_batch(
            ProjectionBatch(
                batch_id="batch-1",
                site_id="site-a",
                source_mode=SourceMode.SYNTHETIC,
                checkpoint="000001",
                source_system="synthetic_kingdee_projection",
                transformation_version="metrics-projection-v1",
                retrieved_at=NOW - timedelta(minutes=5),
                rows=(projection,),
            )
        )
    return (
        MetricsService(
            registry=MetricRegistry((definition(),)),
            repository=repository,
            source_mode=SourceMode.SYNTHETIC,
        ),
        repository,
    )


def query(metric_key: str = "sales.order_value") -> MetricQuery:
    return MetricQuery(
        site_id="site-a",
        metric_key=metric_key,
        window_start=START,
        window_end=NOW,
        queried_at=NOW,
        request_id="request-1",
    )


def test_available_requires_every_gate_and_includes_lineage_and_mode() -> None:
    service, repository = service_with(row())

    response = service.query(query())

    assert response["status"] == "available"
    assert response["value"] == 125.5
    assert response["unit"] == "CNY"
    assert response["source_mode"] == "synthetic"
    assert response["synthetic"] is True
    assert response["freshness"]["status"] == "fresh"
    assert response["coverage"]["status"] == "sufficient"
    assert response["reconciliation"]["status"] == "passed"
    assert response["source_lineage"][0]["source_record_refs"] == ["sales-order-1"]
    assert repository.audits("site-a")[-1].outcome == "available"


@pytest.mark.parametrize(
    ("projection", "reason"),
    [
        (None, "source_unavailable"),
        (row(governed=False), "ungoverned_source"),
        (row(definition_version="0.9.0"), "definition_unavailable"),
        (row(as_of=NOW - timedelta(hours=2)), "stale"),
        (row(included_count=94, total_count=100), "insufficient_coverage"),
        (row(reconciliation_variance=Decimal("0.02")), "reconciliation_failed"),
    ],
)
def test_fail_closed_policies_remove_value_and_unit(
    projection: ProjectionRow | None,
    reason: str,
) -> None:
    service, repository = service_with(projection)

    response = service.query(query())

    assert response["status"] == "unavailable"
    assert response["unavailable_reason"] == reason
    assert "value" not in response
    assert "unit" not in response
    assert repository.audits("site-a")[-1].reason == reason


def test_deterministic_policy_precedence_is_governance_then_freshness_then_coverage() -> None:
    projection = row(
        governed=False,
        as_of=NOW - timedelta(hours=2),
        included_count=0,
        total_count=100,
        reconciliation_variance=Decimal("99"),
    )
    service, _repository = service_with(projection)

    assert service.query(query())["unavailable_reason"] == "ungoverned_source"


def test_service_never_normalizes_or_executes_unregistered_input() -> None:
    service, repository = service_with(row())

    with pytest.raises(ValidationError, match="registered"):
        service.query(query("sales.order_value; DROP TABLE metrics.projection_rows"))
    assert repository.audits("site-a") == ()
