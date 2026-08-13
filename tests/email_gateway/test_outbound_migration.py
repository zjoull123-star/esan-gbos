from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services/email_gateway/migrations/007_email_gateway_outbound.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_adds_closed_command_receipt_state_attempt_and_receipt_tables() -> None:
    sql = _sql()
    for table in (
        "email_gateway.command_inbox",
        "email_gateway.send_outbox_state",
        "email_gateway.send_attempts",
        "email_gateway.provider_receipts",
        "email_gateway.reconciliation_receipts",
    ):
        assert f"table if not exists {table}" in sql
        assert f"alter table {table} enable row level security" in sql
        assert f"alter table {table} force row level security" in sql
        assert f"revoke all on {table} from public" in sql
    assert "approved_envelope jsonb" in sql
    assert "raw_address" not in sql
    assert "mime_bytes" not in sql


def test_only_executor_inserts_immutable_outbox_and_only_worker_appends_attempts() -> None:
    sql = _sql()
    assert "create role gbos_email_command_executor nologin" in sql
    assert "create role gbos_email_send_worker nologin" in sql
    assert "grant insert on email_gateway.command_inbox" in sql
    assert "to gbos_email_command_executor" in sql
    assert "grant insert on email_gateway.send_outbox" in sql
    assert "grant insert on email_gateway.send_attempts" in sql
    assert "to gbos_email_send_worker" in sql
    assert "grant insert, update on email_gateway.send_outbox to gbos_email_gateway_app" not in sql
    assert "grant insert on email_gateway.send_outbox to gbos_email_send_worker" not in sql
    assert "grant update on email_gateway.command_inbox" not in sql
    assert "before update or delete on email_gateway.command_inbox" in sql
    assert "before update or delete on email_gateway.send_attempts" in sql
