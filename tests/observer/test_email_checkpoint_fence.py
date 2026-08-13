from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from observer.email_checkpoint_fence import (
    EmailCheckpointFenceConflict,
    InMemoryEmailCheckpointFence,
)
from observer.models import ConnectorKey, RawDelivery, TenantScope
from observer.scheduler import DurablePollingScheduler, PollBatch, PollDisposition

NOW = datetime(2026, 8, 13, 11, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("email", "sales-mailbox")
DELIVERY = RawDelivery("delivery-001", b"mime", "message/rfc822", NOW)


class FencedState:
    def __init__(self) -> None:
        self.cursor: str | None = "10"
        self.version = 4
        self.fence = InMemoryEmailCheckpointFence()
        self.accepted: list[str] = []
        self.health: list[tuple[str, str | None]] = []

    def acquire(self, *args: object, **kwargs: object) -> int:
        return 1

    def release(self, *args: object, **kwargs: object) -> None:
        assert kwargs["lease_generation"] == 1

    def load_checkpoint(self, *args: object) -> tuple[str | None, int, str]:
        return self.cursor, self.version, "healthy"

    def register_poll_batch(
        self,
        scope,
        key,
        batch,
        *,
        expected_version,
        owner,
        lease_generation,
        now,
    ):
        assert owner == "email-poller"
        assert lease_generation == 1
        return self.fence.register(
            scope,
            key,
            expected_cursor=batch.expected_cursor,
            candidate_cursor=batch.candidate_cursor,
            expected_version=expected_version,
            lease_generation=1,
            delivery_ids=tuple(value.delivery_id for value in batch.deliveries),
            now=now,
        )

    def accept_delivery(self, scope, key, delivery, *, batch_id, owner, lease_generation, now):
        assert owner == "email-poller"
        assert lease_generation == 1
        del scope, key, batch_id
        if delivery.delivery_id not in self.accepted:
            self.accepted.append(delivery.delivery_id)

    def finalize_poll_batch(
        self,
        scope,
        key,
        *,
        batch_id,
        expected_version,
        owner,
        lease_generation,
        now,
    ):
        assert owner == "email-poller"
        assert lease_generation == 1
        finalized = self.fence.finalize(
            scope,
            key,
            batch_id=batch_id,
            expected_version=expected_version,
            lease_generation=1,
            now=now,
        )
        if finalized:
            self.cursor = "11"
            self.version += 1
        return finalized

    def update_health(self, scope, key, *, status, error_code, now):
        del scope, key, now
        self.health.append((status, error_code))


def _scheduler(state: FencedState) -> DurablePollingScheduler:
    return DurablePollingScheduler(
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
        worker_id="email-poller",
    )


def test_accepted_delivery_then_publication_crash_does_not_advance_and_recovers() -> None:
    state = FencedState()

    crashed = _scheduler(state).run_once(limit=10)
    assert crashed.accepted_count == 1
    assert crashed.checkpoint_advanced is False
    assert state.cursor == "10"

    batch_id = state.fence.batches[0].batch_id
    state.fence.mark_publication_terminal(
        SCOPE,
        KEY,
        batch_id=batch_id,
        delivery_id=DELIVERY.delivery_id,
        terminal_ref="PUB-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        lease_generation=1,
        now=NOW,
    )
    recovered = _scheduler(state).run_once(limit=10)

    assert recovered.checkpoint_advanced is True
    assert state.cursor == "11"
    assert state.accepted == ["delivery-001"]


def test_partial_batch_and_stale_generation_are_retryable() -> None:
    fence = InMemoryEmailCheckpointFence()
    batch = fence.register(
        SCOPE,
        KEY,
        expected_cursor="10",
        candidate_cursor="12",
        expected_version=4,
        lease_generation=1,
        delivery_ids=("delivery-001", "delivery-002"),
        now=NOW,
    )
    fence.mark_quarantine_terminal(
        SCOPE,
        KEY,
        batch_id=batch.batch_id,
        delivery_id="delivery-001",
        terminal_ref="quarantine-safe-ref",
        lease_generation=1,
        now=NOW,
    )

    assert (
        fence.finalize(
            SCOPE,
            KEY,
            batch_id=batch.batch_id,
            expected_version=4,
            lease_generation=1,
            now=NOW,
        )
        is False
    )
    assert (
        fence.finalize(
            SCOPE,
            KEY,
            batch_id=batch.batch_id,
            expected_version=5,
            lease_generation=1,
            now=NOW,
        )
        is False
    )


def test_concurrent_takeover_rejects_stale_generation_terminal_write() -> None:
    fence = InMemoryEmailCheckpointFence()
    first = fence.register(
        SCOPE,
        KEY,
        expected_cursor="10",
        candidate_cursor="11",
        expected_version=4,
        lease_generation=1,
        delivery_ids=(DELIVERY.delivery_id,),
        now=NOW,
    )
    stale_ready = Event()
    takeover_done = Event()

    def stale_worker() -> str:
        stale_ready.set()
        assert takeover_done.wait(timeout=2)
        with pytest.raises(EmailCheckpointFenceConflict) as raised:
            fence.mark_publication_terminal(
                SCOPE,
                KEY,
                batch_id=first.batch_id,
                delivery_id=DELIVERY.delivery_id,
                terminal_ref="PUB-stale",
                lease_generation=1,
                now=NOW,
            )
        return raised.value.code

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(stale_worker)
        assert stale_ready.wait(timeout=2)
        taken_over = fence.register(
            SCOPE,
            KEY,
            expected_cursor="10",
            candidate_cursor="11",
            expected_version=4,
            lease_generation=2,
            delivery_ids=(DELIVERY.delivery_id,),
            now=NOW,
        )
        takeover_done.set()

    assert taken_over.lease_generation == 2
    assert stale.result(timeout=2) == "poll_batch_lease_generation_stale"
    assert fence.batches[0].members[0].terminal_kind is None
