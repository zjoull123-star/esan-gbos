from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from services.metrics import (
    InMemoryMetricsRepository,
    MetricDefinition,
    MetricRegistry,
    MetricsService,
    SourceMode,
    create_metrics_app,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


def client() -> TestClient:
    service = MetricsService(
        registry=MetricRegistry(
            (
                MetricDefinition(
                    metric_key="sales.order_value",
                    definition_version="1.0.0",
                    unit="CNY",
                    freshness_slo_seconds=3600,
                    minimum_coverage=Decimal("0.95"),
                    reconciliation_tolerance=Decimal("0.01"),
                ),
            )
        ),
        repository=InMemoryMetricsRepository(),
        source_mode=SourceMode.SYNTHETIC,
    )
    return TestClient(create_metrics_app(service=service, clock=lambda: NOW))


def headers() -> dict[str, str]:
    return {"X-Site-ID": "site-a", "X-Request-ID": "request-1"}


def params() -> dict[str, str]:
    return {
        "window_start": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "window_end": NOW.isoformat().replace("+00:00", "Z"),
    }


def test_http_surface_only_accepts_registry_key_and_bounded_window() -> None:
    response = client().get("/v1/metrics/sales.order_value", headers=headers(), params=params())

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["unavailable_reason"] == "source_unavailable"


def test_http_surface_rejects_unknown_query_fields_and_unknown_keys() -> None:
    broad = params() | {"sql": "select 1", "dimension": "customer", "source_url": "x"}
    assert (
        client().get("/v1/metrics/sales.order_value", headers=headers(), params=broad).status_code
        == 400
    )
    assert (
        client().get("/v1/metrics/unknown.metric", headers=headers(), params=params()).status_code
        == 404
    )


def test_http_surface_rejects_duplicate_window_parameters() -> None:
    duplicate = [
        ("window_start", params()["window_start"]),
        ("window_start", params()["window_start"]),
        ("window_end", params()["window_end"]),
    ]

    assert (
        client()
        .get("/v1/metrics/sales.order_value", headers=headers(), params=duplicate)
        .status_code
        == 400
    )


def test_http_surface_requires_site_request_and_strict_utc() -> None:
    assert client().get("/v1/metrics/sales.order_value", params=params()).status_code == 400
    local_time = params() | {"window_end": "2026-08-07T16:00:00+08:00"}
    assert (
        client()
        .get("/v1/metrics/sales.order_value", headers=headers(), params=local_time)
        .status_code
        == 422
    )
