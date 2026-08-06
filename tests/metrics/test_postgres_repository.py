from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from services.metrics import (
    PostgresMetricsRepository,
    ProjectionBatch,
    ProjectionRow,
    SourceMode,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)


class Cursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rows: list[tuple[Any, ...] | None] = []

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [row for row in self.rows if row is not None]


class Connection:
    def __init__(self) -> None:
        self.cursor_instance = Cursor()
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> Cursor:
        return self.cursor_instance


def batch() -> ProjectionBatch:
    return ProjectionBatch(
        batch_id="batch-1",
        site_id="site-a",
        source_mode=SourceMode.SYNTHETIC,
        checkpoint="000001",
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
                value=Decimal("10"),
                included_count=1,
                total_count=1,
                reconciliation_reference="control-1",
                reconciliation_variance=Decimal("0"),
                reconciliation_checked_at=NOW,
                source_record_refs=("source-1",),
                governed=True,
            ),
        ),
    )


def test_postgres_append_is_one_transaction_site_scoped_and_insert_only() -> None:
    connection = Connection()
    repository = PostgresMetricsRepository(connection)

    repository.append_batch(batch())

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.statements)
    assert connection.transactions == 1
    assert "set_config('app.site_id'" in sql
    assert "INSERT INTO metrics.projection_batches" in sql
    assert "INSERT INTO metrics.projection_rows" in sql
    assert "INSERT INTO metrics.checkpoints" in sql
    assert "UPDATE metrics.projection_batches" not in sql
    assert "UPDATE metrics.projection_rows" not in sql


def test_postgres_lookup_uses_fixed_sql_and_bound_exact_values() -> None:
    connection = Connection()
    connection.cursor_instance.rows = [None]
    repository = PostgresMetricsRepository(connection)

    assert (
        repository.find_projection(
            site_id="site-a",
            metric_key="sales.order_value",
            source_mode=SourceMode.SYNTHETIC,
            window_start=NOW - timedelta(days=1),
            window_end=NOW,
        )
        is None
    )

    sql, params = connection.cursor_instance.statements[-1]
    assert "metric_key = %s" in sql
    assert "window_start = %s" in sql
    assert "window_end = %s" in sql
    assert "ORDER BY" in sql
    assert params is not None and "sales.order_value" in params
    assert ";" not in "sales.order_value"
