import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "services" / "agent_runtime" / "migrations"
TABLES = {"agent_tasks", "timeline", "dead_letter", "model_invocations"}


def migration_sql() -> str:
    paths = sorted(MIGRATIONS.glob("*.sql"))
    assert paths, "Agent runtime must provide a PostgreSQL migration"
    return "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()


def table_body(sql: str, table: str) -> str:
    match = re.search(
        rf"create\s+table\s+if\s+not\s+exists\s+agent_runtime\.{table}\s*\((.*?)\);",
        sql,
        re.DOTALL,
    )
    assert match, f"missing agent_runtime.{table}"
    return match.group(1)


def test_migration_uses_site_composite_keys_and_lineage_foreign_keys() -> None:
    sql = migration_sql()

    for table in TABLES:
        assert re.search(r"primary\s+key\s*\(\s*site_id\s*,", table_body(sql, table))
    assert "unique (site_id, idempotency_key)" in table_body(sql, "agent_tasks")
    assert (
        "foreign key (site_id, task_id) references agent_runtime.agent_tasks (site_id, task_id)"
        in table_body(sql, "timeline")
    )
    assert (
        "foreign key (site_id, task_id) references agent_runtime.agent_tasks (site_id, task_id)"
        in table_body(sql, "dead_letter")
    )
    assert "causation_id" in table_body(sql, "agent_tasks")
    assert "correlation_id" in table_body(sql, "timeline")
    assert "foreign key (site_id, parent_task_id)" in sql
    assert "references agent_runtime.agent_tasks (site_id, task_id)" in sql
    assert "parent_task_id <> task_id" in sql


def test_migration_enforces_status_lease_attempt_and_timeline_invariants() -> None:
    sql = migration_sql()
    tasks = table_body(sql, "agent_tasks")
    timeline = table_body(sql, "timeline")

    assert "attempt <= max_attempts" in tasks
    assert "lease_owner is null and lease_expires_at is null" in tasks
    assert "status in ('leased', 'running')" in tasks
    assert "sequence bigint not null" in timeline
    assert "unique (site_id, timeline_event_id)" in timeline
    assert "create index" in sql
    assert "priority desc" in sql
    assert "due_at asc" in sql


def test_migration_forces_site_row_level_security() -> None:
    sql = migration_sql()

    for table in TABLES:
        assert f"alter table agent_runtime.{table} enable row level security" in sql
        assert f"alter table agent_runtime.{table} force row level security" in sql
        assert f"create policy {table}_site_isolation" in sql
    assert "current_setting('app.site_id', true)" in sql


def test_migration_declares_a_dedicated_least_privilege_runtime_role() -> None:
    sql = migration_sql()

    assert "gbos_agent_app" in sql
    assert "nobypassrls" in sql
    assert "nosuperuser" in sql
    assert "nocreatedb" in sql
    assert "nocreaterole" in sql
    assert "noreplication" in sql
    assert "grant usage on schema agent_runtime to gbos_agent_app" in sql
    assert "grant select, insert, update on" in sql
    assert "context." not in sql
    assert "observer." not in sql


def test_model_invocation_ledger_is_content_free_and_represents_unknown_values() -> None:
    sql = migration_sql()
    invocations = table_body(sql, "model_invocations")

    assert "primary key (site_id, invocation_id)" in invocations
    assert "unique (site_id, idempotency_key)" in invocations
    assert "token_usage_status" in invocations
    assert "cost_status" in invocations
    assert "output_digest" in invocations
    assert "retry_count" in invocations
    assert "error_code" in invocations
    assert "price_catalog_version" in invocations
    for forbidden in (
        "prompt_text",
        "response_text",
        "reasoning_content",
        "api_key",
        "token_map",
        "email",
        "phone",
        "person_name",
        "organization_text",
    ):
        assert forbidden not in invocations
    assert "token_usage_status = 'unknown'" in invocations
    assert "input_tokens is null" in invocations
    assert "cost_status = 'unknown'" in invocations
    assert "cost_amount is null" in invocations


def test_gate4_migrate_discovers_all_additive_agent_migrations() -> None:
    script = (ROOT / "scripts" / "dev" / "gate4-migrate").read_text(encoding="utf-8")

    assert "/migrations/agent/*.sql" in script
    assert "observer.schema_migrations" in script
    assert "migration_checksum" in script
