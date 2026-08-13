from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "018_email_draft_material_evidence_binding.sql"
)


def _sql() -> str:
    assert MIGRATION.is_file(), "migration 018 must durably close draft material"
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_migration_is_idempotent_append_only_forced_rls_and_least_privilege() -> None:
    sql = _sql()
    tables = (
        "email_draft_material_receipts",
        "email_draft_evidence_bindings",
        "email_final_mime_evidence_bindings",
    )
    for table in tables:
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql
        assert f"revoke all on observer.{table} from public" in sql
        assert f"grant select, insert on observer.{table} to gbos_observer_app" in sql
    assert "before update or delete" in sql
    assert "drop trigger if exists email_draft_material_receipts_immutable" in sql
    assert "drop trigger if exists email_draft_evidence_bindings_immutable" in sql
    assert "drop trigger if exists email_final_mime_evidence_bindings_immutable" in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql
    assert "valid_email_draft_material_response(operation, response)" in sql
    assert (
        "grant execute on function observer.valid_email_draft_material_response(text, jsonb) "
        "to gbos_observer_app" in sql
    )
    assert "response_value - array['evidence_ref', 'digest', 'revision']" in sql
    assert (
        "response_value - array[ 'evidence_ref', 'digest', 'role_binding', 'participants' ]" in sql
    )
    assert "jsonb_array_elements(response_value->'participants')" in sql
    assert "opaque_address_ref" in sql
    assert "raw_address" not in sql
    assert "content text" not in sql
