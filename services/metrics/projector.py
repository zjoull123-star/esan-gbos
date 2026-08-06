from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal

from services.kingdee_adapter import AdapterResponse, AdapterStatus, VerificationStatus

from .models import (
    MetricRegistry,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
    UnavailableReason,
    ValidationError,
    require_identifier,
    require_utc,
    require_window,
    utc_iso,
)

MAX_PROJECTION_ROWS = 50
TRANSFORMATION_VERSION = "metrics-projection-v1"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricRecipe:
    metric_key: str
    logical_object: str
    tool_name: str
    value_field: str
    source_fields: tuple[str, ...]
    aggregation: Literal["sum", "balance"]


METRIC_RECIPES: Mapping[str, MetricRecipe] = MappingProxyType(
    {
        recipe.metric_key: recipe
        for recipe in (
            MetricRecipe(
                metric_key="sales.order_value",
                logical_object="sales_order",
                tool_name="kingdee.sales_order.get",
                value_field="total_amount",
                source_fields=(
                    "order_number",
                    "customer_number",
                    "order_date",
                    "total_amount",
                ),
                aggregation="sum",
            ),
            MetricRecipe(
                metric_key="inventory.on_hand_quantity",
                logical_object="inventory",
                tool_name="kingdee.inventory.get",
                value_field="base_quantity",
                source_fields=("material_number", "warehouse_number", "base_quantity"),
                aggregation="balance",
            ),
            MetricRecipe(
                metric_key="receivables.balance",
                logical_object="receivable",
                tool_name="kingdee.receivable.get",
                value_field="open_amount",
                source_fields=("bill_number", "customer_number", "due_date", "open_amount"),
                aggregation="balance",
            ),
        )
    }
)


class ProjectionRejected(ValidationError):
    """An adapter response cannot enter the governed projection candidate set."""

    def __init__(self, reason: UnavailableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectionInputs:
    metric_key: str
    window_start: datetime
    window_end: datetime
    as_of: datetime
    retrieved_at: datetime
    checkpoint: str
    total_count: int
    reconciliation_reference: str
    reconciliation_variance: Decimal
    reconciliation_checked_at: datetime
    definition_version: str = "1.0.0"

    def __post_init__(self) -> None:
        require_window(self.window_start, self.window_end)
        require_utc(self.as_of, "as_of")
        require_utc(self.retrieved_at, "retrieved_at")
        require_utc(self.reconciliation_checked_at, "reconciliation_checked_at")
        require_identifier(self.checkpoint, "checkpoint")
        require_identifier(self.reconciliation_reference, "reconciliation_reference")
        require_identifier(self.definition_version, "definition_version")
        if not isinstance(self.total_count, int) or isinstance(self.total_count, bool):
            raise ValidationError("total_count must be an integer")
        if self.total_count < 0:
            raise ValidationError("total_count must be non-negative")
        if not self.reconciliation_variance.is_finite():
            raise ValidationError("reconciliation_variance must be finite")


class MetricsProjector:
    """Pure AdapterResponse-to-ProjectionBatch bridge with no I/O surface."""

    def __init__(self, *, registry: MetricRegistry) -> None:
        self._registry = registry

    def project(
        self,
        *,
        response: AdapterResponse,
        inputs: ProjectionInputs,
        crosswalk: Mapping[str, str],
    ) -> ProjectionBatch:
        definition = self._registry.require(inputs.metric_key)
        recipe = METRIC_RECIPES.get(inputs.metric_key)
        if recipe is None:
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "metric has no exact governed projection recipe",
            )
        if inputs.definition_version != definition.definition_version:
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "projection definition version is not governed",
            )
        mode = SourceMode.SYNTHETIC if response.synthetic else SourceMode.LIVE
        if mode not in self._registry.enabled_modes:
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "adapter source mode is disabled by the governed registry",
            )
        self._validate_response(response=response, recipe=recipe, inputs=inputs)

        ordered_rows = sorted(response.rows, key=lambda row: str(row["record_ref"]))
        source_refs = tuple(str(row["record_ref"]) for row in ordered_rows)
        resolved = tuple(crosswalk.get(source_ref) for source_ref in source_refs)
        governed_refs = tuple(value for value in resolved if _valid_crosswalk_ref(value))
        governed = len(governed_refs) == len(source_refs) and len(set(governed_refs)) == len(
            governed_refs
        )
        included_count = len(governed_refs)
        value = sum(
            (
                _decimal_value(row["values"][recipe.value_field])
                for row, governed_ref in zip(ordered_rows, resolved, strict=True)
                if _valid_crosswalk_ref(governed_ref)
            ),
            start=Decimal(0),
        )

        identity = {
            "request_id": response.request_id,
            "site_id": response.site_id,
            "source_mode": mode.value,
            "metric_key": inputs.metric_key,
            "definition_version": inputs.definition_version,
            "window_start": utc_iso(inputs.window_start),
            "window_end": utc_iso(inputs.window_end),
            "as_of": utc_iso(inputs.as_of),
            "retrieved_at": utc_iso(inputs.retrieved_at),
            "checkpoint": inputs.checkpoint,
            "total_count": inputs.total_count,
            "reconciliation_reference": inputs.reconciliation_reference,
            "reconciliation_variance": str(inputs.reconciliation_variance),
            "reconciliation_checked_at": utc_iso(inputs.reconciliation_checked_at),
            "source_refs": source_refs,
            "crosswalk_refs": resolved,
            "value": str(value),
        }
        digest = _digest(identity)
        projection_row = ProjectionRow(
            row_id=f"metric-row-{digest[:24]}",
            metric_key=inputs.metric_key,
            definition_version=inputs.definition_version,
            window_start=inputs.window_start,
            window_end=inputs.window_end,
            as_of=inputs.as_of,
            value=value,
            included_count=included_count,
            total_count=inputs.total_count,
            reconciliation_reference=inputs.reconciliation_reference,
            reconciliation_variance=inputs.reconciliation_variance,
            reconciliation_checked_at=inputs.reconciliation_checked_at,
            source_record_refs=source_refs,
            governed=governed,
        )
        return ProjectionBatch(
            batch_id=f"metric-batch-{digest[24:48]}",
            site_id=response.site_id,
            source_mode=mode,
            checkpoint=inputs.checkpoint,
            source_system=(
                "kingdee-gate5-synthetic" if mode is SourceMode.SYNTHETIC else "kingdee-live-read"
            ),
            transformation_version=TRANSFORMATION_VERSION,
            retrieved_at=inputs.retrieved_at,
            rows=(projection_row,),
        )

    @staticmethod
    def _validate_response(
        *, response: AdapterResponse, recipe: MetricRecipe, inputs: ProjectionInputs
    ) -> None:
        if response.status is not AdapterStatus.AVAILABLE:
            raise ProjectionRejected(
                UnavailableReason.SOURCE_UNAVAILABLE,
                "unavailable adapter response cannot produce a projection",
            )
        if response.reason_code is not None:
            raise ProjectionRejected(
                UnavailableReason.SOURCE_UNAVAILABLE,
                "available adapter response cannot carry a failure reason",
            )
        if (
            response.logical_object != recipe.logical_object
            or response.tool_name != recipe.tool_name
        ):
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "adapter response does not match the exact metric recipe",
            )
        if any(
            status is not VerificationStatus.VERIFIED
            for status in (
                response.verification.startup,
                response.verification.authentication,
                response.verification.metadata,
                response.verification.business,
            )
        ):
            raise ProjectionRejected(
                UnavailableReason.SOURCE_UNAVAILABLE,
                "adapter verification is incomplete",
            )
        if (
            response.controls.writer_tools_discovered != 0
            or response.controls.mutation_attempts != 0
            or response.controls.synthetic_fallbacks != 0
        ):
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "adapter controls do not prove a read-only non-fallback response",
            )
        if response.synthetic and response.controls.network_calls != 0:
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "synthetic adapter response cannot contain network calls",
            )
        if not 1 <= len(response.rows) <= MAX_PROJECTION_ROWS:
            raise ProjectionRejected(
                UnavailableReason.SOURCE_UNAVAILABLE,
                "adapter response row count is outside the projection budget",
            )

        returned_rows = response.page.get("returned_rows")
        limit = response.page.get("limit")
        offset = response.page.get("offset")
        has_more = response.page.get("has_more")
        if (
            not isinstance(returned_rows, int)
            or isinstance(returned_rows, bool)
            or returned_rows != len(response.rows)
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PROJECTION_ROWS
            or len(response.rows) > limit
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(has_more, bool)
            or inputs.total_count < offset + len(response.rows)
            or (has_more and inputs.total_count <= offset + len(response.rows))
        ):
            raise ProjectionRejected(
                UnavailableReason.UNGOVERNED_SOURCE,
                "adapter page metadata is inconsistent or unbounded",
            )

        observed_refs: set[str] = set()
        for row in response.rows:
            expected_row_keys = {"record_ref", "values"}
            if response.synthetic:
                expected_row_keys.add("synthetic")
            values = row.get("values")
            record_ref = row.get("record_ref")
            if (
                set(row) != expected_row_keys
                or (response.synthetic and row.get("synthetic") is not True)
                or not isinstance(record_ref, str)
                or record_ref in observed_refs
                or not isinstance(values, Mapping)
                or set(values) != set(recipe.source_fields)
            ):
                raise ProjectionRejected(
                    UnavailableReason.UNGOVERNED_SOURCE,
                    "adapter row does not match the exact governed field recipe",
                )
            observed_refs.add(record_ref)
            _decimal_value(values[recipe.value_field])


def _valid_crosswalk_ref(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value and len(value) <= 256


def _decimal_value(value: object) -> Decimal:
    if not isinstance(value, int | float | Decimal) or isinstance(value, bool):
        raise ProjectionRejected(
            UnavailableReason.UNGOVERNED_SOURCE,
            "metric recipe value is not numeric",
        )
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProjectionRejected(
            UnavailableReason.UNGOVERNED_SOURCE,
            "metric recipe value is not numeric",
        ) from exc
    if not result.is_finite():
        raise ProjectionRejected(
            UnavailableReason.UNGOVERNED_SOURCE,
            "metric recipe value must be finite",
        )
    return result


def _digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
