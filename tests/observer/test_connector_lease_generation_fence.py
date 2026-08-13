from __future__ import annotations

import inspect
from pathlib import Path

from observer.local_pilot_storage import PostgresLocalPilotStorage

ROOT = Path(__file__).parents[2]
MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "017_email_connector_lease_generation_fence.sql"
)


def test_migration_is_idempotent_fail_closed_rls_and_least_grant() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists lease_generation bigint" in sql
    assert "add column if not exists connector_lease_generation bigint" in sql
    assert "email_poll_batch_generation_backfill_ambiguous" in sql
    assert "information_schema.columns" in sql
    assert "column_name = 'lease_generation'" in sql
    assert "column_name = 'connector_lease_generation'" in sql
    assert "if not exists" in sql
    assert "lease_owner is not null" in sql
    assert "from observer.email_poll_batches" in sql
    assert "check (lease_generation >= 0)" in sql
    assert "check (connector_lease_generation >= 1)" in sql
    assert "enable row level security" in sql
    assert "force row level security" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "revoke all on observer.connector_checkpoints from public" in sql
    assert "revoke all on observer.email_poll_batches from public" in sql
    assert "grant select, insert, update on all tables" not in sql
    assert "grant select, insert, update on observer.connector_checkpoints" in sql
    assert "grant select, insert, update on observer.email_poll_batches" in sql


def test_replay_guards_skip_backfill_checks_after_columns_exist() -> None:
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())

    connector_guard = sql.index("column_name = 'lease_generation'")
    connector_ambiguity = sql.index("connector_lease_generation_backfill_ambiguous")
    batch_guard = sql.index("column_name = 'connector_lease_generation'")
    batch_ambiguity = sql.index("email_poll_batch_generation_backfill_ambiguous")
    assert connector_guard < connector_ambiguity
    assert batch_guard < batch_ambiguity


def test_migration_does_not_create_a_second_checkpoint_or_provider_cursor() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table" not in sql
    assert "provider_cursor" not in sql
    assert "gateway_checkpoint" not in sql


def test_terminal_publication_and_quarantine_require_current_batch_generation() -> None:
    publication = inspect.getsource(PostgresLocalPilotStorage.persist_normalized_batch)
    quarantine = inspect.getsource(PostgresLocalPilotStorage.quarantine_processing_job)

    for source in (publication, quarantine):
        assert "batch.connector_lease_generation" in source
        assert "checkpoint.lease_generation" in source
    assert "terminal_kind = 'published'" in publication
    assert "terminal_kind = 'quarantined'" in quarantine


def test_finalize_keeps_checkpoint_version_cas_separate_from_lease_fence() -> None:
    source = inspect.getsource(PostgresLocalPilotStorage.finalize_email_poll_batch)

    assert "checkpoint_version = %s" in source
    assert "lease_generation = %s" in source
    assert "expected_version" in source
    assert "expected_lease_generation" in source
