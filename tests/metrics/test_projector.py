from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

import pytest

from services.kingdee_adapter import (
    AdapterResponse,
    AdapterStatus,
    ControlMetrics,
    VerificationSnapshot,
    VerificationStatus,
)
from services.metrics import (
    METRIC_RECIPES,
    MetricRegistry,
    MetricsProjector,
    ProjectionInputs,
    ProjectionRejected,
    SourceMode,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 9, 1, tzinfo=UTC)


def registry() -> MetricRegistry:
    from json import loads
    from pathlib import Path

    path = Path(__file__).parents[2] / "contracts" / "gate5" / "metrics-registry-v1.json"
    return MetricRegistry.from_document(loads(path.read_text(encoding="utf-8")))


def response(
    *,
    logical_object: str = "sales_order",
    tool_name: str = "kingdee.sales_order.get",
    field: str = "total_amount",
    values: tuple[int | float, ...] = (100, 250.5),
    synthetic: bool = True,
    status: AdapterStatus = AdapterStatus.AVAILABLE,
) -> AdapterResponse:
    exact_fields: dict[str, dict[str, object]] = {
        "sales_order": {
            "order_number": "SO-0001",
            "customer_number": "C-0001",
            "order_date": "2026-08-07",
            "total_amount": 0,
        },
        "inventory": {
            "material_number": "M-0001",
            "warehouse_number": "W-0001",
            "base_quantity": 0,
        },
        "receivable": {
            "bill_number": "AR-0001",
            "customer_number": "C-0001",
            "due_date": "2026-08-31",
            "open_amount": 0,
        },
    }
    rows = tuple(
        {
            "record_ref": f"{logical_object}-{index:04d}",
            **({"synthetic": True} if synthetic else {}),
            "values": (
                {**exact_fields[logical_object], field: value}
                if logical_object in exact_fields and field in exact_fields[logical_object]
                else {field: value}
            ),
        }
        for index, value in enumerate(values, start=1)
    )
    verified = VerificationStatus.VERIFIED
    return AdapterResponse(
        status=status,
        request_id=f"request-{logical_object}-0001",
        site_id="gbos.localhost",
        logical_object=logical_object,
        tool_name=tool_name,
        synthetic=synthetic,
        rows=rows if status is AdapterStatus.AVAILABLE else (),
        metadata={"source": "test"},
        page={
            "limit": 50,
            "offset": 0,
            "returned_rows": len(rows) if status is AdapterStatus.AVAILABLE else 0,
            "has_more": False,
        },
        verification=VerificationSnapshot(
            startup=verified,
            authentication=verified,
            metadata=verified,
            business=(
                verified if status is AdapterStatus.AVAILABLE else VerificationStatus.UNAVAILABLE
            ),
        ),
        controls=ControlMetrics(network_calls=0),
        reason_code=None if status is AdapterStatus.AVAILABLE else "live_transport_unavailable",
    )


def inputs(*, metric_key: str = "sales.order_value", total_count: int = 2) -> ProjectionInputs:
    return ProjectionInputs(
        metric_key=metric_key,
        window_start=START,
        window_end=END,
        as_of=NOW,
        retrieved_at=NOW,
        checkpoint="checkpoint-0001",
        total_count=total_count,
        reconciliation_reference="control-total-0001",
        reconciliation_variance=Decimal("0"),
        reconciliation_checked_at=NOW,
    )


def crosswalk_for(adapter_response: AdapterResponse) -> MappingProxyType[str, str]:
    return MappingProxyType(
        {
            str(row["record_ref"]): f"governed-{index:04d}"
            for index, row in enumerate(adapter_response.rows, start=1)
        }
    )


def test_exact_registry_recipes_are_frozen() -> None:
    assert {
        key: (recipe.logical_object, recipe.value_field, recipe.aggregation)
        for key, recipe in METRIC_RECIPES.items()
    } == {
        "sales.order_value": ("sales_order", "total_amount", "sum"),
        "inventory.on_hand_quantity": ("inventory", "base_quantity", "balance"),
        "receivables.balance": ("receivable", "open_amount", "balance"),
    }
    with pytest.raises(TypeError):
        METRIC_RECIPES["new.metric"] = METRIC_RECIPES["sales.order_value"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("metric_key", "logical_object", "tool_name", "field"),
    [
        ("sales.order_value", "sales_order", "kingdee.sales_order.get", "total_amount"),
        (
            "inventory.on_hand_quantity",
            "inventory",
            "kingdee.inventory.get",
            "base_quantity",
        ),
        ("receivables.balance", "receivable", "kingdee.receivable.get", "open_amount"),
    ],
)
def test_projector_builds_deterministic_immutable_batches_for_exact_recipes(
    metric_key: str,
    logical_object: str,
    tool_name: str,
    field: str,
) -> None:
    adapter_response = response(
        logical_object=logical_object,
        tool_name=tool_name,
        field=field,
    )
    crosswalk = crosswalk_for(adapter_response)
    projector = MetricsProjector(registry=registry())

    first = projector.project(
        response=adapter_response,
        inputs=inputs(metric_key=metric_key),
        crosswalk=crosswalk,
    )
    second = projector.project(
        response=adapter_response,
        inputs=inputs(metric_key=metric_key),
        crosswalk=crosswalk,
    )

    assert first == second
    assert first.source_mode is SourceMode.SYNTHETIC
    assert first.checkpoint == "checkpoint-0001"
    assert first.transformation_version == "metrics-projection-v1"
    assert first.rows[0].value == Decimal("350.5")
    assert first.rows[0].included_count == 2
    assert first.rows[0].total_count == 2
    assert first.rows[0].source_record_refs == (
        f"{logical_object}-0001",
        f"{logical_object}-0002",
    )
    assert first.rows[0].governed is True
    with pytest.raises(AttributeError):
        first.rows[0].value = Decimal("0")  # type: ignore[misc]


def test_missing_crosswalk_is_visible_and_cannot_be_silently_counted() -> None:
    adapter_response = response()

    batch = MetricsProjector(registry=registry()).project(
        response=adapter_response,
        inputs=inputs(),
        crosswalk={"sales_order-0001": "governed-0001"},
    )

    assert batch.rows[0].governed is False
    assert batch.rows[0].included_count == 1
    assert batch.rows[0].total_count == 2


@pytest.mark.parametrize(
    "adapter_response",
    [
        response(logical_object="inventory"),
        response(tool_name="kingdee.inventory.get"),
        response(field="open_amount"),
        response(status=AdapterStatus.UNAVAILABLE, synthetic=False),
        response(values=tuple(range(51))),
    ],
)
def test_projector_rejects_non_exact_unavailable_or_unbounded_inputs(
    adapter_response: AdapterResponse,
) -> None:
    with pytest.raises(ProjectionRejected):
        MetricsProjector(registry=registry()).project(
            response=adapter_response,
            inputs=inputs(total_count=max(2, len(adapter_response.rows))),
            crosswalk=crosswalk_for(adapter_response),
        )


def test_live_mode_stays_live_and_never_becomes_synthetic() -> None:
    live_registry = MetricRegistry(
        tuple(registry().require(key) for key in registry().metric_keys),
        enabled_modes=frozenset({SourceMode.LIVE}),
    )
    live_response = response(synthetic=False)

    batch = MetricsProjector(registry=live_registry).project(
        response=live_response,
        inputs=inputs(),
        crosswalk=crosswalk_for(live_response),
    )

    assert batch.source_mode is SourceMode.LIVE
    assert "synthetic" not in batch.source_system.lower()
