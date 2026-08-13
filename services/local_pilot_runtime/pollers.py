"""Credential-injected, durable pull-channel composition."""

from __future__ import annotations

import imaplib
import os
import ssl
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.observer.observer.connectors.email_imap import (
    EmailImapConfig,
    EmailImapConnector,
    ImapCheckpoint,
    TlsImapClientFactory,
)
from services.observer.observer.connectors.wecom_archive import (
    OfficialWeComArchiveSDK,
    WeComArchiveAdapter,
    WeComArchiveConfig,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_storage import (
    LocalPilotStorage,
    PostgresLocalPilotStorage,
)
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

from .channel_config import (
    ChannelConfigError,
    EmailCredentialConfig,
    WeComCredentialConfig,
    load_channel_config,
    load_channel_credential,
    require_active_channel,
)
from .runtime_support import (
    RuntimeSupportError,
    close_connection,
    connect_postgres,
    load_runtime_config,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_CONNECTORS_CONFIG = Path("/config/connectors.json")
DEFAULT_IMAP_CONNECT_TIMEOUT_SECONDS = 10.0
StorageFactory = Callable[[object], LocalPilotStorage]
WeComSdkFactory = Callable[[WeComCredentialConfig], OfficialWeComArchiveSDK]


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
        lease_generation: int,
    ) -> PollRunResult:
        now = self._now()
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
                self._state.accept_delivery(
                    self._scope,
                    self._key,
                    delivery,
                    owner=self._worker_id,
                    lease_generation=lease_generation,
                    now=self._now(),
                )
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
                    owner=self._worker_id,
                    lease_generation=lease_generation,
                    now=self._now(),
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


class StopEvent(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


def run_poll_daemon(
    runner: LocalPullRunner,
    *,
    stop_event: StopEvent,
    interval_seconds: float = 60,
) -> None:
    """Run one durable poll per interval until the injected event is signalled."""

    if (
        not callable(getattr(runner, "run_once", None))
        or not callable(getattr(stop_event, "is_set", None))
        or not callable(getattr(stop_event, "wait", None))
        or isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int | float)
        or not 0 < interval_seconds <= 3_600
    ):
        raise ValueError("invalid polling daemon composition")
    while not stop_event.is_set():
        runner.run_once()
        stop_event.wait(float(interval_seconds))


def main(
    argv: list[str] | None = None,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    connectors_path: Path = DEFAULT_CONNECTORS_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    storage_factory: StorageFactory | None = None,
    tls_client_factory: TlsImapClientFactory | None = None,
    wecom_sdk_factory: WeComSdkFactory | None = None,
    daemon_runner: Callable[..., None] | None = None,
    stop_event: StopEvent | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Preflight and run exactly one durable receive-only pull channel."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in {"email", "wecom"}:
        return 78
    channel_name = arguments[0]
    environment = os.environ if environ is None else environ
    active_clock = clock or _utc_now
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        if (
            environment.get("GBOS_CONNECTOR_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
        ):
            raise ChannelConfigError("pull channel remains kill-switched")
        manifest = load_local_manifest(manifest_path)
        if channel_name == "email" and "email_gateway" in manifest:
            raise ChannelConfigError("legacy email poller is replaced by Gateway mailboxes")
        require_component_enabled(
            manifest,
            component=channel_name,
            environ=environment,
        )
        runtime = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, runtime)
        channels = load_channel_config(
            connectors_path,
            expected_site_id=runtime.site_id,
            manifest=manifest,
        )
        channel = require_active_channel(
            channels,
            channel_name,
            now=active_clock(),
        )
        credential = load_channel_credential(channels, channel_name)
        activation_time = channel.activation_time
        if activation_time is None:
            raise ChannelConfigError("active pull channel has no activation time")
        if channel_name == "email":
            if not isinstance(credential, EmailCredentialConfig):
                raise ChannelConfigError("email credential type is invalid")
            if credential.initial_checkpoint is not None:
                ImapCheckpoint.parse(credential.initial_checkpoint)
        else:
            if not isinstance(credential, WeComCredentialConfig):
                raise ChannelConfigError("WeCom credential type is invalid")
            if (
                credential.initial_checkpoint is None
                or not credential.initial_checkpoint.isascii()
                or not credential.initial_checkpoint.isdecimal()
            ):
                raise ChannelConfigError("WeCom initial checkpoint is required")
            if wecom_sdk_factory is None:
                raise ChannelConfigError("official WeCom SDK factory is unavailable")

        connection = connect_postgres(runtime.postgres, connector=connector)
        storage = (
            PostgresLocalPilotStorage(cast(Connection, connection))
            if storage_factory is None
            else storage_factory(connection)
        )
        scope = TenantScope(runtime.site_id, "observation_processing")
        key = ConnectorKey(channel_name, credential.instance_id)
        storage.register_connector_instance(
            scope,
            key,
            now=active_clock(),
            team_ref=credential.team_ref,
            agent_task_type=credential.agent_task_type,
            account_user_ref=credential.account_user_ref,
        )
        inbox = DurableDeliveryInbox(
            storage=storage,
            evidence_store=ContentAddressedEvidenceStore(channels.evidence_cas_root),
        )
        state = compose_postgres_polling_state(
            connection=cast(Connection, connection),
            storage=storage,
            inbox=inbox,
        )
        _ensure_initial_checkpoint(
            state=state,
            scope=scope,
            key=key,
            initial_checkpoint=credential.initial_checkpoint,
            now=active_clock(),
        )
        if isinstance(credential, EmailCredentialConfig):
            factory = tls_client_factory or _stdlib_imap_factory
            puller = compose_email_poller(
                state=state,
                scope=scope,
                key=key,
                config=EmailImapConfig(
                    host=credential.host,
                    port=credential.port,
                    mailbox=credential.mailbox,
                    folder=credential.folder,
                    enabled_at=activation_time,
                    poll_limit=credential.poll_limit,
                    max_message_bytes=credential.max_message_bytes,
                    max_attachment_bytes=credential.max_attachment_bytes,
                    max_attachments=credential.max_attachments,
                    rescan_max_window=timedelta(seconds=credential.rescan_max_window_seconds),
                    rescan_max_uids=credential.rescan_max_uids,
                ),
                tls_client_factory=factory,
                credentials=EmailCredentials(
                    username=credential.username,
                    password=credential.password,
                ),
                clock=active_clock,
                worker_id=runtime.worker.worker_id,
                limit=credential.poll_limit,
            )
        else:
            assert wecom_sdk_factory is not None
            sdk = wecom_sdk_factory(credential)
            puller = compose_wecom_poller(
                state=state,
                scope=scope,
                key=key,
                config=WeComArchiveConfig(instance_id=credential.instance_id),
                sdk=sdk,
                activation_time=activation_time,
                clock=active_clock,
                worker_id=runtime.worker.worker_id,
                limit=100,
            )
        active_stop = stop_event or Event()
        active_runner = daemon_runner or run_poll_daemon
        active_runner(puller, stop_event=active_stop)
        return 0
    except (
        ChannelConfigError,
        LocalEntrypointDisabled,
        RuntimeSupportError,
        OSError,
        TypeError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _ensure_initial_checkpoint(
    *,
    state: PostgresPollingState,
    scope: TenantScope,
    key: ConnectorKey,
    initial_checkpoint: str | None,
    now: datetime,
) -> None:
    cursor, version, _status = state.load_checkpoint(scope, key)
    if cursor is None and initial_checkpoint is not None:
        owner = "checkpoint-initializer"
        lease_generation = state.acquire(
            scope,
            key,
            owner=owner,
            now=now,
            lease_seconds=60,
        )
        try:
            state.advance_checkpoint(
                scope,
                key,
                expected_version=version,
                cursor=initial_checkpoint,
                owner=owner,
                lease_generation=lease_generation,
                now=now,
            )
        finally:
            state.release(
                scope,
                key,
                owner=owner,
                lease_generation=lease_generation,
                now=now,
            )
    elif initial_checkpoint is not None and cursor != initial_checkpoint:
        raise ChannelConfigError("persisted checkpoint conflicts with configured initial value")


def _stdlib_imap_factory(host: str, port: int) -> Any:
    ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ssl_context.verify_mode = ssl.CERT_REQUIRED
    ssl_context.check_hostname = True
    if ssl_context.minimum_version < ssl.TLSVersion.TLSv1_2:
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    return imaplib.IMAP4_SSL(
        host,
        port,
        ssl_context=ssl_context,
        timeout=DEFAULT_IMAP_CONNECT_TIMEOUT_SECONDS,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EmailCredentials",
    "LocalPullRunner",
    "compose_email_poller",
    "compose_postgres_polling_state",
    "compose_wecom_poller",
    "main",
    "run_poll_daemon",
]
