from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

import pytest

from services.metrics import (
    PostgresMetricsRepository,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
)

pytestmark = pytest.mark.postgres_integration


def test_gate5_metrics_postgres_role_and_rls_live() -> None:
    if os.getenv("GBOS_RUN_GATE5_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set GBOS_RUN_GATE5_POSTGRES_INTEGRATION=1 for Gate 5 PostgreSQL tests")

    import psycopg

    with (
        psycopg.connect(os.environ["GBOS_GATE5_METRICS_DSN"]) as connection,
        connection.transaction(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (False, False)
        cursor.execute("SELECT has_schema_privilege(current_user, 'metrics', 'USAGE')")
        assert cursor.fetchone() == (True,)
        cursor.execute("SELECT has_schema_privilege(current_user, 'agent_runtime', 'USAGE')")
        assert cursor.fetchone() == (False,)


def test_gate5_projection_query_audit_and_immutability_live() -> None:
    if os.getenv("GBOS_RUN_GATE5_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set GBOS_RUN_GATE5_POSTGRES_INTEGRATION=1 for Gate 5 PostgreSQL tests")

    import psycopg

    suffix = uuid4().hex
    site_id = f"gate5-{suffix}.localhost"
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=1)
    batch = ProjectionBatch(
        batch_id=f"batch-{suffix}",
        site_id=site_id,
        source_mode=SourceMode.SYNTHETIC,
        checkpoint=f"checkpoint-{suffix}",
        source_system="kingdee-gate5-synthetic",
        transformation_version="metrics-projection-v1",
        retrieved_at=now,
        rows=(
            ProjectionRow(
                row_id=f"row-{suffix}",
                metric_key="sales.order_value",
                definition_version="1.0.0",
                window_start=start,
                window_end=now,
                as_of=now,
                value=Decimal("125.50"),
                included_count=1,
                total_count=1,
                reconciliation_reference=f"recon-{suffix}",
                reconciliation_variance=Decimal("0"),
                reconciliation_checked_at=now,
                source_record_refs=(f"sales-order-{suffix}",),
                governed=True,
            ),
        ),
    )

    with psycopg.connect(os.environ["GBOS_GATE5_METRICS_DSN"]) as connection:
        repository = PostgresMetricsRepository(cast(Any, connection))
        assert repository.append_batch(batch) == batch
        assert repository.append_batch(batch) == batch
        projection = repository.find_projection(
            site_id=site_id,
            metric_key="sales.order_value",
            source_mode=SourceMode.SYNTHETIC,
            window_start=start,
            window_end=now,
        )
        assert projection is not None and projection.row.value == Decimal("125.50")
        assert (
            repository.find_projection(
                site_id=f"other-{suffix}.localhost",
                metric_key="sales.order_value",
                source_mode=SourceMode.SYNTHETIC,
                window_start=start,
                window_end=now,
            )
            is None
        )
        repository.append_audit(
            site_id=site_id,
            audit_id=f"audit-{suffix}",
            request_id=f"request-{suffix}",
            metric_key="sales.order_value",
            source_mode=SourceMode.SYNTHETIC,
            window_start=start,
            window_end=now,
            queried_at=now,
            outcome="available",
            reason=None,
            batch_id=batch.batch_id,
            row_id=batch.rows[0].row_id,
        )

        with pytest.raises(psycopg.Error), connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))
            cursor.execute(
                "UPDATE metrics.projection_rows SET value = 0 WHERE site_id = %s AND row_id = %s",
                (site_id, batch.rows[0].row_id),
            )
