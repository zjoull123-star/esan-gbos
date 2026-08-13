from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "services/email_gateway/migrations/013_identity_route_work.sql"


def test_identity_route_work_migration_is_forced_rls_fenced_and_data_minimal() -> None:
    assert MIGRATION.is_file()
    sql = MIGRATION.read_text().lower()
    assert "create table if not exists email_gateway.identity_route_work" in sql
    assert "force row level security" in sql
    assert "current_setting('gbos.processing_purpose', true)" in sql
    assert "attempt between 0 and 5" in sql
    assert "lease_generation" in sql and "fence_token" in sql
    assert "identity_route_work_immutable_pins" in sql
    assert "apply_identity_route_fenced" in sql
    assert "security definer" in sql
    assert "grant execute" in sql
    assert "grant update on email_gateway.inbox_items" not in sql
    for forbidden in ("raw_email", "target_ref", "content_ciphertext", "address_display"):
        assert forbidden not in sql


def test_inbox_insert_rearms_current_projection_work_and_fences_a_racing_worker() -> None:
    sql = MIGRATION.read_text().lower()

    assert "requeue_identity_route_work_for_inbox" in sql
    assert "after insert on email_gateway.inbox_items" in sql
    assert "new.state <> 'identity_pending'" in sql
    assert "participant.role = 'from'" in sql
    assert "mailbox.business_purpose" in sql
    assert "mailbox.default_team_ref = new.team_ref" in sql
    assert "projection.identity_type = 'party'" in sql
    assert "projection.status = 'confirmed'" in sql
    assert "order by projection.external_identity_revision desc" in sql
    assert "status in ('completed', 'leased')" in sql
    assert "lease_generation = work.lease_generation + 1" in sql
    assert "lease_owner = null" in sql
    assert "fence_token = null" in sql
    assert "attempt = 0" in sql
    assert "completed_at = null" in sql
    assert "greatest(clock_timestamp(), work.created_at)" in sql


def test_projection_trigger_and_apply_take_the_address_lock_before_the_work_row() -> None:
    sql = MIGRATION.read_text().lower()
    trigger = sql.split(
        "create or replace function email_gateway.requeue_identity_route_work_for_inbox()",
        1,
    )[1].split("drop trigger if exists inbox_items_requeue_identity_route_work", 1)[0]
    apply = sql.split("create or replace function email_gateway.apply_identity_route_fenced(", 1)[1]

    assert trigger.index("pg_advisory_xact_lock") < trigger.index(
        "insert into email_gateway.identity_route_work as work"
    )
    assert apply.index("pg_advisory_xact_lock") < apply.index("select work.* into v_work")
    assert apply.index("select work.* into v_work") < apply.index("for update")
