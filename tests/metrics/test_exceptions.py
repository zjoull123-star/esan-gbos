from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from services.metrics import (
    MetricDefinition,
    MetricRegistry,
    ProjectionBatch,
    ProjectionPromoter,
    ProjectionRow,
    SourceMode,
    UnavailableReason,
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


def row() -> ProjectionRow:
    return ProjectionRow(
        row_id="row-0001",
        metric_key="sales.order_value",
        definition_version="1.0.0",
        window_start=START,
        window_end=NOW,
        as_of=NOW - timedelta(minutes=5),
        value=Decimal("350.5"),
        included_count=100,
        total_count=100,
        reconciliation_reference="control-total-0001",
        reconciliation_variance=Decimal("0"),
        reconciliation_checked_at=NOW - timedelta(minutes=4),
        source_record_refs=("sales_order-0001",),
        governed=True,
    )


def batch(projection_row: ProjectionRow | None = None) -> ProjectionBatch:
    return ProjectionBatch(
        batch_id="batch-0001",
        site_id="gbos.localhost",
        source_mode=SourceMode.SYNTHETIC,
        checkpoint="checkpoint-0001",
        source_system="kingdee-gate5-synthetic",
        transformation_version="metrics-projection-v1",
        retrieved_at=NOW - timedelta(minutes=5),
        rows=(projection_row or row(),),
    )


def promoter() -> ProjectionPromoter:
    return ProjectionPromoter(registry=MetricRegistry((definition(),)))


def test_promotion_returns_the_same_batch_only_after_every_gate_passes() -> None:
    candidate = batch()

    result = promoter().promote(candidate, evaluated_at=NOW)

    assert result.promoted is True
    assert result.batch is candidate
    assert result.exceptions == ()


@pytest.mark.parametrize(
    ("projection_row", "reason"),
    [
        (replace(row(), governed=False), UnavailableReason.UNGOVERNED_SOURCE),
        (replace(row(), as_of=NOW - timedelta(hours=2)), UnavailableReason.STALE),
        (
            replace(row(), included_count=94, total_count=100),
            UnavailableReason.INSUFFICIENT_COVERAGE,
        ),
        (
            replace(row(), reconciliation_variance=Decimal("0.02")),
            UnavailableReason.RECONCILIATION_FAILED,
        ),
    ],
)
def test_failed_gate_withholds_batch_and_emits_deterministic_exception(
    projection_row: ProjectionRow,
    reason: UnavailableReason,
) -> None:
    first = promoter().promote(batch(projection_row), evaluated_at=NOW)
    second = promoter().promote(batch(projection_row), evaluated_at=NOW)

    assert first == second
    assert first.promoted is False
    assert first.batch is None
    assert len(first.exceptions) == 1
    assert first.exceptions[0].reason is reason
    assert first.exceptions[0].site_id == "gbos.localhost"
    assert first.exceptions[0].metric_key == "sales.order_value"
    with pytest.raises(AttributeError):
        first.exceptions[0].reason = UnavailableReason.STALE  # type: ignore[misc]


def test_policy_precedence_is_governance_then_freshness_then_coverage_then_reconciliation() -> None:
    failed_everything = replace(
        row(),
        governed=False,
        as_of=NOW - timedelta(hours=2),
        included_count=0,
        total_count=100,
        reconciliation_variance=Decimal("99"),
    )

    result = promoter().promote(batch(failed_everything), evaluated_at=NOW)

    assert result.exceptions[0].reason is UnavailableReason.UNGOVERNED_SOURCE


def test_unavailable_response_generates_source_exception_without_a_projection() -> None:
    first = promoter().source_unavailable(
        site_id="gbos.localhost",
        metric_key="sales.order_value",
        source_mode=SourceMode.LIVE,
        request_id="request-live-0001",
        reason_code="live_transport_unavailable",
        detected_at=NOW,
    )
    second = promoter().source_unavailable(
        site_id="gbos.localhost",
        metric_key="sales.order_value",
        source_mode=SourceMode.LIVE,
        request_id="request-live-0001",
        reason_code="live_transport_unavailable",
        detected_at=NOW,
    )

    assert first == second
    assert first.batch is None
    assert first.exceptions[0].reason is UnavailableReason.SOURCE_UNAVAILABLE
    assert first.exceptions[0].source_mode is SourceMode.LIVE


def test_promotion_independently_rejects_a_registry_disabled_source_mode() -> None:
    live_row = row()
    live_batch = ProjectionBatch(
        batch_id="batch-live-0001",
        site_id="gbos.localhost",
        source_mode=SourceMode.LIVE,
        checkpoint="checkpoint-live-0001",
        source_system="kingdee-live-read",
        transformation_version="metrics-projection-v1",
        retrieved_at=NOW - timedelta(minutes=5),
        rows=(live_row,),
    )

    result = promoter().promote(live_batch, evaluated_at=NOW)

    assert result.batch is None
    assert result.exceptions[0].reason is UnavailableReason.UNGOVERNED_SOURCE
