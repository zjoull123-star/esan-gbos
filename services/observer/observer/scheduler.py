from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .email_checkpoint_fence import EmailPollBatchFence
from .local_pilot_storage import LocalPilotStorage
from .models import ConnectorKey, RawDelivery, TenantScope, _require_aware
from .storage import Connection


class PollDisposition(StrEnum):
    OK = "ok"
    RETRY = "retry"
    PAUSE = "pause"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PollBatch:
    disposition: PollDisposition
    expected_cursor: str | None
    candidate_cursor: str | None
    deliveries: tuple[RawDelivery, ...]
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.deliveries, tuple) or not all(
            isinstance(delivery, RawDelivery) for delivery in self.deliveries
        ):
            raise TypeError("deliveries must contain RawDelivery values")
        if self.disposition is PollDisposition.OK:
            if self.error_code is not None:
                raise ValueError("successful poll cannot contain an error")
            if self.deliveries and self.candidate_cursor is None:
                raise ValueError("deliveries require a checkpoint candidate")
        elif not self.error_code or len(self.error_code) > 80:
            raise ValueError("failed poll requires a safe error code")


@dataclass(frozen=True, slots=True)
class PollRunResult:
    status: str
    accepted_count: int
    checkpoint_advanced: bool
    safe_error_code: str | None


class PollingState(Protocol):
    """Durable state seam; accept_delivery must return only after durable commit."""

    def acquire(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> None: ...

    def release(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> None: ...

    def load_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> tuple[str | None, int, str]: ...

    def accept_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        batch_id: str | None = None,
    ) -> None: ...

    def register_poll_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        batch: PollBatch,
        *,
        expected_version: int,
        now: datetime,
    ) -> EmailPollBatchFence: ...

    def finalize_poll_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        expected_version: int,
        now: datetime,
    ) -> bool: ...

    def advance_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        now: datetime,
    ) -> None: ...

    def update_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> None: ...


DurableAccept = Callable[[TenantScope, ConnectorKey, RawDelivery], None]


class PostgresPollingState:
    """Polling state adapter over the durable local-pilot storage transaction API."""

    __slots__ = ("_connection", "_durable_accept", "_storage")

    def __init__(
        self,
        *,
        connection: Connection,
        storage: LocalPilotStorage,
        durable_accept: DurableAccept,
    ) -> None:
        if not callable(durable_accept):
            raise TypeError("durable_accept must be callable")
        self._connection = connection
        self._storage = storage
        self._durable_accept = durable_accept

    def __repr__(self) -> str:
        return (
            "PostgresPollingState("
            "connection=<redacted>, storage=<redacted>, durable_accept=<redacted>)"
        )

    def acquire(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        self._storage.acquire_connector_lease(
            scope,
            key,
            owner=owner,
            now=now,
            lease_seconds=lease_seconds,
        )

    def release(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        owner: str,
        now: datetime,
    ) -> None:
        self._storage.release_connector_lease(
            scope,
            key,
            owner=owner,
            now=now,
        )

    def load_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
    ) -> tuple[str | None, int, str]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            cursor.execute(
                """
                SELECT checkpoint.cursor_value,
                       checkpoint.checkpoint_version,
                       CASE
                         WHEN instance.status = 'paused' THEN 'paused'
                         ELSE checkpoint.status
                       END
                FROM observer.connector_checkpoints AS checkpoint
                JOIN observer.connector_instances AS instance
                  ON instance.site_id = checkpoint.site_id
                 AND instance.connector = checkpoint.connector
                 AND instance.connector_instance_id =
                     checkpoint.connector_instance_id
                WHERE checkpoint.site_id = %s
                  AND checkpoint.connector = %s
                  AND checkpoint.connector_instance_id = %s
                """,
                (scope.site_id, key.connector, key.instance_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise LookupError("connector checkpoint does not exist")
        return (
            None if row[0] is None else str(row[0]),
            int(row[1]),
            str(row[2]),
        )

    def accept_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        batch_id: str | None = None,
    ) -> None:
        del batch_id
        self._durable_accept(scope, key, delivery)

    def register_poll_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        batch: PollBatch,
        *,
        expected_version: int,
        now: datetime,
    ) -> EmailPollBatchFence:
        return self._storage.register_email_poll_batch(
            scope,
            key,
            expected_cursor=batch.expected_cursor,
            candidate_cursor=batch.candidate_cursor,
            expected_version=expected_version,
            delivery_ids=tuple(dict.fromkeys(value.delivery_id for value in batch.deliveries)),
            delivery_received_at=tuple(
                {value.delivery_id: value.received_at for value in batch.deliveries}.values()
            ),
            now=now,
        )

    def finalize_poll_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        expected_version: int,
        now: datetime,
    ) -> bool:
        return self._storage.finalize_email_poll_batch(
            scope,
            key,
            batch_id=batch_id,
            expected_version=expected_version,
            now=now,
        )

    def advance_checkpoint(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_version: int,
        cursor: str | None,
        now: datetime,
    ) -> None:
        self._storage.compare_and_swap_checkpoint(
            scope,
            key,
            expected_version=expected_version,
            cursor=cursor,
            next_version=expected_version + 1,
            now=now,
        )

    def update_health(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        status: str,
        error_code: str | None,
        now: datetime,
    ) -> None:
        self._storage.update_checkpoint_health(
            scope,
            key,
            status=status,
            now=now,
            last_success_at=(now if status == "healthy" else None),
            error_code=error_code,
        )


Poll = Callable[[str | None, int], PollBatch]
Clock = Callable[[], datetime]


class DurablePollingScheduler:
    """Runs one pull batch and commits its candidate only after durable acceptance."""

    __slots__ = (
        "_clock",
        "_key",
        "_lease_seconds",
        "_poll",
        "_scope",
        "_state",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        state: PollingState,
        poll: Poll,
        scope: TenantScope,
        key: ConnectorKey,
        clock: Clock,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        if key.connector not in {"email", "wecom"}:
            raise ValueError("polling scheduler supports only email and wecom")
        if not callable(poll) or not callable(clock):
            raise TypeError("poll and clock must be callable")
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 256:
            raise ValueError("invalid worker_id")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("invalid lease_seconds")
        self._state = state
        self._poll = poll
        self._scope = scope
        self._key = key
        self._clock = clock
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def run_once(self, *, limit: int) -> PollRunResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        _, _, status = self._state.load_checkpoint(self._scope, self._key)
        if status == "paused":
            return PollRunResult(
                status="paused",
                accepted_count=0,
                checkpoint_advanced=False,
                safe_error_code="connector_paused",
            )
        now = self._now()
        try:
            self._state.acquire(
                self._scope,
                self._key,
                owner=self._worker_id,
                now=now,
                lease_seconds=self._lease_seconds,
            )
        except Exception:
            return self._record_failure(now, "lease_unavailable", paused=False)
        try:
            leased_cursor, leased_version, leased_status = self._state.load_checkpoint(
                self._scope,
                self._key,
            )
            if leased_status == "paused":
                result = PollRunResult(
                    status="paused",
                    accepted_count=0,
                    checkpoint_advanced=False,
                    safe_error_code="connector_paused",
                )
            else:
                result = self._run_leased(
                    leased_cursor,
                    leased_version,
                    limit,
                    now,
                )
        finally:
            self._state.release(
                self._scope,
                self._key,
                owner=self._worker_id,
                now=now,
            )
        return result

    def _run_leased(
        self,
        cursor: str | None,
        version: int,
        limit: int,
        now: datetime,
    ) -> PollRunResult:
        try:
            batch = self._poll(cursor, limit)
        except Exception:
            return self._record_failure(now, "poll_failed", paused=False)
        if batch.expected_cursor != cursor:
            return self._record_failure(now, "checkpoint_candidate_mismatch", paused=True)
        if batch.disposition is not PollDisposition.OK:
            return self._record_failure(
                now,
                batch.error_code or "poll_failed",
                paused=batch.disposition in {PollDisposition.PAUSE, PollDisposition.REJECTED},
            )
        accepted = 0
        batch_id: str | None = None
        if self._key.connector == "email" and batch.deliveries:
            try:
                registered = self._state.register_poll_batch(
                    self._scope,
                    self._key,
                    batch,
                    expected_version=version,
                    now=now,
                )
                batch_id = registered.batch_id
            except Exception:
                return self._record_failure(now, "poll_batch_register_failed", paused=False)
        try:
            for delivery in batch.deliveries:
                if batch_id is None:
                    self._state.accept_delivery(self._scope, self._key, delivery)
                else:
                    self._state.accept_delivery(
                        self._scope,
                        self._key,
                        delivery,
                        batch_id=batch_id,
                    )
                accepted += 1
        except Exception:
            return self._record_failure(now, "durable_accept_failed", paused=False)

        advanced = False
        if batch.deliveries and batch_id is not None:
            try:
                advanced = self._state.finalize_poll_batch(
                    self._scope,
                    self._key,
                    batch_id=batch_id,
                    expected_version=version,
                    now=now,
                )
            except Exception:
                return self._record_failure(now, "checkpoint_conflict", paused=False)
            if not advanced:
                self._state.update_health(
                    self._scope,
                    self._key,
                    status="degraded",
                    error_code="email_batch_pending",
                    now=now,
                )
                return PollRunResult(
                    status="retry",
                    accepted_count=accepted,
                    checkpoint_advanced=False,
                    safe_error_code="email_batch_pending",
                )
        elif batch.deliveries:
            try:
                self._state.advance_checkpoint(
                    self._scope,
                    self._key,
                    expected_version=version,
                    cursor=batch.candidate_cursor,
                    now=now,
                )
                advanced = True
            except Exception:
                return self._record_failure(now, "checkpoint_conflict", paused=False)
        self._state.update_health(
            self._scope,
            self._key,
            status="healthy",
            error_code=None,
            now=now,
        )
        return PollRunResult(
            status="ok",
            accepted_count=accepted,
            checkpoint_advanced=advanced,
            safe_error_code=None,
        )

    def _record_failure(
        self,
        now: datetime,
        error_code: str,
        *,
        paused: bool,
    ) -> PollRunResult:
        self._state.update_health(
            self._scope,
            self._key,
            status=("paused" if paused else "degraded"),
            error_code=error_code,
            now=now,
        )
        return PollRunResult(
            status=("paused" if paused else "retry"),
            accepted_count=0,
            checkpoint_advanced=False,
            safe_error_code=error_code,
        )

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now


def imap_poll_batch(
    *,
    expected_cursor: str | None,
    result: object,
) -> PollBatch:
    """Adapt EmailImapConnector output without importing credentials into the scheduler."""

    from .connectors.email_imap import ImapPollResult

    if not isinstance(result, ImapPollResult):
        raise TypeError("invalid IMAP poll result")
    disposition = {
        "ok": PollDisposition.OK,
        "retry": PollDisposition.RETRY,
        "paused": PollDisposition.PAUSE,
        "rejected": PollDisposition.REJECTED,
    }[result.status]
    return PollBatch(
        disposition=disposition,
        expected_cursor=expected_cursor,
        candidate_cursor=result.checkpoint_candidate,
        deliveries=tuple(message.raw_delivery for message in result.messages),
        error_code=result.error_code,
    )


def wecom_poll_batch(result: object) -> PollBatch:
    """Adapt WeCom archive fetch output; decryption remains a delivery-worker stage."""

    from .connectors.wecom_archive import EncryptedBatch

    if not isinstance(result, EncryptedBatch):
        raise TypeError("invalid WeCom poll result")
    return PollBatch(
        disposition=PollDisposition.OK,
        expected_cursor=result.expected_checkpoint,
        candidate_cursor=result.next_checkpoint,
        deliveries=result.deliveries,
    )
