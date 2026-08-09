from __future__ import annotations

from datetime import UTC, datetime, timedelta

from observer.context_outbox import ContextOutboxPublisherWorker
from observer.local_pilot_storage import ContextOutboxMetadata
from observer.models import TenantScope

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


def _leased_outbox(
    *,
    status: str = "leased",
    attempt_count: int = 1,
) -> ContextOutboxMetadata:
    return ContextOutboxMetadata(
        site_id=SCOPE.site_id,
        outbox_id="outbox-001",
        observation_event_id="event-001",
        idempotency_key="context:event-001",
        payload_digest="a" * 64,
        status=status,
        attempt_count=attempt_count,
        max_attempts=3,
        next_retry_at=NOW,
        lease_owner="publisher-1",
        lease_expires_at=NOW + timedelta(minutes=1),
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeOutboxStorage:
    def __init__(self, *, final_attempt: bool = False) -> None:
        self.claimed = False
        self.final_attempt = final_attempt
        self.marked: list[tuple[bool, str | None, datetime | None]] = []

    def claim_context_outbox(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ContextOutboxMetadata | None:
        assert scope == SCOPE and worker_id == "publisher-1" and now == NOW
        assert lease_seconds == 60
        if self.claimed:
            return None
        self.claimed = True
        return _leased_outbox(attempt_count=3 if self.final_attempt else 1)

    def mark_context_outbox(
        self,
        scope: TenantScope,
        *,
        outbox_id: str,
        worker_id: str,
        now: datetime,
        published: bool,
        error_code: str | None = None,
        next_retry_at: datetime | None = None,
    ) -> ContextOutboxMetadata:
        assert scope == SCOPE and outbox_id == "outbox-001" and worker_id == "publisher-1"
        self.marked.append((published, error_code, next_retry_at))
        return _leased_outbox(
            status=(
                "published"
                if published
                else ("dead_letter" if self.final_attempt else "retry_wait")
            ),
            attempt_count=3 if self.final_attempt else 1,
        )


def test_outbox_worker_marks_success_only_after_context_accepts() -> None:
    storage = FakeOutboxStorage()
    published: list[tuple[str, str]] = []
    worker = ContextOutboxPublisherWorker(
        storage=storage,
        publisher=lambda scope, event_id, idempotency_key: published.append(
            (event_id, idempotency_key)
        ),
        worker_id="publisher-1",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None and result.status == "published"
    assert published == [("event-001", "context:event-001")]
    assert storage.marked == [(True, None, None)]


def test_outbox_worker_schedules_safe_retry_without_leaking_exception_text() -> None:
    storage = FakeOutboxStorage()

    def fail(*_args: object) -> None:
        raise RuntimeError("secret endpoint and token")

    worker = ContextOutboxPublisherWorker(
        storage=storage,
        publisher=fail,
        worker_id="publisher-1",
        clock=lambda: NOW,
        retry_delay_seconds=30,
    )

    result = worker.run_once(SCOPE)

    assert result is not None and result.status == "retry_wait"
    assert storage.marked == [(False, "context_publication_failed", NOW + timedelta(seconds=30))]


def test_outbox_worker_reports_dead_letter_after_final_failed_attempt() -> None:
    storage = FakeOutboxStorage(final_attempt=True)

    def fail(*_args: object) -> None:
        raise RuntimeError("provider failed")

    worker = ContextOutboxPublisherWorker(
        storage=storage,
        publisher=fail,
        worker_id="publisher-1",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None and result.status == "dead_letter"
