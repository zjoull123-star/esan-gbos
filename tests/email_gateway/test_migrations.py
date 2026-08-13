from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "services" / "email_gateway" / "migrations"


def _all_sql() -> str:
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert [path.name for path in files] == [
        "001_email_gateway_foundation.sql",
        "002_email_gateway_inbox.sql",
        "003_email_gateway_workflow_outboxes.sql",
        "004_email_gateway_retention.sql",
        "005_email_gateway_human_operations.sql",
    ]
    return "\n".join(path.read_text() for path in files).lower()


def test_migrations_create_exact_gateway_inventory() -> None:
    sql = _all_sql()
    required = {
        "email_gateway.schema_migrations",
        "email_gateway.mailboxes",
        "email_gateway.mailbox_config_outbox",
        "email_gateway.publication_receipts",
        "email_gateway.channel_messages",
        "email_gateway.message_participants",
        "email_gateway.inbox_items",
        "email_gateway.identity_projection_receipts",
        "email_gateway.route_decisions",
        "email_gateway.routing_rules",
        "email_gateway.conversations",
        "email_gateway.conversation_messages",
        "email_gateway.thread_suggestions",
        "email_gateway.reply_drafts",
        "email_gateway.send_outbox",
        "email_gateway.audit_events",
        "email_gateway.retention_runs",
        "email_gateway.content_expiration_receipts",
        "email_gateway.mailbox_sla_policies",
        "email_gateway.inbox_sla_clocks",
        "email_gateway.inbox_operation_requests",
    }
    for relation in required:
        assert f"table if not exists {relation}" in sql


def test_migrations_forbid_duplicate_observer_authority_tables() -> None:
    sql = _all_sql()
    for forbidden in (
        "provider_deliveries",
        "provider_cursor",
        "connector_checkpoint",
        "raw_eml",
        "attachments",
        "quarantine",
        "identity_authority",
        "identity_revision_sequence",
    ):
        assert f"email_gateway.{forbidden}" not in sql


def test_every_business_table_forces_rls_and_public_has_no_grants() -> None:
    sql = _all_sql()
    tables = [
        "mailboxes",
        "mailbox_config_outbox",
        "publication_receipts",
        "channel_messages",
        "message_participants",
        "inbox_items",
        "identity_projection_receipts",
        "route_decisions",
        "routing_rules",
        "conversations",
        "conversation_messages",
        "thread_suggestions",
        "reply_drafts",
        "send_outbox",
        "audit_events",
        "retention_runs",
        "content_expiration_receipts",
        "mailbox_sla_policies",
        "inbox_sla_clocks",
        "inbox_operation_requests",
    ]
    for table in tables:
        relation = f"email_gateway.{table}"
        assert f"alter table {relation} enable row level security" in sql
        assert f"alter table {relation} force row level security" in sql
        assert f"revoke all on {relation} from public" in sql
    assert "create policy email_gateway_site_scope" in sql
    assert "current_setting('gbos.site_id', true)" in sql


def test_grants_keep_observer_and_gateway_databases_separate() -> None:
    sql = _all_sql()
    assert "gbos_email_gateway_app" in sql
    assert "gbos_email_gateway_worker" in sql
    assert "grant" in sql
    assert "observer." not in sql
    assert "gbos_observer_app" not in sql
    assert "grant insert, update on email_gateway.send_outbox to gbos_email_gateway_app" not in sql


def test_participant_constraint_accepts_both_frozen_opaque_identity_shapes() -> None:
    sql = _all_sql()
    assert "extid:v1:email:" in sql
    assert "unresolved:delivery:" in sql


def test_mailbox_config_outbox_persists_server_generated_activation_watermark() -> None:
    sql = _all_sql()
    assert "activation_not_before timestamptz" in sql
    assert "activation_not_before timestamptz not null" in sql
    assert "clock_timestamp()" in sql
