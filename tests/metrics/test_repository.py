from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from services.metrics import (
    ImmutableConflict,
    InMemoryMetricsRepository,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
    ValidationError,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


def batch(*, batch_id: str = "batch-1", checkpoint: str = "000001") -> ProjectionBatch:
    return ProjectionBatch(
        batch_id=batch_id,
        site_id="site-a",
        source_mode=SourceMode.SYNTHETIC,
        checkpoint=checkpoint,
        source_system="synthetic_kingdee_projection",
        transformation_version="metrics-projection-v1",
        retrieved_at=NOW,
        rows=(
            ProjectionRow(
                row_id="row-1",
                metric_key="sales.order_value",
                window_start=NOW - timedelta(days=1),
                window_end=NOW,
                as_of=NOW,
                value=Decimal("125.50"),
                included_count=10,
                total_count=10,
                reconciliation_reference="control-total-1",
                reconciliation_variance=Decimal("0"),
                reconciliation_checked_at=NOW,
                source_record_refs=("sales-order-1",),
                governed=True,
            ),
        ),
    )


def test_ingestion_is_idempotent_but_immutable_on_conflict() -> None:
    repository = InMemoryMetricsRepository()
    original = batch()

    assert repository.append_batch(original) == original
    assert repository.append_batch(original) == original
    with pytest.raises(ImmutableConflict):
        repository.append_batch(replace(original, checkpoint="000002"))


def test_checkpoint_is_append_only_opaque_and_mode_scoped() -> None:
    repository = InMemoryMetricsRepository()
    repository.append_batch(batch(checkpoint="000002"))

    assert repository.checkpoint("site-a", SourceMode.SYNTHETIC) == "000002"
    assert repository.checkpoint("site-a", SourceMode.LIVE) is None
    repository.append_batch(
        replace(
            batch(batch_id="batch-2", checkpoint="opaque-earlier-token"),
            rows=(replace(batch().rows[0], row_id="row-2"),),
        )
    )
    assert repository.checkpoint("site-a", SourceMode.SYNTHETIC) == "opaque-earlier-token"
    with pytest.raises(ImmutableConflict, match="checkpoint"):
        repository.append_batch(
            replace(
                batch(batch_id="batch-3", checkpoint="opaque-earlier-token"),
                rows=(replace(batch().rows[0], row_id="row-3"),),
            )
        )


def test_repository_refuses_mixed_source_modes_for_one_site() -> None:
    repository = InMemoryMetricsRepository()
    repository.append_batch(batch())
    live = replace(
        batch(batch_id="batch-live", checkpoint="000002"),
        source_mode=SourceMode.LIVE,
        source_system="kingdee-live-projection",
    )

    with pytest.raises(ValidationError, match="mutually exclusive"):
        repository.append_batch(live)


def test_query_audit_is_append_only_and_defensively_copied() -> None:
    repository = InMemoryMetricsRepository()
    repository.append_audit(
        site_id="site-a",
        audit_id="audit-1",
        request_id="request-1",
        metric_key="sales.order_value",
        source_mode=SourceMode.SYNTHETIC,
        window_start=NOW - timedelta(days=1),
        window_end=NOW,
        queried_at=NOW,
        outcome="available",
        reason=None,
        batch_id="batch-1",
        row_id="row-1",
    )

    audits = repository.audits("site-a")
    assert len(audits) == 1
    assert audits[0].request_id == "request-1"
    with pytest.raises(ImmutableConflict):
        repository.append_audit(
            site_id="site-a",
            audit_id="audit-1",
            request_id="request-2",
            metric_key="sales.order_value",
            source_mode=SourceMode.SYNTHETIC,
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
            queried_at=NOW,
            outcome="unavailable",
            reason="stale",
            batch_id=None,
            row_id=None,
        )
