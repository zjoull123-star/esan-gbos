from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from .conftest import NOW, SITE

ROOT = Path(__file__).resolve().parents[2]


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()):
        self.queries.append((" ".join(query.split()), params))
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def close(self):
        pass


class _Connection:
    def __init__(self, rows=()):
        self.cursor_value = _Cursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_postgres_retention_repository_discovers_only_terminal_due_drafts(scope) -> None:
    from services.email_gateway.repositories.retention_runtime import PostgresRetentionRepository

    row = (
        "DRF-01",
        SITE,
        "EVD-01",
        "sha256:" + "a" * 64,
        NOW - timedelta(days=30),
        "TMB-01",
    )
    connection = _Connection([row])

    projections = PostgresRetentionRepository(connection).discover_due_projections(
        scope, now=NOW, limit=10
    )

    assert tuple(item.projection_ref for item in projections) == ("DRF-01",)
    query = " ".join(item[0] for item in connection.cursor_value.queries)
    assert "draft.state IN ('discarded', 'terminal')" in query
    assert "draft.legal_hold_ref IS NULL" in query
    assert "draft.observer_tombstone_receipt_ref IS NOT NULL" in query
    assert "draft.content_expires_at <= %s" in query
    assert "content_expiration_receipts" in query


def test_postgres_retention_repository_uses_fenced_claim_and_append_only_receipts() -> None:
    from services.email_gateway.repositories.retention_runtime import PostgresRetentionRepository

    source = inspect.getsource(PostgresRetentionRepository)
    assert "claim_human_retention_run" in source
    assert "lease_generation" in source
    assert "content_expiration_receipts" in source
    assert "retention_audit_events" in source
    assert "DELETE FROM" not in source.upper()
    assert "UPDATE observer." not in source
    assert "DELETE FROM observer." not in source


def test_migration_011_is_idempotent_forced_rls_immutable_and_least_privilege() -> None:
    sql = (
        ROOT / "services/email_gateway/migrations/011_email_gateway_retention_runtime.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())

    for table in ("retention_run_items", "retention_audit_events"):
        assert f"create table if not exists email_gateway.{table}" in normalized
        assert f"alter table email_gateway.{table} force row level security" in normalized
    assert "create role gbos_email_gateway_retention_worker nologin" in normalized
    assert "for update skip locked" in normalized
    assert "status = 'leased'" in normalized and "lease_expires_at <= p_now" in normalized
    assert "grant delete" not in normalized
    assert "delete from observer." not in normalized
    assert "update observer." not in normalized
    assert "alter column content_evidence_ref drop not null" not in normalized
    assert "content_expiration_receipts_immutable" in normalized
    assert "create policy reply_draft_content_expiry_scope" in normalized
    assert "as restrictive" in normalized
    assert "not exists" in normalized


def test_optional_disposable_postgres_migration_011_runs_twice() -> None:
    import os

    if os.environ.get("GBOS_RUN_EMAIL_GATEWAY_RETENTION_POSTGRES") != "1":
        pytest.skip("set GBOS_RUN_EMAIL_GATEWAY_RETENTION_POSTGRES=1 for disposable PostgreSQL")
    import psycopg

    dsn = os.environ["GBOS_EMAIL_GATEWAY_POSTGRES_DSN"]
    migration = (
        ROOT / "services/email_gateway/migrations/011_email_gateway_retention_runtime.sql"
    ).read_text(encoding="utf-8")
    with psycopg.connect(dsn, autocommit=True) as connection:
        for _ in range(2):
            connection.execute(migration)
