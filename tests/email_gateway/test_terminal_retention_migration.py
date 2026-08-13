from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "services"
    / "email_gateway"
    / "migrations"
    / "014_email_material_terminal_authority.sql"
)


def test_migration_has_immutable_authorities_fenced_registration_and_callbacks() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in (
        "email_material_terminal_authorities",
        "email_material_terminal_authority_state",
        "email_material_tombstone_callbacks",
    ):
        assert f"create table if not exists email_gateway.{table}" in sql
        assert f"alter table email_gateway.{table} enable row level security" in sql
        assert f"alter table email_gateway.{table} force row level security" in sql
        assert f"revoke all on email_gateway.{table} from public" in sql

    assert "before update or delete on email_gateway.email_material_terminal_authorities" in sql
    assert "before update or delete on email_gateway.email_material_tombstone_callbacks" in sql
    assert "attempt between 0 and 5" in sql
    assert "lease_generation" in sql
    assert "for update skip locked" in sql


def test_sent_and_discard_functions_require_exact_durable_pins() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create_sent_email_material_authorities" in sql
    assert "provider_receipts" in sql
    assert "outcome in ('accepted', 'delivered')" in sql
    assert "approved_envelope" in sql
    assert "reply_draft_revision" in sql
    assert "reply_draft_digest" in sql
    assert "final_mime_evidence_ref" in sql
    assert "create_discarded_email_material_authority" in sql
    assert "state = 'editable'" in sql
    assert "state = 'discarded'" in sql
    assert "inbox_items" not in sql


def test_payload_digests_use_postgresql_core_sha256_without_pgcrypto() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.count("pg_catalog.sha256(pg_catalog.convert_to(") == 2
    assert "public.digest" not in sql
    assert "create extension" not in sql


def test_callback_updates_only_draft_tombstone_pointer_and_no_table_stores_raw_mail() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "accept_email_material_tombstone_callback" in sql
    assert "material_kind = 'draft'" in sql
    assert "observer_tombstone_receipt_ref" in sql
    assert "material_kind = 'final_mime'" in sql
    for forbidden in (
        "raw_content",
        "message_body",
        "address_ciphertext",
        "object_ref",
        "sender_address",
        "recipient_address",
    ):
        assert forbidden not in sql
