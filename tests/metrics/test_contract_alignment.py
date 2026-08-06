from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from services.metrics import (
    InMemoryMetricsRepository,
    MetricQuery,
    MetricRegistry,
    MetricsService,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
    ValidationError,
)

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


def document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_runtime_registry_loads_exact_gate5_contract_fields() -> None:
    registry = MetricRegistry.from_document(
        document(ROOT / "contracts" / "gate5" / "metrics-registry-v1.json")
    )

    sales = registry.require("sales.order_value")
    assert sales.display_name == "销售订单金额"
    assert sales.minimum_coverage == Decimal("1")
    assert sales.reconciliation_tolerance == Decimal("0.01")
    assert sales.window_grain == "month"


def test_available_runtime_response_validates_gate5_schema() -> None:
    registry = MetricRegistry.from_document(
        document(ROOT / "contracts" / "gate5" / "metrics-registry-v1.json")
    )
    repository = InMemoryMetricsRepository()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)
    row = ProjectionRow(
        row_id="sales-row-1",
        metric_key="sales.order_value",
        definition_version="1.0.0",
        window_start=start,
        window_end=end,
        as_of=NOW - timedelta(minutes=5),
        value=Decimal("20600"),
        included_count=2,
        total_count=2,
        reconciliation_reference="recon-sales-1",
        reconciliation_variance=Decimal("0"),
        reconciliation_checked_at=NOW - timedelta(minutes=4),
        source_record_refs=("sales_order-synthetic-0001",),
        governed=True,
    )
    repository.append_batch(
        ProjectionBatch(
            batch_id="sales-batch-1",
            site_id="gbos.localhost",
            source_mode=SourceMode.SYNTHETIC,
            checkpoint="000001",
            source_system="kingdee-gate5-synthetic",
            transformation_version="metrics-projection-v1",
            retrieved_at=NOW - timedelta(minutes=5),
            rows=(row,),
        )
    )
    response = MetricsService(
        registry=registry,
        repository=repository,
        source_mode=SourceMode.SYNTHETIC,
    ).query(
        MetricQuery(
            site_id="gbos.localhost",
            metric_key="sales.order_value",
            window_start=start,
            window_end=end,
            queried_at=NOW,
            request_id="request-1",
        )
    )

    schema = document(ROOT / "contracts" / "gate5" / "metric-response.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response)
    assert response["governed_sources"] is True
    assert response["display_name"] == "销售订单金额"


def test_point_in_time_registry_metric_allows_exact_instant_window() -> None:
    registry = MetricRegistry.from_document(
        document(ROOT / "contracts" / "gate5" / "metrics-registry-v1.json")
    )
    query = MetricQuery(
        site_id="gbos.localhost",
        metric_key="inventory.on_hand_quantity",
        window_start=NOW,
        window_end=NOW,
        queried_at=NOW,
        request_id="request-point-1",
    )

    response = MetricsService(
        registry=registry,
        repository=InMemoryMetricsRepository(),
        source_mode=SourceMode.SYNTHETIC,
    ).query(query)

    assert response["window"]["type"] == "point_in_time"
    assert response["window"]["start"] == response["window"]["end"]


def test_runtime_refuses_a_source_mode_disabled_by_the_registry() -> None:
    registry = MetricRegistry.from_document(
        document(ROOT / "contracts" / "gate5" / "metrics-registry-v1.json")
    )

    with pytest.raises(ValidationError, match="source mode"):
        MetricsService(
            registry=registry,
            repository=InMemoryMetricsRepository(),
            source_mode=SourceMode.LIVE,
        )


def test_runtime_refuses_a_disabled_registry() -> None:
    registry_document = deepcopy(
        document(ROOT / "contracts" / "gate5" / "metrics-registry-v1.json")
    )
    registry_document["runtime_enabled"] = False

    with pytest.raises(ValidationError, match="runtime"):
        MetricRegistry.from_document(registry_document)
