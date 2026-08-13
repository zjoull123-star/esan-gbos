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
        "006_email_gateway_human_retention.sql",
        "007_email_gateway_outbound.sql",
        "008_email_gateway_participant_authority.sql",
        "009_email_gateway_mailbox_identity.sql",
        "010_email_gateway_sla_authority.sql",
        "011_email_gateway_retention_runtime.sql",
        "012_identity_projection_purpose_scope.sql",
        "013_identity_route_work.sql",
        "014_email_material_terminal_authority.sql",
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
        "email_gateway.worker_heartbeats",
        "email_gateway.command_inbox",
        "email_gateway.send_outbox_state",
        "email_gateway.send_attempts",
        "email_gateway.provider_receipts",
        "email_gateway.reconciliation_receipts",
        "email_gateway.inbox_sla_events",
        "email_gateway.inbox_authority_receipts",
        "email_gateway.retention_run_items",
        "email_gateway.retention_audit_events",
        "email_gateway.identity_route_work",
        "email_gateway.email_material_terminal_authorities",
        "email_gateway.email_material_terminal_authority_state",
        "email_gateway.email_material_tombstone_callbacks",
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
        "worker_heartbeats",
        "command_inbox",
        "send_outbox_state",
        "send_attempts",
        "provider_receipts",
        "reconciliation_receipts",
        "inbox_sla_events",
        "inbox_authority_receipts",
        "retention_run_items",
        "retention_audit_events",
        "identity_route_work",
        "email_material_terminal_authorities",
        "email_material_terminal_authority_state",
        "email_material_tombstone_callbacks",
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


def test_participant_authority_binding_columns_are_nullable_for_legacy_and_digest_only() -> None:
    sql = _all_sql()
    assert "add column if not exists mailbox_config_revision bigint" in sql
    assert "add column if not exists participant_binding_digest text" in sql
    assert "add column if not exists evidence_binding_digest text" in sql
    assert "update email_gateway.publication_receipts" not in sql
    for forbidden in ("from_address", "to_address", "cc_address", "bcc_address"):
        assert forbidden not in sql


def test_mailbox_identity_migration_is_idempotent_nullable_and_reasserts_inventory() -> None:
    migration = (MIGRATIONS / "009_email_gateway_mailbox_identity.sql").read_text().lower()

    assert "add column if not exists mailbox_address_identity_ref text" in migration
    assert "mailbox_address_identity_ref is null" in migration
    assert "^extid:v1:email:[a-za-z0-9_-]{43}$" in migration
    assert "if not exists" in migration
    assert "add constraint mailboxes_mailbox_address_identity_ref_check" in migration
    assert "alter table email_gateway.mailboxes enable row level security" in migration
    assert "alter table email_gateway.mailboxes force row level security" in migration
    assert "revoke all on email_gateway.mailboxes from public" in migration
    assert (
        "grant select, insert, update on email_gateway.mailboxes to gbos_email_gateway_app"
        in migration
    )
    assert "grant select on email_gateway.mailboxes to gbos_email_gateway_worker" in migration
    for forbidden in ("canonical_mailbox_address", "@example", "sentinel"):
        assert forbidden not in migration
