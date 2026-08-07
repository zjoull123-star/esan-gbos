from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .local_pilot_storage import ContextOutboxMetadata
from .models import TenantScope, _require_aware


class ContextOutboxStorage(Protocol):
    def claim_context_outbox(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ContextOutboxMetadata | None: ...

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
    ) -> ContextOutboxMetadata: ...


ContextEventPublisher = Callable[[TenantScope, str, str], None]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class ContextPublicationResult:
    outbox_id: str
    observation_event_id: str
    status: str


class ContextOutboxPublisherWorker:
    """Claims and publishes one durable outbox row without external-send capability."""

    __slots__ = (
        "_clock",
        "_lease_seconds",
        "_publisher",
        "_retry_delay_seconds",
        "_storage",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        storage: ContextOutboxStorage,
        publisher: ContextEventPublisher,
        worker_id: str,
        clock: Clock,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 256:
            raise ValueError("invalid worker_id")
        if not callable(publisher) or not callable(clock):
            raise TypeError("publisher and clock must be callable")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("invalid lease_seconds")
        if not 1 <= retry_delay_seconds <= 86_400:
            raise ValueError("invalid retry_delay_seconds")
        self._storage = storage
        self._publisher = publisher
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    def run_once(self, scope: TenantScope) -> ContextPublicationResult | None:
        now = self._now()
        claimed = self._storage.claim_context_outbox(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return None
        if (
            claimed.site_id != scope.site_id
            or claimed.status != "leased"
            or claimed.lease_owner != self._worker_id
        ):
            raise RuntimeError("outbox claim crossed its site or lease boundary")
        try:
            self._publisher(
                scope,
                claimed.observation_event_id,
                claimed.idempotency_key,
            )
        except Exception:
            marked = self._storage.mark_context_outbox(
                scope,
                outbox_id=claimed.outbox_id,
                worker_id=self._worker_id,
                now=now,
                published=False,
                error_code="context_publication_failed",
                next_retry_at=now + timedelta(seconds=self._retry_delay_seconds),
            )
            return ContextPublicationResult(
                outbox_id=claimed.outbox_id,
                observation_event_id=claimed.observation_event_id,
                status=marked.status,
            )
        self._storage.mark_context_outbox(
            scope,
            outbox_id=claimed.outbox_id,
            worker_id=self._worker_id,
            now=now,
            published=True,
        )
        return ContextPublicationResult(
            outbox_id=claimed.outbox_id,
            observation_event_id=claimed.observation_event_id,
            status="published",
        )

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now
