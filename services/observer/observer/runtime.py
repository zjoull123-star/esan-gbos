from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .connectors.whatsapp_cloud import (
    DurableDeliveryConflict,
    DurableDeliveryExpired,
    DurableDeliveryReplay,
)
from .local_pilot_storage import DeliveryConflict, IngressExpired, NonceReplay

if TYPE_CHECKING:
    from .local_pilot_api import IdentityResolutionMetrics

_SAFE_REASON = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")


class KillSwitchEngaged(RuntimeError):
    """The local pilot is disabled or its operator kill switch is engaged."""


class LocalPilotRuntimeGuard:
    """Process-local control gate; persistent manifest state remains the authority."""

    __slots__ = ("_enabled", "_kill_switch", "_lock", "_reason")

    def __init__(self, *, enabled: bool, kill_switch: bool) -> None:
        self._enabled = bool(enabled)
        self._kill_switch = bool(kill_switch)
        self._reason: str | None = "configured_stop" if kill_switch else None
        self._lock = Lock()

    def require_running(self) -> None:
        with self._lock:
            if not self._enabled or self._kill_switch:
                raise KillSwitchEngaged(self._reason or "runtime_disabled")

    def engage(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or _SAFE_REASON.fullmatch(reason_code) is None:
            raise ValueError("invalid safe reason code")
        with self._lock:
            self._kill_switch = True
            self._reason = reason_code

    def health(self) -> dict[str, object]:
        with self._lock:
            running = self._enabled and not self._kill_switch
            return {
                "status": "ok" if running else "stopped",
                "runtime_enabled": self._enabled,
                "kill_switch": self._kill_switch,
                "safe_reason_code": self._reason,
                "external_send": False,
                "formal_business_commands": False,
            }


class AuthenticatedAccept(Protocol):
    def __call__(
        self,
        delivery: object,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str: ...


def map_whatsapp_durable_accept(
    accept: AuthenticatedAccept,
) -> AuthenticatedAccept:
    """Map durable storage conflicts into the receiver's stable public failures."""

    if not callable(accept):
        raise TypeError("accept must be callable")

    def mapped(
        delivery: object,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        try:
            return accept(
                delivery,
                nonce=nonce,
                nonce_expires_at=nonce_expires_at,
                now=now,
            )
        except DeliveryConflict:
            raise DurableDeliveryConflict from None
        except NonceReplay:
            raise DurableDeliveryReplay from None
        except IngressExpired:
            raise DurableDeliveryExpired from None

    return mapped


def guarded[**P, R](
    guard: LocalPilotRuntimeGuard,
    operation: Callable[P, R],
) -> Callable[P, R]:
    """Wrap a worker or ingress callable so the kill switch is checked per invocation."""

    if not callable(operation):
        raise TypeError("operation must be callable")

    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        guard.require_running()
        return operation(*args, **kwargs)

    return invoke


@dataclass(frozen=True, slots=True)
class PostgresLocalPilotRuntime:
    """Instantiated production seams; constructing it does not bind or start I/O."""

    guard: LocalPilotRuntimeGuard
    control_repository: Any
    communication_repository: Any
    control: Any
    reader: Any
    outbox: Any
    projection_repository: Any | None
    projection_publisher: Any | None
    identity_resolution_metrics: IdentityResolutionMetrics
    email_connector_config_repository: Any
    app: Any
    connection: Any
    storage: Any

    def polling_state(
        self,
        *,
        durable_accept: Callable[[Any, Any, Any], None],
    ) -> Any:
        from .scheduler import PostgresPollingState

        return PostgresPollingState(
            connection=self.connection,
            storage=self.storage,
            durable_accept=durable_accept,
        )


def compose_postgres_local_pilot_runtime(
    *,
    connection: Any,
    storage: Any,
    api_config: Any,
    cursor_secret: bytes,
    publisher: Callable[[Any, str, str], None],
    clock: Callable[[], datetime],
    outbox_worker_id: str,
    enabled: bool,
    kill_switch: bool,
    raw_loader: Callable[[Any, str], str | None] | None = None,
    model_provider: Any | None = None,
    model_raw_loader: Callable[[Any, str], str] | None = None,
    model_tokenizer: Callable[[Any, str, str], Any] | None = None,
    intelligence_publisher: Any | None = None,
    restricted_model_policy: Literal["deny", "local_tokenized"] = "deny",
    identity_resolution_metrics: IdentityResolutionMetrics | None = None,
    email_connector_configs: Any | None = None,
) -> PostgresLocalPilotRuntime:
    """Wire the PostgreSQL repositories, worker and authenticated internal app."""

    from .context_outbox import ContextOutboxPublisherWorker
    from .control_service import LocalPilotControlService, PostgresControlRepository
    from .email_connector_config import PostgresEmailConnectorConfigRepository
    from .identity_resolution_work import PostgresIdentityResolutionWorkRepository
    from .local_pilot_api import create_local_pilot_app
    from .model_projection import (
        ObservationProjectionPublisher,
        PostgresObservationProjectionRepository,
    )
    from .read_service import LocalPilotReadService, PostgresCommunicationRepository

    guard = LocalPilotRuntimeGuard(enabled=enabled, kill_switch=kill_switch)
    control_repository = PostgresControlRepository(
        connection=connection,
        replay_storage=storage,
    )
    communication_repository = PostgresCommunicationRepository(
        connection=connection,
        raw_loader=raw_loader,
    )
    control = LocalPilotControlService(repository=control_repository, clock=clock)
    reader = LocalPilotReadService(
        repository=communication_repository,
        cursor_secret=cursor_secret,
    )
    active_identity_resolution_metrics = (
        PostgresIdentityResolutionWorkRepository(connection)
        if identity_resolution_metrics is None
        else identity_resolution_metrics
    )
    active_email_connector_configs = (
        PostgresEmailConnectorConfigRepository(connection)
        if email_connector_configs is None
        else email_connector_configs
    )
    projection_repository = None
    projection_publisher = None
    effective_publisher: Callable[[Any, str, str], Any] = publisher
    projection_requested = any(
        value is not None
        for value in (
            model_provider,
            model_raw_loader,
            model_tokenizer,
            intelligence_publisher,
        )
    )
    if projection_requested:
        if model_raw_loader is None or intelligence_publisher is None:
            raise ValueError("model projection requires raw loader and Context publisher")
        projection_repository = PostgresObservationProjectionRepository(
            connection=connection,
            projection_store=communication_repository,
            raw_loader=model_raw_loader,
        )
        projection_publisher = ObservationProjectionPublisher(
            repository=projection_repository,
            raw_loader=projection_repository.raw_loader,
            tokenizer=model_tokenizer,
            provider=model_provider,
            context_publisher=intelligence_publisher,
            clock=clock,
            restricted_policy=restricted_model_policy,
        )
        effective_publisher = projection_publisher
    outbox = ContextOutboxPublisherWorker(
        storage=storage,
        publisher=effective_publisher,
        worker_id=outbox_worker_id,
        clock=clock,
    )
    app = create_local_pilot_app(
        config=api_config,
        control=control,
        reader=reader,
        guard=guard,
        clock=clock,
        identity_resolution_metrics=active_identity_resolution_metrics,
        email_connector_configs=active_email_connector_configs,
    )
    return PostgresLocalPilotRuntime(
        guard=guard,
        control_repository=control_repository,
        communication_repository=communication_repository,
        control=control,
        reader=reader,
        outbox=outbox,
        projection_repository=projection_repository,
        projection_publisher=projection_publisher,
        identity_resolution_metrics=active_identity_resolution_metrics,
        email_connector_config_repository=active_email_connector_configs,
        app=app,
        connection=connection,
        storage=storage,
    )
