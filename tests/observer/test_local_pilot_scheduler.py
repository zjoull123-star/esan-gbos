from __future__ import annotations

from datetime import UTC, datetime

from observer.models import ConnectorKey, RawDelivery, TenantScope
from observer.scheduler import (
    DurablePollingScheduler,
    PollBatch,
    PollDisposition,
    PostgresPollingState,
)

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("wecom", "sales-archive")
DELIVERY = RawDelivery(
    delivery_id="delivery-001",
    exact_bytes=b"encrypted",
    media_type="application/octet-stream",
    received_at=NOW,
)


class FakePollingState:
    def __init__(self, *, fail_accept: bool = False) -> None:
        self.cursor: str | None = "10"
        self.version = 3
        self.fail_accept = fail_accept
        self.advanced: list[tuple[int, str | None]] = []
        self.health: list[tuple[str, str | None]] = []
        self.leases: list[str] = []

    def acquire(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        assert scope == SCOPE and key == KEY and now == NOW and lease_seconds == 60
        self.leases.append(f"acquire:{owner}")

    def release(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> None:
        assert scope == SCOPE and key == KEY and now == NOW
        self.leases.append(f"release:{owner}")

    def load_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> tuple[str | None, int, str]:
        assert scope == SCOPE and key == KEY
        return self.cursor, self.version, "healthy"

    def accept_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
    ) -> None:
        assert scope == SCOPE and key == KEY and delivery == DELIVERY
        if self.fail_accept:
            raise RuntimeError("disk unavailable")

    def advance_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.advanced.append((expected_version, cursor))

    def update_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> None:
        assert now == NOW
        self.health.append((status, error_code))


def test_checkpoint_advances_only_after_every_delivery_is_durably_accepted() -> None:
    state = FakePollingState()
    scheduler = DurablePollingScheduler(
        state=state,
        poll=lambda cursor, limit: PollBatch(
            disposition=PollDisposition.OK,
            expected_cursor=cursor,
            candidate_cursor="11",
            deliveries=(DELIVERY,),
        ),
        scope=SCOPE,
        key=KEY,
        clock=lambda: NOW,
        worker_id="poller-1",
    )

    result = scheduler.run_once(limit=10)

    assert result.accepted_count == 1
    assert state.advanced == [(3, "11")]
    assert state.health == [("healthy", None)]
    assert state.leases == ["acquire:poller-1", "release:poller-1"]


def test_durable_accept_failure_preserves_checkpoint_candidate() -> None:
    state = FakePollingState(fail_accept=True)
    scheduler = DurablePollingScheduler(
        state=state,
        poll=lambda cursor, limit: PollBatch(
            disposition=PollDisposition.OK,
            expected_cursor=cursor,
            candidate_cursor="11",
            deliveries=(DELIVERY,),
        ),
        scope=SCOPE,
        key=KEY,
        clock=lambda: NOW,
        worker_id="poller-1",
    )

    result = scheduler.run_once(limit=10)

    assert result.status == "retry"
    assert state.advanced == []
    assert state.health == [("degraded", "durable_accept_failed")]
    assert state.leases == ["acquire:poller-1", "release:poller-1"]


def test_postgres_polling_state_is_composed_from_storage_and_acceptor() -> None:
    state = PostgresPollingState(
        connection=object(),
        storage=object(),
        durable_accept=lambda _scope, _key, _delivery: None,
    )

    assert "connection=<redacted>" in repr(state)
    assert "storage=<redacted>" in repr(state)
