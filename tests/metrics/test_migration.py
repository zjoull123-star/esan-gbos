from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "services" / "metrics" / "migrations"
TABLES = {"projection_batches", "projection_rows", "checkpoints", "query_audit"}


def migration_sql() -> str:
    paths = sorted(MIGRATIONS.glob("*.sql"))
    assert paths, "Metrics service must provide PostgreSQL migrations"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()


def table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"create\s+table\s+if\s+not\s+exists\s+metrics\.{table}\s*\((.*?)\);",
        sql,
        re.DOTALL,
    )
    assert match, f"missing metrics.{table}"
    return match.group(1)


def test_migration_defines_immutable_projection_and_audit_relations() -> None:
    sql = migration_sql()

    for table in TABLES:
        assert re.search(r"primary\s+key\s*\(\s*site_id\s*,", table_body(sql, table))
    assert "source_mode in ('synthetic', 'live')" in sql
    assert "foreign key (site_id, batch_id)" in table_body(sql, "projection_rows")
    assert "on delete restrict" in sql
    assert "metric_key" in table_body(sql, "projection_rows")
    assert "window_start" in table_body(sql, "query_audit")
    assert "window_end" in table_body(sql, "query_audit")
    assert "source_lineage" in table_body(sql, "projection_rows")
    assert "freshness" in table_body(sql, "projection_rows")
    assert "coverage" in table_body(sql, "projection_rows")
    assert "reconciliation" in table_body(sql, "projection_rows")


def test_migration_forces_rls_and_declares_least_privilege_role() -> None:
    sql = migration_sql()

    for table in TABLES:
        assert f"alter table metrics.{table} enable row level security" in sql
        assert f"alter table metrics.{table} force row level security" in sql
        assert f"create policy {table}_site_isolation" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "gbos_metrics_app" in sql
    assert "nosuperuser" in sql
    assert "nobypassrls" in sql
    assert "grant usage on schema metrics to gbos_metrics_app" in sql
    assert "grant select, insert on" in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql


def test_migration_blocks_update_and_delete_for_immutable_relations() -> None:
    sql = migration_sql()

    assert "raise exception 'metrics records are append-only'" in sql
    for table in ("projection_batches", "projection_rows", "query_audit"):
        assert f"before update or delete on metrics.{table}" in sql
