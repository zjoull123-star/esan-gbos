from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "services/email_gateway/migrations/005_email_gateway_human_operations.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_adds_revisioned_sla_and_idempotent_human_operation_records() -> None:
    sql = _sql()
    for relation in (
        "email_gateway.mailbox_sla_policies",
        "email_gateway.inbox_sla_clocks",
        "email_gateway.inbox_operation_requests",
    ):
        assert f"table if not exists {relation}" in sql
    assert "first_response_duration_seconds between 60 and 604800" in sql
    assert "unique (site_id, idempotency_key)" in sql
    assert "provider_accepted_receipt_ref" in sql
    assert "pause" not in sql


def test_new_business_tables_force_rls_and_have_minimum_grants() -> None:
    sql = _sql()
    for table in ("mailbox_sla_policies", "inbox_sla_clocks", "inbox_operation_requests"):
        relation = f"email_gateway.{table}"
        assert f"alter table {relation} enable row level security" in sql
        assert f"alter table {relation} force row level security" in sql
        assert f"revoke all on {relation} from public" in sql
        assert f"create policy email_gateway_site_scope on {relation}" in sql
    assert "grant select, insert on email_gateway.mailbox_sla_policies" in sql
    assert "grant select, insert, update on email_gateway.inbox_sla_clocks" in sql
    assert "grant select, insert on email_gateway.inbox_operation_requests" in sql
    assert "grant delete" not in sql
    assert "observer." not in sql


def test_split_uses_one_narrow_scope_bound_function_without_table_delete_grant() -> None:
    sql = _sql()

    assert "function email_gateway.clear_conversation_members_for_split" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog" in sql
    assert "current_setting('gbos.site_id', true)" in sql
    assert "current_setting( 'gbos.processing_purpose', true )" in sql
    assert "mailbox.business_purpose = p_processing_purpose" in sql
    assert "revoke all on function email_gateway.clear_conversation_members_for_split" in sql
    assert "grant execute on function email_gateway.clear_conversation_members_for_split" in sql
    assert "grant delete" not in sql
