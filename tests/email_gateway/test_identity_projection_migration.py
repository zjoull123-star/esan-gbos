from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "services/email_gateway/migrations/012_identity_projection_purpose_scope.sql"
OBSERVER_MIGRATION = ROOT / "services/observer/migrations/020_identity_projection_outbox.sql"


def test_gateway_migration_rekeys_identity_revision_by_exact_purpose() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    normalized = " ".join(sql.split())

    assert "from pg_constraint" in sql
    assert "pg_get_constraintdef" in sql
    assert (
        "site_id, processing_purpose, opaque_address_ref, external_identity_revision" in normalized
    )
    assert "force row level security" in sql
    assert "revoke all on email_gateway.identity_projection_receipts from public" in sql
    assert "grant select, insert" in sql
    assert "target_ref" not in sql


def test_observer_migration_defines_fenced_least_privilege_outbox() -> None:
    sql = OBSERVER_MIGRATION.read_text(encoding="utf-8").lower()

    assert "create role gbos_observer_identity_projector" in sql
    assert "create table if not exists observer.identity_projection_outbox" in sql
    assert "attempt_count between 0 and 5" in sql
    assert "lease_generation" in sql
    assert "jsonb_object_length" not in sql
    assert "payload - array[" in sql
    assert "force row level security" in sql
    assert "grant select" in sql
    assert "grant update (" in sql
    assert "target_ref" not in sql
