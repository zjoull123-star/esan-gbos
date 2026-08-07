"""Credential-injected, durable pull-channel composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.observer.observer.connectors.email_imap import (
    EmailImapConfig,
    EmailImapConnector,
    TlsImapClientFactory,
)
from services.observer.observer.connectors.wecom_archive import (
    OfficialWeComArchiveSDK,
    WeComArchiveAdapter,
    WeComArchiveConfig,
)
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_storage import LocalPilotStorage
from services.observer.observer.models import (
    ConnectorKey,
    RawDelivery,
    TenantScope,
    stable_ulid,
)
from services.observer.observer.scheduler import (
    DurablePollingScheduler,
    PollBatch,
    PollDisposition,
    PollingState,
    PollRunResult,
    PostgresPollingState,
    imap_poll_batch,
    wecom_poll_batch,
)
from services.observer.observer.storage import Connection


@dataclass(frozen=True, slots=True, repr=False)
class EmailCredentials:
    """Ephemeral caller-owned authentication values which are never rendered."""

    username: str
    password: str

    def __post_init__(self) -> None:
        for value in (self.username, self.password):
            if not isinstance(value, str) or not value or len(value.encode()) > 4_096:
                raise ValueError("invalid injected email credentials")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(username=<redacted>, password=<redacted>)"


class DurableInbox(Protocol):
    def accept(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        correlation_id: str,
        max_attempts: int = 3,
    ) -> object: ...


class LocalPullRunner:
    """One inert polling composition; caller decides when to invoke it."""

    __slots__ = ("_dependency_label", "_limit", "_scheduler")

    def __init__(
        self,
        *,
        scheduler: DurablePollingScheduler,
        limit: int,
        dependency_label: str,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("poll limit must be between 1 and 1000")
        self._scheduler = scheduler
        self._limit = limit
        self._dependency_label = dependency_label

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(scheduler=<redacted>, "
            "credentials=<redacted>, "
            f"{self._dependency_label}=<redacted>, limit={self._limit})"
        )

    def run_once(self) -> PollRunResult:
        return self._scheduler.run_once(limit=self._limit)


class _FilteredBatchPollingScheduler(DurablePollingScheduler):
    """Commit a safe IMAP scan cursor even when activation filtering removes all items."""

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
        try:
            for delivery in batch.deliveries:
                self._state.accept_delivery(self._scope, self._key, delivery)
                accepted += 1
        except Exception:
            return self._record_failure(now, "durable_accept_failed", paused=False)

        advanced = batch.candidate_cursor != cursor
        if advanced:
            try:
                self._state.advance_checkpoint(
                    self._scope,
                    self._key,
                    expected_version=version,
                    cursor=batch.candidate_cursor,
                    now=now,
                )
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


def compose_email_poller(
    *,
    state: PollingState,
    scope: TenantScope,
    key: ConnectorKey,
    config: EmailImapConfig,
    tls_client_factory: TlsImapClientFactory,
    credentials: EmailCredentials,
    clock: Callable[[], datetime],
    worker_id: str,
    limit: int,
) -> LocalPullRunner:
    """Compose IMAP TLS polling without resolving or retaining an environment secret."""

    if key.connector != "email":
        raise ValueError("email poller requires an email connector key")
    if not isinstance(credentials, EmailCredentials):
        raise TypeError("email credentials must be explicitly injected")
    if limit > config.poll_limit:
        raise ValueError("poll limit exceeds email connector configuration")
    connector = EmailImapConnector(
        connector_instance_id=key.instance_id,
        config=config,
        tls_client_factory=tls_client_factory,
        clock=clock,
    )

    def poll(cursor: str | None, requested_limit: int) -> PollBatch:
        result = connector.poll(
            cursor,
            username=credentials.username,
            password=credentials.password,
            limit=requested_limit,
        )
        return imap_poll_batch(
            expected_cursor=cursor,
            result=result,
        )

    scheduler = _FilteredBatchPollingScheduler(
        state=state,
        poll=poll,
        scope=scope,
        key=key,
        clock=clock,
        worker_id=worker_id,
    )
    return LocalPullRunner(
        scheduler=scheduler,
        limit=limit,
        dependency_label="tls_factory",
    )


def compose_wecom_poller(
    *,
    state: PollingState,
    scope: TenantScope,
    key: ConnectorKey,
    config: WeComArchiveConfig,
    sdk: OfficialWeComArchiveSDK,
    activation_time: datetime,
    clock: Callable[[], datetime],
    worker_id: str,
    limit: int,
) -> LocalPullRunner:
    """Compose an injected official-SDK adapter without loading or downloading one."""

    if key.connector != "wecom":
        raise ValueError("WeCom poller requires a wecom connector key")
    if key.instance_id != config.instance_id:
        raise ValueError("WeCom connector instance does not match configuration")
    if limit > config.max_batch_size:
        raise ValueError("poll limit exceeds WeCom connector configuration")
    if (
        not isinstance(activation_time, datetime)
        or activation_time.tzinfo is None
        or activation_time.utcoffset() is None
    ):
        raise ValueError("WeCom activation_time must be timezone-aware")
    try:
        sdk_methods = (
            sdk.fetch_chat_data,
            sdk.decrypt_random_key,
            sdk.decrypt_chat_data,
            sdk.download_media,
        )
    except AttributeError:
        raise TypeError("an injected official SDK boundary is required") from None
    if not all(callable(method) for method in sdk_methods):
        raise TypeError("an injected official SDK boundary is required")
    connector = WeComArchiveAdapter(
        config=config,
        sdk=sdk,
        clock=clock,
    )

    def poll(cursor: str | None, requested_limit: int) -> PollBatch:
        polled_at = clock()
        if (
            not isinstance(polled_at, datetime)
            or polled_at.tzinfo is None
            or polled_at.utcoffset() is None
        ):
            return PollBatch(
                disposition=PollDisposition.PAUSE,
                expected_cursor=cursor,
                candidate_cursor=cursor,
                deliveries=(),
                error_code="invalid_clock",
            )
        if polled_at < activation_time:
            return PollBatch(
                disposition=PollDisposition.PAUSE,
                expected_cursor=cursor,
                candidate_cursor=cursor,
                deliveries=(),
                error_code="activation_not_reached",
            )
        if cursor is None:
            return PollBatch(
                disposition=PollDisposition.PAUSE,
                expected_cursor=None,
                candidate_cursor=None,
                deliveries=(),
                error_code="activation_checkpoint_required",
            )
        return wecom_poll_batch(connector.fetch(cursor, requested_limit))

    scheduler = DurablePollingScheduler(
        state=state,
        poll=poll,
        scope=scope,
        key=key,
        clock=clock,
        worker_id=worker_id,
    )
    return LocalPullRunner(
        scheduler=scheduler,
        limit=limit,
        dependency_label="sdk",
    )


def compose_postgres_polling_state(
    *,
    connection: Connection,
    storage: LocalPilotStorage,
    inbox: DurableDeliveryInbox | DurableInbox,
) -> PostgresPollingState:
    """Bind durable inbox acceptance to the existing PostgreSQL checkpoint state."""

    def durable_accept(
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
    ) -> None:
        inbox.accept(
            scope,
            key,
            delivery,
            correlation_id=stable_ulid(
                "pull-delivery",
                scope.site_id,
                key.connector,
                key.instance_id,
                delivery.delivery_id,
            ),
        )

    return PostgresPollingState(
        connection=connection,
        storage=storage,
        durable_accept=durable_accept,
    )


def main() -> int:
    """Refuse standalone polling without injected credentials, SDK, and storage."""

    return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EmailCredentials",
    "LocalPullRunner",
    "compose_email_poller",
    "compose_postgres_polling_state",
    "compose_wecom_poller",
    "main",
]
