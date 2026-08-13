from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "services/email_gateway/migrations/010_email_gateway_sla_authority.sql"


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_adds_immutable_sla_event_and_authority_receipt_records() -> None:
    sql = _sql()
    for relation in (
        "email_gateway.inbox_sla_events",
        "email_gateway.inbox_authority_receipts",
    ):
        assert f"create table if not exists {relation}" in sql
        assert f"alter table {relation} force row level security" in sql
        assert f"revoke all on {relation} from public" in sql
    assert "provider_accepted_receipt_ref" in sql
    assert "policy_revision" in sql
    assert "payload_digest" in sql
    assert "grant delete" not in sql
    assert "observer." not in sql
    for forbidden in ("raw_content", "raw_address", "from_address", "to_address"):
        assert forbidden not in sql
    authority_table = sql.split(
        "create table if not exists email_gateway.inbox_authority_receipts", 1
    )[1].split(");", 1)[0]
    assert "actor_ref text" not in authority_table
    assert "target_user_ref text" not in authority_table
    assert "actor_ref_digest" in authority_table
    assert "target_user_ref_digest" in authority_table
    assert (
        "grant select, insert on email_gateway.inbox_sla_events to gbos_email_send_worker"
        not in sql
    )


def test_migration_rejects_clock_policy_and_replay_drift_in_one_database_function() -> None:
    sql = _sql()
    assert "function email_gateway.apply_inbox_sla_operation" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog" in sql
    assert "sla clock regression" in sql
    assert "sla policy revision drift" in sql
    assert "sla replay drift" in sql


def test_provider_accepted_receipt_completes_original_sla_in_same_transaction() -> None:
    sql = _sql()
    assert "function email_gateway.complete_sla_from_provider_receipt" in sql
    assert "after insert on email_gateway.provider_receipts" in sql
    assert "new.outcome not in ('accepted', 'delivered')" in sql
    assert "provider-accepted sla clock regression" in sql
    assert "clock.started_at" in sql
    assert "clock.due_at" in sql
    assert "first provider-accepted receipt is authoritative" in sql
    assert "provider receipt replay drift" not in sql
    assert "revoke all on function email_gateway.complete_sla_from_provider_receipt" in sql
