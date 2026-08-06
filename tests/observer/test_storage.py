from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
OBSERVER_MIGRATIONS = ROOT / "services" / "observer" / "migrations"
EXPECTED_TABLES = {
    "manual_import_jobs",
    "raw_objects",
    "observation_events",
    "participants",
    "evidence_refs",
    "event_evidence",
    "checkpoints",
    "quarantine",
    "dead_letter",
    "processor_runs",
    "derivation_edges",
    "consent",
    "legal_holds",
    "deletion_receipts",
}
GATE4_TABLES = {
    "verified_facts",
    "conflicts",
    "decisions",
    "actions",
    "draft_mutations",
    "approved_commands",
    "review_cases",
    "agent_tasks",
}


def _migration_sql() -> str:
    paths = sorted(OBSERVER_MIGRATIONS.glob("*.sql"))
    assert paths, "Observer must provide plain SQL migrations"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()


def _table_body(sql: str, schema: str, table: str) -> str:
    match = re.search(
        rf"create\s+table\s+if\s+not\s+exists\s+{schema}\.{table}\s*\((.*?)\);",
        sql,
        re.DOTALL,
    )
    assert match, f"missing CREATE TABLE for {schema}.{table}"
    return match.group(1)


def test_observer_migration_creates_only_gate3_tables() -> None:
    sql = _migration_sql()

    for table in EXPECTED_TABLES:
        assert f"create table if not exists observer.{table}" in sql
    for table in GATE4_TABLES:
        assert f"observer.{table}" not in sql


def test_observer_tables_use_composite_site_primary_keys() -> None:
    sql = _migration_sql()

    for table in EXPECTED_TABLES:
        body = _table_body(sql, "observer", table)
        assert re.search(r"primary\s+key\s*\(\s*site_id\s*,", body), table


def test_observer_foreign_keys_include_site_id() -> None:
    sql = _migration_sql()
    foreign_keys = re.findall(
        r"foreign\s+key\s*\(([^)]+)\)\s+references\s+observer\.[a-z_]+\s*\(([^)]+)\)",
        sql,
    )

    assert foreign_keys
    assert all("site_id" in local and "site_id" in remote for local, remote in foreign_keys)


def test_observer_tables_force_row_level_security() -> None:
    sql = _migration_sql()

    for table in EXPECTED_TABLES:
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql
        assert f"create policy {table}_site_isolation" in sql
    assert "current_setting('app.site_id', true)" in sql


def test_runtime_role_cannot_bypass_rls() -> None:
    sql = _migration_sql()

    assert "gbos_observer_app" in sql
    assert "gbos_context_app" not in sql
    assert "nobypassrls" in sql
    assert "nosuperuser" in sql
    assert "grant select, insert, update, delete on all tables" not in sql


def test_observer_policies_are_safe_on_direct_second_run() -> None:
    sql = _migration_sql()

    for table in EXPECTED_TABLES:
        assert f"drop policy if exists {table}_site_isolation on observer.{table}" in sql
