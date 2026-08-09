from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from observer.models import TenantScope

ROOT = Path(__file__).parents[2]
MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "010_local_pilot_identity_resolution_worker.sql"
)
NOW = datetime(2026, 8, 10, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
OTHER_SCOPE = TenantScope("beta.example", "observation_processing")
IDENTITY_REF = "extid:v1:email:opaque-token"


def _module():
    from observer import identity_resolution_work

    return identity_resolution_work


def _enqueue(repository: object, **overrides: object):
    values: dict[str, object] = {
        "identity_provider": "email",
        "identity_ref": IDENTITY_REF,
        "team_ref": "team-sales",
        "now": NOW,
        "max_attempts": 3,
    }
    values.update(overrides)
    return repository.enqueue(SCOPE, **values)  # type: ignore[attr-defined]


def test_migration_defines_closed_rls_queue_and_identity_free_metrics() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    repository_source = (
        (ROOT / "services" / "observer" / "observer" / "identity_resolution_work.py")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "create table if not exists observer.identity_resolution_work" in sql
    assert "create table if not exists observer.identity_resolution_worker_metrics" in sql
    assert "enable row level security" in sql
    assert sql.count("force row level security") >= 2
    assert "identity_resolution_work_site_isolation" in sql
    assert "identity_resolution_worker_metrics_site_isolation" in sql
    assert "for update skip locked" in repository_source
    assert "prevent_identity_resolution_work_scope_mutation" in sql
    assert "unique (site_id, identity_provider, identity_ref, team_ref)" in sql
    assert "last_resolution_status text" in sql
    assert "last_resolution_success_at timestamptz" in sql
    assert "add column if not exists last_resolution_status text" in sql
    assert "add column if not exists last_resolution_success_at timestamptz" in sql
    assert "last_resolution_status is null" in sql
    assert "last_resolution_success_at is null" in sql
    assert "last_resolution_success_at <= updated_at" in sql
    assert "to gbos_observer_app" in sql
    assert "revoke all on observer.identity_resolution_work from public" in sql
    assert "revoke all on observer.identity_resolution_worker_metrics from public" in sql
    for forbidden in (
        "subject_ref",
        "target_ref",
        "message_body",
        "display_name",
        "email_address",
        "phone_number",
        "jsonb",
    ):
        assert forbidden not in sql


def test_enqueue_is_site_scoped_idempotent_and_updates_only_last_seen() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()

    original = _enqueue(repository)
    replay = _enqueue(repository, now=NOW + timedelta(minutes=2), max_attempts=9)

    assert replay.work_id == original.work_id
    assert replay.first_seen_at == NOW
    assert replay.last_seen_at == NOW + timedelta(minutes=2)
    assert replay.max_attempts == 3
    assert replay.last_resolution_status is None
    assert replay.last_resolution_success_at is None
    assert repository.get(SCOPE, original.work_id) == replay
    assert repository.get(OTHER_SCOPE, original.work_id) is None
    assert IDENTITY_REF not in repr(replay)
    assert "identity_ref=<redacted>" in repr(replay)


@pytest.mark.parametrize("terminal", ("conflict", "dead_letter"))
def test_enqueue_never_reopens_terminal_operator_states(terminal: str) -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository, max_attempts=1)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claim is not None
    if terminal == "conflict":
        closed = repository.record_outcome(
            SCOPE,
            queued.work_id,
            worker_id="worker-1",
            fence_token=claim.fence_token,
            now=NOW + timedelta(seconds=1),
            outcome="conflict",
            latency=timedelta(milliseconds=40),
        )
    else:
        closed = repository.mark_failed(
            SCOPE,
            queued.work_id,
            worker_id="worker-1",
            fence_token=claim.fence_token,
            now=NOW + timedelta(seconds=1),
            retry_at=NOW + timedelta(minutes=1),
            error_code="resolver_unavailable",
        )
    replay = _enqueue(repository, now=NOW + timedelta(hours=1))

    assert closed.status == terminal
    assert replay.status == terminal
    assert replay.last_seen_at == NOW + timedelta(hours=1)
    assert (
        repository.claim(
            SCOPE,
            worker_id="worker-2",
            now=NOW + timedelta(days=1),
            lease_duration=timedelta(minutes=1),
        )
        is None
    )


def test_claim_is_fair_heartbeat_is_fenced_and_expiry_reclaims() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    first = _enqueue(repository, identity_ref="extid:v1:email:first", now=NOW)
    second = _enqueue(
        repository,
        identity_ref="extid:v1:email:second",
        now=NOW + timedelta(seconds=1),
    )

    claim = repository.claim(
        SCOPE,
        worker_id="worker-a",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=10),
    )
    assert claim is not None and claim.work_id == first.work_id
    assert claim.attempt_count == 1 and claim.lease_generation == 1
    extended = repository.heartbeat(
        SCOPE,
        first.work_id,
        worker_id="worker-a",
        fence_token=claim.fence_token,
        now=NOW + timedelta(seconds=3),
        lease_duration=timedelta(seconds=20),
    )
    assert extended.lease_expires_at == NOW + timedelta(seconds=23)

    other = repository.claim(
        SCOPE,
        worker_id="worker-b",
        now=NOW + timedelta(seconds=4),
        lease_duration=timedelta(seconds=10),
    )
    assert other is not None and other.work_id == second.work_id
    reclaimed = repository.claim(
        SCOPE,
        worker_id="worker-c",
        now=NOW + timedelta(seconds=24),
        lease_duration=timedelta(seconds=10),
    )
    assert reclaimed is not None and reclaimed.work_id == first.work_id
    assert reclaimed.attempt_count == 2 and reclaimed.lease_generation == 2
    with pytest.raises(module.IdentityResolutionLeaseConflict):
        repository.heartbeat(
            SCOPE,
            first.work_id,
            worker_id="worker-a",
            fence_token=claim.fence_token,
            now=NOW + timedelta(seconds=25),
            lease_duration=timedelta(seconds=10),
        )


def test_failures_are_bounded_and_accept_only_deterministic_safe_codes() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository, max_attempts=2)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claim is not None
    with pytest.raises(ValueError, match="error code") as caught:
        repository.mark_failed(
            SCOPE,
            queued.work_id,
            worker_id="worker-1",
            fence_token=claim.fence_token,
            now=NOW + timedelta(seconds=1),
            retry_at=NOW + timedelta(minutes=1),
            error_code="upstream said alice@example.invalid",
        )
    assert "alice@example.invalid" not in str(caught.value)

    retried = repository.mark_failed(
        SCOPE,
        queued.work_id,
        worker_id="worker-1",
        fence_token=claim.fence_token,
        now=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(minutes=1),
        error_code="resolver_unavailable",
    )
    assert retried.status == "retry_wait"
    final_claim = repository.claim(
        SCOPE,
        worker_id="worker-2",
        now=NOW + timedelta(minutes=1),
        lease_duration=timedelta(minutes=1),
    )
    assert final_claim is not None
    dead = repository.mark_failed(
        SCOPE,
        queued.work_id,
        worker_id="worker-2",
        fence_token=final_claim.fence_token,
        now=NOW + timedelta(minutes=1, seconds=1),
        retry_at=NOW + timedelta(minutes=2),
        error_code="invalid_resolver_response",
    )
    assert dead.status == "dead_letter"


def test_expired_final_attempt_is_dead_lettered_instead_of_staying_leased() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository, max_attempts=1)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=5),
    )
    assert claim is not None

    assert (
        repository.claim(
            SCOPE,
            worker_id="worker-2",
            now=NOW + timedelta(seconds=6),
            lease_duration=timedelta(seconds=5),
        )
        is None
    )
    expired = repository.get(SCOPE, queued.work_id)
    assert expired is not None
    assert expired.status == "dead_letter"
    assert expired.last_error_code == "resolver_timeout"


@pytest.mark.parametrize("outcome", ("unresolved", "confirmed", "revoked"))
def test_success_outcomes_schedule_freshness_rechecks(outcome: str) -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claim is not None
    resolved = repository.record_outcome(
        SCOPE,
        queued.work_id,
        worker_id="worker-1",
        fence_token=claim.fence_token,
        now=NOW + timedelta(seconds=1),
        outcome=outcome,
        latency=timedelta(milliseconds=120),
        recheck_at=NOW + timedelta(hours=6),
    )

    assert resolved.status == outcome
    assert resolved.attempt_count == 0
    assert resolved.next_attempt_at == NOW + timedelta(hours=6)
    assert resolved.last_resolution_status == outcome
    assert resolved.last_resolution_success_at == NOW + timedelta(seconds=1)
    assert (
        repository.claim(
            SCOPE,
            worker_id="worker-2",
            now=NOW + timedelta(hours=5),
            lease_duration=timedelta(minutes=1),
        )
        is None
    )
    recheck = repository.claim(
        SCOPE,
        worker_id="worker-2",
        now=NOW + timedelta(hours=6),
        lease_duration=timedelta(minutes=1),
    )
    assert recheck is not None and recheck.attempt_count == 1
    assert recheck.last_resolution_status == outcome
    assert recheck.last_resolution_success_at == NOW + timedelta(seconds=1)


def test_transient_retry_preserves_confirmed_success_until_a_new_outcome_replaces_it() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository)
    initial_claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert initial_claim is not None
    confirmed = repository.record_outcome(
        SCOPE,
        queued.work_id,
        worker_id="worker-1",
        fence_token=initial_claim.fence_token,
        now=NOW + timedelta(seconds=1),
        outcome="confirmed",
        latency=timedelta(milliseconds=50),
        recheck_at=NOW + timedelta(hours=1),
    )

    replay = _enqueue(repository, now=NOW + timedelta(minutes=30))
    assert replay.last_resolution_status == "confirmed"
    assert replay.last_resolution_success_at == confirmed.last_resolution_success_at
    recheck = repository.claim(
        SCOPE,
        worker_id="worker-2",
        now=NOW + timedelta(hours=1),
        lease_duration=timedelta(minutes=1),
    )
    assert recheck is not None
    assert recheck.last_resolution_status == "confirmed"
    heartbeat = repository.heartbeat(
        SCOPE,
        queued.work_id,
        worker_id="worker-2",
        fence_token=recheck.fence_token,
        now=NOW + timedelta(hours=1, milliseconds=500),
        lease_duration=timedelta(minutes=1),
    )
    assert heartbeat.last_resolution_status == "confirmed"
    assert heartbeat.last_resolution_success_at == confirmed.last_resolution_success_at
    retried = repository.mark_failed(
        SCOPE,
        queued.work_id,
        worker_id="worker-2",
        fence_token=recheck.fence_token,
        now=NOW + timedelta(hours=1, seconds=1),
        retry_at=NOW + timedelta(hours=1, minutes=1),
        error_code="resolver_unavailable",
    )
    assert retried.last_resolution_status == "confirmed"
    assert retried.last_resolution_success_at == confirmed.last_resolution_success_at

    replacement_claim = repository.claim(
        SCOPE,
        worker_id="worker-3",
        now=NOW + timedelta(hours=1, minutes=1),
        lease_duration=timedelta(minutes=1),
    )
    assert replacement_claim is not None
    unresolved = repository.record_outcome(
        SCOPE,
        queued.work_id,
        worker_id="worker-3",
        fence_token=replacement_claim.fence_token,
        now=NOW + timedelta(hours=1, minutes=1, seconds=1),
        outcome="unresolved",
        latency=timedelta(milliseconds=60),
        recheck_at=NOW + timedelta(hours=2),
    )
    assert unresolved.last_resolution_status == "unresolved"
    assert unresolved.last_resolution_success_at == NOW + timedelta(hours=1, minutes=1, seconds=1)


def test_conflict_never_fabricates_a_successful_resolution() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    queued = _enqueue(repository)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert claim is not None
    conflict = repository.record_outcome(
        SCOPE,
        queued.work_id,
        worker_id="worker-1",
        fence_token=claim.fence_token,
        now=NOW + timedelta(seconds=1),
        outcome="conflict",
        latency=timedelta(milliseconds=50),
    )

    assert conflict.last_resolution_status is None
    assert conflict.last_resolution_success_at is None


def test_snapshot_is_low_cardinality_and_contains_no_identity_labels() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()
    _enqueue(repository, identity_ref="extid:v1:email:one", now=NOW)
    second = _enqueue(
        repository,
        identity_ref="extid:v1:email:two",
        now=NOW + timedelta(seconds=1),
    )
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW + timedelta(seconds=2),
        lease_duration=timedelta(minutes=1),
    )
    assert claim is not None
    repository.record_outcome(
        SCOPE,
        claim.work_id,
        worker_id="worker-1",
        fence_token=claim.fence_token,
        now=NOW + timedelta(seconds=3),
        outcome="unresolved",
        latency=timedelta(milliseconds=80),
        recheck_at=NOW + timedelta(hours=1),
    )
    repository.record_worker_heartbeat(SCOPE, now=NOW + timedelta(seconds=4))

    snapshot = repository.snapshot(
        SCOPE,
        now=NOW + timedelta(seconds=5),
        readiness_window=timedelta(seconds=30),
    )

    assert snapshot.ready is True
    assert snapshot.backlog_count == 1
    assert snapshot.oldest_backlog_age_seconds == 4
    assert snapshot.unresolved_count == 1
    assert snapshot.conflict_count == 0
    assert snapshot.request_outcomes == {
        "confirmed": 0,
        "conflict": 0,
        "error": 0,
        "revoked": 0,
        "unresolved": 1,
    }
    assert snapshot.latency_buckets == {
        "le_100_ms": 1,
        "le_500_ms": 0,
        "le_2000_ms": 0,
        "gt_2000_ms": 0,
    }
    rendered = repr(snapshot)
    assert IDENTITY_REF not in rendered
    assert second.identity_ref not in rendered
    assert not hasattr(snapshot, "identity_ref")
    assert not hasattr(snapshot, "team_ref")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        if (
            (
                "returning" in normalized.lower()
                and "observer.identity_resolution_work" in normalized.lower()
            )
            or normalized.lower().startswith("select")
            and "set_config(" not in normalized
        ):
            self._one = self.connection.responses.pop(0)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one


class FakeConnection:
    def __init__(self, responses: list[tuple[Any, ...] | None]) -> None:
        self.responses = responses
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _work_row(**overrides: object) -> tuple[Any, ...]:
    values: dict[str, object] = {
        "site_id": SCOPE.site_id,
        "work_id": "IRW-" + "a" * 64,
        "identity_provider": "email",
        "identity_ref": IDENTITY_REF,
        "team_ref": "team-sales",
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": 3,
        "next_attempt_at": NOW,
        "lease_owner": None,
        "lease_expires_at": None,
        "lease_generation": 0,
        "last_error_code": None,
        "last_resolution_status": None,
        "last_resolution_success_at": None,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return tuple(values.values())


def test_postgres_repository_uses_one_scoped_transaction_and_skip_locked_claim() -> None:
    module = _module()
    queued = _work_row()
    leased = _work_row(
        status="leased",
        attempt_count=1,
        lease_owner="worker-1",
        lease_expires_at=NOW + timedelta(minutes=1),
        lease_generation=1,
        updated_at=NOW,
    )
    connection = FakeConnection([queued, leased])
    repository = module.PostgresIdentityResolutionWorkRepository(connection)

    item = _enqueue(repository)
    claim = repository.claim(
        SCOPE,
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(minutes=1),
    )

    assert item.status == "queued"
    assert claim is not None and claim.status == "leased"
    assert connection.transactions == 2
    scope_statements = [
        params for sql, params in connection.executed if "set_config('app.site_id'" in sql
    ]
    assert scope_statements == [(SCOPE.site_id,), (SCOPE.site_id,)]
    claim_sql = next(sql for sql, _ in connection.executed if "SKIP LOCKED" in sql)
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "lease_generation = work.lease_generation + 1" in claim_sql
    assert "status IN ('queued', 'retry_wait', 'unresolved', 'confirmed', 'revoked')" in claim_sql
    assert "status = 'dead_letter'" in claim_sql
    assert "last_error_code = 'resolver_timeout'" in claim_sql


def test_postgres_repository_rejects_forged_fence_before_database_access() -> None:
    module = _module()
    connection = FakeConnection([])
    repository = module.PostgresIdentityResolutionWorkRepository(connection)

    with pytest.raises(module.IdentityResolutionLeaseConflict):
        repository.heartbeat(
            SCOPE,
            "IRW-" + "a" * 64,
            worker_id="worker-1",
            fence_token="v1:1:1:" + "0" * 64,
            now=NOW,
            lease_duration=timedelta(minutes=1),
        )

    assert connection.transactions == 0


def test_work_item_validation_rejects_raw_or_mutable_identity_fields() -> None:
    module = _module()
    repository = module.InMemoryIdentityResolutionWorkRepository()

    for identity_ref in (
        "alice@example.invalid",
        "extid:v1:email:alice@example.invalid",
        "extid:v1:email:13800138000",
        "extid:v1:wecom:opaque-token",
    ):
        with pytest.raises(ValueError):
            _enqueue(repository, identity_ref=identity_ref)
    item = _enqueue(repository)
    assert not hasattr(item, "target_ref")
    assert not hasattr(item, "message_body")
    with pytest.raises(ValueError, match="last resolution"):
        replace(item, last_resolution_status="confirmed")
    with pytest.raises(ValueError, match="last resolution"):
        replace(
            item,
            last_resolution_status="confirmed",
            last_resolution_success_at=item.updated_at + timedelta(seconds=1),
        )
    with pytest.raises((AttributeError, TypeError)):
        item.site_id = OTHER_SCOPE.site_id  # type: ignore[misc]
