from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.local_pilot_runtime.model_projection_worker import ProjectionLeaseConflict
from services.observer.observer.models import TenantScope
from services.observer.observer.projection_outbox import PostgresProjectionOutboxRepository

NOW = datetime(2026, 8, 8, 10, tzinfo=UTC)
SCOPE = TenantScope("gbos.localhost", "observation_processing")


def test_projection_fencing_migration_is_idempotent_rls_locked_and_minimal() -> None:
    migration = (
        (
            Path(__file__).parents[2]
            / "services"
            / "observer"
            / "migrations"
            / "008_local_pilot_projection_fencing.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "add column if not exists lease_generation bigint" in migration
    assert "check (lease_generation >= 0)" in migration
    assert "force row level security" in migration
    assert "current_setting('app.site_id', true)" in migration
    assert "current_setting('app.processing_purpose', true)" in migration
    assert "create index if not exists" in migration
    assert "revoke all on observer.context_publication_outbox from public" in migration
    for forbidden in ("payload_body", "prompt_text", "response_text", "identity_value"):
        assert forbidden not in migration


class _Transaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0)


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.cursor_value = _Cursor(rows)

    def transaction(self) -> _Transaction:
        return _Transaction()

    def cursor(self) -> _Cursor:
        return self.cursor_value


def _claim_row(*, attempt: int = 1, generation: int = 1) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        "outbox-1",
        "observation-1",
        "context-normalized:1",
        "leased",
        attempt,
        3,
        "worker-1",
        NOW + timedelta(minutes=1),
        generation,
    )


def test_claim_is_skip_locked_purpose_scoped_and_returns_opaque_generation_fence() -> None:
    connection = _Connection([_claim_row()])
    repository = PostgresProjectionOutboxRepository(connection)

    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )

    assert claim is not None
    assert claim.attempt == 1
    assert "outbox-1" not in claim.fence_token
    assert "worker-1" not in claim.fence_token
    sql = "\n".join(item[0] for item in connection.cursor_value.executed).lower()
    assert "set_config('app.site_id'" in sql
    assert "set_config('app.processing_purpose'" in sql
    assert "for update of outbox skip locked" in sql
    assert "lease_generation = outbox.lease_generation + 1" in sql
    assert "event.processing_purpose = %s" in sql


def test_stale_generation_is_rejected_even_for_same_worker_aba() -> None:
    first_connection = _Connection([_claim_row(attempt=1, generation=1)])
    first_repository = PostgresProjectionOutboxRepository(first_connection)
    first = first_repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=5),
    )
    assert first is not None

    reclaimed_connection = _Connection([_claim_row(attempt=2, generation=2)])
    reclaimed_repository = PostgresProjectionOutboxRepository(reclaimed_connection)
    reclaimed = reclaimed_repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW + timedelta(seconds=6),
        lease_duration=timedelta(seconds=5),
    )
    assert reclaimed is not None
    assert first.fence_token != reclaimed.fence_token

    stale_connection = _Connection([None])
    stale_repository = PostgresProjectionOutboxRepository(stale_connection)
    with pytest.raises(ProjectionLeaseConflict):
        stale_repository.mark_published(
            SCOPE,
            first.outbox_id,
            worker_id="worker-1",
            expected_attempt=first.attempt,
            fence_token=first.fence_token,
            now=NOW + timedelta(seconds=7),
        )
    sql, params = stale_connection.cursor_value.executed[-1]
    assert "lease_generation = %s" in sql
    assert 1 in (params or ())


def test_heartbeat_and_terminal_transitions_require_live_attempt_and_generation() -> None:
    claimed = PostgresProjectionOutboxRepository(_Connection([_claim_row()])).claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claimed is not None
    heartbeat_connection = _Connection([_claim_row()])
    repository = PostgresProjectionOutboxRepository(heartbeat_connection)

    repository.heartbeat(
        SCOPE,
        claimed.outbox_id,
        worker_id="worker-1",
        expected_attempt=1,
        fence_token=claimed.fence_token,
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )

    sql = heartbeat_connection.cursor_value.executed[-1][0].lower()
    for predicate in (
        "status = 'leased'",
        "lease_owner = %s",
        "attempt_count = %s",
        "lease_generation = %s",
        "lease_expires_at > %s",
    ):
        assert predicate in sql


def test_final_failed_attempt_enters_dead_letter_without_sensitive_error() -> None:
    claimed = PostgresProjectionOutboxRepository(
        _Connection([_claim_row(attempt=3, generation=4)])
    ).claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claimed is not None
    connection = _Connection([("dead_letter", 3), None])
    repository = PostgresProjectionOutboxRepository(connection)

    status = repository.mark_failed(
        SCOPE,
        claimed.outbox_id,
        worker_id="worker-1",
        expected_attempt=3,
        fence_token=claimed.fence_token,
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
        error_code="projection_failed",
    )

    assert status == "dead_letter"
    sql = "\n".join(statement for statement, _ in connection.cursor_value.executed).lower()
    assert "local_pilot_dead_letter" in sql
    assert "on conflict" in sql
    rendered = repr(repository)
    assert "worker-1" not in rendered
    assert "observation-1" not in rendered
