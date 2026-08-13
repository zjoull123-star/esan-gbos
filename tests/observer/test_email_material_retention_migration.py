from pathlib import Path

MIGRATION = (
    Path(__file__).parents[2]
    / "services"
    / "observer"
    / "migrations"
    / "019_email_material_retention_tombstones.sql"
)


def test_migration_pins_requests_work_receipts_and_legal_hold_events() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in (
        "email_material_retention_requests",
        "email_material_retention_work",
        "email_material_tombstone_receipts",
        "email_material_legal_hold_events",
    ):
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql
        assert f"revoke all on observer.{table} from public" in sql

    assert "terminal_at + interval '30 days'" in sql
    assert "email_draft_evidence_bindings" in sql
    assert "email_final_mime_evidence_bindings" in sql
    assert "object_ref" in sql
    assert "digest" in sql
    assert "draft_revision" in sql


def test_migration_is_append_only_and_exposes_only_fenced_mutation_functions() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "reject_email_material_retention_mutation" in sql
    assert "email_material_retention_requests_immutable" in sql
    assert "email_material_tombstone_receipts_immutable" in sql
    assert "email_material_legal_hold_events_immutable" in sql
    assert "before update or delete" in sql
    assert "register_email_material_retention" in sql
    assert "claim_email_material_retention" in sql
    assert "complete_email_material_retention" in sql
    assert "for update of work skip locked" in sql
    assert "lease_generation" in sql
    assert "lease_expires_at > p_deleted_at" in sql
    assert "status = 'leased'" in sql

    assert (
        "grant select, insert, update, delete on observer.email_material_retention_requests"
        not in sql
    )
    assert (
        "grant select, insert, update, delete on observer.email_material_tombstone_receipts"
        not in sql
    )


def test_claim_and_completion_recheck_holds_and_receipt_binds_deleted_object() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.count("email_material_legal_hold_events") >= 8
    assert "action = 'placed'" in sql
    assert "not exists" in sql
    assert "tombstone_receipt_ref" in sql
    assert "deleted_at" in sql
    assert "unique (site_id, purpose, evidence_ref)" in sql
    assert "unique (site_id, purpose, request_ref)" in sql
    assert "current_setting('app.site_id', true)" in sql


def test_public_and_app_function_grants_are_minimal() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    functions = (
        "register_email_material_retention",
        "claim_email_material_retention",
        "complete_email_material_retention",
        "resolve_email_material_tombstone",
        "email_material_has_legal_hold",
    )
    for function in functions:
        assert f"revoke all on function observer.{function}" in sql
        assert f"grant execute on function observer.{function}" in sql
    assert "to gbos_observer_app" in sql
