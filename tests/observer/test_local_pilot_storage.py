from __future__ import annotations

import importlib
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from observer.local_pilot_storage import (
    CheckpointConflict,
    DeliveryConflict,
    LeaseConflict,
    NonceReplay,
    OutboxConflict,
    PostgresLocalPilotStorage,
)
from observer.models import ConnectorKey, TenantScope

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services" / "observer" / "migrations" / "003_local_pilot_runtime.sql"
MIGRATION_SCRIPT = ROOT / "scripts" / "dev" / "gate3-migrate"
NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("wecom", "sales-primary")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._many: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        upper = normalized.upper()
        if ("RETURNING " in upper or upper.startswith("SELECT")) and "SET_CONFIG(" not in upper:
            response = self.connection.responses.pop(0)
            if isinstance(response, list):
                self._many = response
                self._one = response[0] if response else None
            else:
                self._one = response
                self._many = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._many


class FakeConnection:
    def __init__(
        self,
        responses: list[tuple[Any, ...] | list[tuple[Any, ...]] | None],
    ) -> None:
        self.responses = responses
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _connector_row(
    *,
    connector: str = KEY.connector,
    instance_id: str = KEY.instance_id,
    status: str = "healthy",
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        connector,
        instance_id,
        status,
        NOW,
        NOW,
    )


def _delivery_row(
    *,
    digest: str = "a" * 64,
    instance_id: str = KEY.instance_id,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        KEY.connector,
        instance_id,
        "delivery-001",
        digest,
        "application/json",
        NOW,
        "received",
        0,
        "corr-001",
        None,
        None,
        NOW,
        NOW,
    )


def _checkpoint_row(
    *,
    version: int = 0,
    cursor_value: str | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        KEY.connector,
        KEY.instance_id,
        f"{KEY.connector}:{KEY.instance_id}",
        cursor_value,
        version,
        60,
        lease_owner,
        lease_expires_at,
        None,
        None,
        "healthy",
        NOW,
    )


def _outbox_row(
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    error_code: str | None = None,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        "outbox-001",
        "01K20B8BV5C6P4YFAT8YQ3D4S5",
        "context:event-001",
        "b" * 64,
        status,
        attempt_count,
        max_attempts,
        NOW,
        lease_owner,
        lease_expires_at,
        error_code,
        NOW,
        NOW,
    )


def test_local_pilot_migration_is_metadata_only_instance_scoped_and_rls_forced() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in (
        "connector_instances",
        "inbound_deliveries",
        "inbound_delivery_events",
        "connector_checkpoints",
        "persistent_nonces",
        "processing_jobs",
        "context_publication_outbox",
        "local_pilot_quarantine",
        "local_pilot_dead_letter",
    ):
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql

    assert "add column if not exists connector_instance_id" in sql
    assert "default 'legacy-manual-import'" in sql
    assert "observation_events_site_id_connector_provider_event_id_key" in sql
    assert "site_id, connector, connector_instance_id, provider_event_id" in sql
    assert "site_id, connector, connector_instance_id, raw_sha256, occurred_minute" in sql
    assert "drop index if exists observer.observation_events_fallback_dedup_uq" in sql
    assert "gbos_observer_app" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "occurred_at" not in _table_columns(sql, "connector_checkpoints")

    forbidden_columns = re.compile(
        r"(?m)^\s*(raw_body|exact_bytes|secret|prompt|response|phone|email)\s+"
    )
    for table in (
        "connector_instances",
        "inbound_deliveries",
        "inbound_delivery_events",
        "connector_checkpoints",
        "persistent_nonces",
        "processing_jobs",
        "context_publication_outbox",
        "local_pilot_quarantine",
        "local_pilot_dead_letter",
    ):
        assert forbidden_columns.search(_table_columns(sql, table)) is None


def test_gate3_migration_runner_discovers_the_additive_local_pilot_migration() -> None:
    script = MIGRATION_SCRIPT.read_text(encoding="utf-8")

    assert "/migrations/observer/00[1-3]_*.sql" in script


def test_local_pilot_storage_exposes_provider_neutral_repository_contract() -> None:
    module = importlib.import_module("observer.local_pilot_storage")

    assert module.LocalPilotStorage
    assert module.PostgresLocalPilotStorage


def test_register_get_and_list_connector_instances_use_per_call_site_transactions() -> None:
    connection = FakeConnection(
        [
            _connector_row(),
            _connector_row(),
            [_connector_row(), _connector_row(connector="email", instance_id="support")],
        ]
    )
    repository = PostgresLocalPilotStorage(connection)

    registered = repository.register_connector_instance(
        SCOPE,
        KEY,
        now=NOW,
        replay_window_seconds=60,
    )
    loaded = repository.get_connector_instance(SCOPE, KEY)
    listed = repository.list_connector_instances(SCOPE)

    assert connection.transactions == 3
    assert [statement for statement in connection.executed if "set_config" in statement[0]] == [
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
    ]
    assert registered == loaded
    assert [item.connector for item in listed] == ["wecom", "email"]
    assert not hasattr(registered, "config")
    assert not hasattr(registered, "secret")


def test_accept_delivery_is_idempotent_for_same_digest_and_rejects_changed_body() -> None:
    connection = FakeConnection(
        [
            None,
            _delivery_row(),
            None,
            _delivery_row(digest="c" * 64),
        ]
    )
    repository = PostgresLocalPilotStorage(connection)

    replay = repository.accept_inbound_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        exact_body_sha256="a" * 64,
        media_type="application/json",
        received_at=NOW,
        correlation_id="corr-001",
    )
    assert replay.delivery_id == "delivery-001"
    assert not hasattr(replay, "body")
    with pytest.raises(DeliveryConflict, match="different body"):
        repository.accept_inbound_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
        )

    with pytest.raises(TypeError, match="exact_bytes"):
        repository.accept_inbound_delivery(  # type: ignore[call-arg]
            SCOPE,
            KEY,
            delivery_id="delivery-002",
            exact_body_sha256="a" * 64,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-002",
            exact_bytes=b"forbidden",
        )
    flattened_params = repr([params for _sql, params in connection.executed])
    assert "forbidden" not in flattened_params


def test_link_delivery_events_is_instance_scoped_and_supports_one_batch_many_events() -> None:
    connection = FakeConnection([])
    repository = PostgresLocalPilotStorage(connection)

    repository.link_delivery_events(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        provider_event_ids=("provider-001", "provider-002"),
        linked_at=NOW,
    )

    insert = next(item for item in connection.executed if "inbound_delivery_events" in item[0])
    assert "UNNEST(%s::text[])" in insert[0]
    assert insert[1] == (
        SCOPE.site_id,
        KEY.connector,
        KEY.instance_id,
        "delivery-001",
        NOW,
        ("provider-001", "provider-002"),
    )


def test_checkpoint_cas_rejects_stale_versions_and_never_uses_occurred_at_as_cursor() -> None:
    success = FakeConnection([_checkpoint_row(version=1, cursor_value="opaque:next")])
    checkpoint = PostgresLocalPilotStorage(success).compare_and_swap_checkpoint(
        SCOPE,
        KEY,
        expected_version=0,
        cursor="opaque:next",
        next_version=1,
        now=NOW,
    )
    assert checkpoint.checkpoint_version == 1
    assert checkpoint.cursor == "opaque:next"
    update_sql = success.executed[-1][0].lower()
    assert "checkpoint_version = %s" in update_sql
    assert "checkpoint_version = %s" in update_sql
    assert "occurred_at" not in update_sql

    stale = FakeConnection([None])
    with pytest.raises(CheckpointConflict, match="stale"):
        PostgresLocalPilotStorage(stale).compare_and_swap_checkpoint(
            SCOPE,
            KEY,
            expected_version=0,
            cursor="opaque:next",
            next_version=1,
            now=NOW,
        )


def test_connector_lease_enforces_owner_expiry_renewal_and_release() -> None:
    lease_expires = NOW + timedelta(seconds=30)
    acquire = FakeConnection(
        [_checkpoint_row(lease_owner="worker-a", lease_expires_at=lease_expires)]
    )
    repository = PostgresLocalPilotStorage(acquire)
    checkpoint = repository.acquire_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
        lease_seconds=30,
    )
    assert checkpoint.lease_owner == "worker-a"
    assert "lease_expires_at <= %s" in acquire.executed[-1][0]

    renew = FakeConnection(
        [
            _checkpoint_row(
                lease_owner="worker-a",
                lease_expires_at=lease_expires + timedelta(seconds=30),
            )
        ]
    )
    PostgresLocalPilotStorage(renew).renew_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
        lease_seconds=60,
    )
    assert "lease_owner = %s" in renew.executed[-1][0]
    assert "lease_expires_at > %s" in renew.executed[-1][0]

    release = FakeConnection([_checkpoint_row()])
    PostgresLocalPilotStorage(release).release_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
    )
    assert "lease_owner = NULL" in release.executed[-1][0]

    conflict = FakeConnection([None])
    with pytest.raises(LeaseConflict, match="lease"):
        PostgresLocalPilotStorage(conflict).acquire_connector_lease(
            SCOPE,
            KEY,
            owner="worker-b",
            now=NOW,
            lease_seconds=30,
        )


def test_nonce_is_hashed_consumed_once_and_requires_future_expiry() -> None:
    accepted = FakeConnection([(NOW, NOW + timedelta(minutes=5))])
    receipt = PostgresLocalPilotStorage(accepted).consume_nonce(
        SCOPE,
        identity_ref="connector:wecom:sales-primary",
        nonce="nonce-001",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert receipt.nonce_sha256 != "nonce-001"
    assert "nonce-001" not in repr(accepted.executed)

    replay = FakeConnection([None])
    with pytest.raises(NonceReplay, match="replay"):
        PostgresLocalPilotStorage(replay).consume_nonce(
            SCOPE,
            identity_ref="connector:wecom:sales-primary",
            nonce="nonce-001",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    with pytest.raises(ValueError, match="future"):
        PostgresLocalPilotStorage(FakeConnection([])).consume_nonce(
            SCOPE,
            identity_ref="connector:wecom:sales-primary",
            nonce="nonce-002",
            now=NOW,
            expires_at=NOW,
        )


def test_context_outbox_idempotency_claim_retry_dead_letter_and_replay_rejection() -> None:
    enqueue = FakeConnection([None, _outbox_row()])
    repository = PostgresLocalPilotStorage(enqueue)
    queued = repository.enqueue_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        observation_event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
        idempotency_key="context:event-001",
        payload_digest="b" * 64,
        now=NOW,
        max_attempts=3,
    )
    assert queued.status == "queued"

    conflict = FakeConnection([None, _outbox_row()])
    with pytest.raises(OutboxConflict, match="different payload"):
        PostgresLocalPilotStorage(conflict).enqueue_context_outbox(
            SCOPE,
            outbox_id="outbox-001",
            observation_event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
            idempotency_key="context:event-001",
            payload_digest="c" * 64,
            now=NOW,
            max_attempts=3,
        )

    claimed_connection = FakeConnection(
        [
            _outbox_row(
                status="leased",
                attempt_count=1,
                lease_owner="publisher-a",
                lease_expires_at=NOW + timedelta(seconds=30),
            )
        ]
    )
    claimed = PostgresLocalPilotStorage(claimed_connection).claim_context_outbox(
        SCOPE,
        worker_id="publisher-a",
        now=NOW,
        lease_seconds=30,
    )
    assert claimed is not None
    assert claimed.attempt_count == 1
    claim_sql = claimed_connection.executed[-1][0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "attempt_count < max_attempts" in claim_sql

    retry_connection = FakeConnection(
        [_outbox_row(status="retry_wait", attempt_count=1, error_code="context_unavailable")]
    )
    retry = PostgresLocalPilotStorage(retry_connection).mark_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        worker_id="publisher-a",
        now=NOW,
        published=False,
        error_code="context_unavailable",
        next_retry_at=NOW + timedelta(minutes=1),
    )
    assert retry.status == "retry_wait"
    assert "attempt_count >= max_attempts" in retry_connection.executed[-1][0]

    dead_connection = FakeConnection(
        [_outbox_row(status="dead_letter", attempt_count=3, error_code="context_rejected")]
    )
    dead = PostgresLocalPilotStorage(dead_connection).mark_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        worker_id="publisher-a",
        now=NOW,
        published=False,
        error_code="context_rejected",
        next_retry_at=NOW + timedelta(minutes=1),
    )
    assert dead.status == "dead_letter"

    replay = FakeConnection([None])
    with pytest.raises(OutboxConflict, match="lease"):
        PostgresLocalPilotStorage(replay).mark_context_outbox(
            SCOPE,
            outbox_id="outbox-001",
            worker_id="publisher-a",
            now=NOW,
            published=True,
        )


def test_health_query_returns_sanitized_site_scoped_status_only() -> None:
    connection = FakeConnection(
        [
            (
                SCOPE.site_id,
                KEY.connector,
                KEY.instance_id,
                "degraded",
                3,
                "worker-a",
                NOW + timedelta(seconds=30),
                NOW,
                "provider_timeout",
                2,
                1,
            )
        ]
    )
    health = PostgresLocalPilotStorage(connection).get_connector_health(SCOPE, KEY)

    assert health is not None
    assert health.last_error_code == "provider_timeout"
    assert health.pending_jobs == 2
    assert health.pending_outbox == 1
    assert not hasattr(health, "payload")
    assert not hasattr(health, "body")
    assert connection.executed[0] == (
        "SELECT set_config('app.site_id', %s, true)",
        (SCOPE.site_id,),
    )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda repository: repository.acquire_connector_lease(
                SCOPE,
                KEY,
                owner="worker-a",
                now=NOW,
                lease_seconds=0,
            ),
            "positive",
        ),
        (
            lambda repository: repository.compare_and_swap_checkpoint(
                SCOPE,
                KEY,
                expected_version=2,
                cursor="opaque",
                next_version=2,
                now=NOW,
            ),
            "next_version",
        ),
        (
            lambda repository: repository.accept_inbound_delivery(
                SCOPE,
                KEY,
                delivery_id="delivery-001",
                exact_body_sha256="A" * 64,
                media_type="application/json",
                received_at=NOW,
                correlation_id="corr-001",
            ),
            "sha256",
        ),
        (
            lambda repository: repository.accept_inbound_delivery(
                SCOPE,
                KEY,
                delivery_id="delivery-001",
                exact_body_sha256="a" * 64,
                media_type="application/json",
                received_at=NOW.replace(tzinfo=None),
                correlation_id="corr-001",
            ),
            "timezone-aware",
        ),
    ],
)
def test_repository_validates_bounds_and_metadata(
    call: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        call(PostgresLocalPilotStorage(FakeConnection([])))


def _table_columns(sql: str, table: str) -> str:
    start = sql.index(f"create table if not exists observer.{table}")
    end = sql.index(");", start)
    return sql[start:end]
