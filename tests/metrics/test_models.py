from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.metrics import (
    MetricDefinition,
    MetricQuery,
    MetricRegistry,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
    ValidationError,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


def definition(metric_key: str = "sales.order_value") -> MetricDefinition:
    return MetricDefinition(
        metric_key=metric_key,
        definition_version="1.0.0",
        unit="CNY",
        freshness_slo_seconds=3600,
        minimum_coverage=Decimal("0.95"),
        reconciliation_tolerance=Decimal("0.01"),
    )


def test_registry_requires_exact_known_metric_keys() -> None:
    registry = MetricRegistry((definition(),))

    assert registry.require("sales.order_value").unit == "CNY"
    for key in ("Sales.Order_Value", "sales.order_value ", "sales/order_value", "unknown.metric"):
        with pytest.raises(ValidationError, match="registered"):
            registry.require(key)


def test_query_requires_strict_utc_bounded_half_open_window() -> None:
    valid = MetricQuery(
        site_id="site-a",
        metric_key="sales.order_value",
        window_start=NOW - timedelta(days=31),
        window_end=NOW,
        queried_at=NOW,
        request_id="request-1",
    )
    assert valid.window_end - valid.window_start == timedelta(days=31)

    invalid_windows = (
        (NOW.replace(tzinfo=None), NOW),
        (NOW.astimezone(timezone(timedelta(hours=8))), NOW),
        (NOW, NOW - timedelta(seconds=1)),
        (NOW - timedelta(days=367), NOW),
    )
    for start, end in invalid_windows:
        with pytest.raises(ValidationError):
            MetricQuery(
                site_id="site-a",
                metric_key="sales.order_value",
                window_start=start,
                window_end=end,
                queried_at=NOW,
                request_id="request-1",
            )


def test_projection_batch_mode_is_single_and_rows_are_immutable() -> None:
    row = ProjectionRow(
        row_id="row-1",
        metric_key="sales.order_value",
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        as_of=NOW,
        value=Decimal("125.50"),
        included_count=10,
        total_count=10,
        reconciliation_reference="control-total-1",
        reconciliation_variance=Decimal("0.00"),
        reconciliation_checked_at=NOW,
        source_record_refs=("sales-order-1",),
        governed=True,
    )
    batch = ProjectionBatch(
        batch_id="batch-1",
        site_id="site-a",
        source_mode=SourceMode.SYNTHETIC,
        checkpoint="checkpoint-1",
        source_system="synthetic_kingdee_projection",
        transformation_version="metrics-projection-v1",
        retrieved_at=NOW,
        rows=(row,),
    )

    assert batch.rows == (row,)
    with pytest.raises(AttributeError):
        batch.batch_id = "changed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        row.value = Decimal("0")  # type: ignore[misc]

    with pytest.raises(ValidationError, match="synthetic"):
        ProjectionBatch(
            batch_id="batch-live",
            site_id="site-a",
            source_mode=SourceMode.LIVE,
            checkpoint="checkpoint-2",
            source_system="synthetic_kingdee_projection",
            transformation_version="metrics-projection-v1",
            retrieved_at=NOW,
            rows=(row,),
        )
