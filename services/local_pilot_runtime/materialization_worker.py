"""Default-off local Frappe materialization worker entrypoint."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Protocol, cast

from services.agent_runtime.frappe_client import (
    FrappeClientError,
    FrappeJsonTransport,
    HttpFrappeDraftClient,
)
from services.agent_runtime.frappe_context import HttpMaterializationContextResolver
from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.materialization import (
    MaterializationRunResult,
    MaterializationWorker,
)
from services.agent_runtime.postgres import Connection, PostgresAgentTaskRepository
from services.agent_runtime.proposals import TrustedMaterializer

from .runtime_support import (
    RuntimeConfig,
    RuntimeSupportError,
    SecretValue,
    close_connection,
    component_settings,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_FRAPPE_API_KEY_FILE = Path("/run/secrets/frappe_materializer_api_key")
DEFAULT_FRAPPE_API_SECRET_FILE = Path("/run/secrets/frappe_materializer_api_secret")
_FRAPPE_INTERNAL_BASE_URL = "http://frappe-backend:8000"
_FRAPPE_INTERNAL_HOSTS = frozenset({"frappe-backend"})
_LEASE_INTERVAL_MULTIPLIER = 10

Sleep = Callable[[float], None]
Clock = Callable[[], datetime]
WorkerRunner = Callable[[MaterializationWorker, str, Event, float, Sleep], None]


class MaterializationRunWorker(Protocol):
    def run_once(self, site_id: str) -> MaterializationRunResult: ...


def build_worker(
    *,
    connection: object,
    config: RuntimeConfig,
    frappe_api_key: SecretValue,
    frappe_api_secret: SecretValue,
    clock: Clock,
    frappe_timeout_seconds: float,
    frappe_transport: FrappeJsonTransport | None = None,
) -> MaterializationWorker:
    lease_duration = timedelta(
        seconds=config.worker.heartbeat_interval_seconds * _LEASE_INTERVAL_MULTIPLIER
    )
    if not 0 < frappe_timeout_seconds < lease_duration.total_seconds():
        raise RuntimeSupportError("Frappe timeout must be strictly below the worker lease")
    base_url = _frappe_base_url(config)
    allowed_internal_hosts = _frappe_allowed_internal_hosts(base_url)
    client = HttpFrappeDraftClient(
        base_url=base_url,
        api_key=frappe_api_key.reveal(),
        api_secret=frappe_api_secret.reveal(),
        auth_ref=config.auth.context_auth_ref,
        site_id=config.site_id,
        timeout_seconds=frappe_timeout_seconds,
        transport=frappe_transport,
        allowed_internal_hosts=allowed_internal_hosts,
    )
    context_resolver = HttpMaterializationContextResolver(
        base_url=base_url,
        api_key=frappe_api_key.reveal(),
        api_secret=frappe_api_secret.reveal(),
        auth_ref=config.auth.context_auth_ref,
        site_id=config.site_id,
        timeout_seconds=frappe_timeout_seconds,
        transport=frappe_transport,
        allowed_internal_hosts=allowed_internal_hosts,
    )
    return MaterializationWorker(
        repository=PostgresAgentTaskRepository(cast(Connection, connection)),
        client=client,
        materializer=TrustedMaterializer(),
        context_resolver=context_resolver,
        worker_id=config.worker.worker_id,
        clock=clock,
        lease_duration=lease_duration,
        retry_delay=timedelta(seconds=30),
    )


def run_worker(
    worker: MaterializationRunWorker,
    *,
    site_id: str,
    stop_event: Event,
    idle_delay: float,
    sleep: Sleep = time.sleep,
) -> None:
    if idle_delay <= 0:
        raise ValueError("idle_delay must be positive")
    while not stop_event.is_set():
        outcome = worker.run_once(site_id)
        if outcome.status == "idle":
            sleep(idle_delay)


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    frappe_api_key_path: Path = DEFAULT_FRAPPE_API_KEY_FILE,
    frappe_api_secret_path: Path = DEFAULT_FRAPPE_API_SECRET_FILE,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    worker_runner: WorkerRunner | None = None,
    stop_event: Event | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock | None = None,
    frappe_timeout_seconds: float = 3.0,
    frappe_transport: FrappeJsonTransport | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="materialization-worker",
            environ=environment,
        )
        if environment.get("GBOS_MATERIALIZATION_KILL_SWITCH", "true") != "false":
            raise LocalEntrypointDisabled("materialization-worker is disabled by kill switch")
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        component_settings(config, "agent_worker")
        lease_seconds = config.worker.heartbeat_interval_seconds * _LEASE_INTERVAL_MULTIPLIER
        if not 0 < frappe_timeout_seconds < lease_seconds:
            raise RuntimeSupportError("Frappe timeout must be strictly below the worker lease")
        frappe_api_key = load_secret_file(frappe_api_key_path)
        frappe_api_secret = load_secret_file(frappe_api_secret_path)
        active_clock = clock or (lambda: datetime.now(UTC))
        base_url = _frappe_base_url(config)
        allowed_internal_hosts = _frappe_allowed_internal_hosts(base_url)
        HttpFrappeDraftClient(
            base_url=base_url,
            api_key=frappe_api_key.reveal(),
            api_secret=frappe_api_secret.reveal(),
            auth_ref=config.auth.context_auth_ref,
            site_id=config.site_id,
            timeout_seconds=frappe_timeout_seconds,
            transport=frappe_transport,
            allowed_internal_hosts=allowed_internal_hosts,
        )
        HttpMaterializationContextResolver(
            base_url=base_url,
            api_key=frappe_api_key.reveal(),
            api_secret=frappe_api_secret.reveal(),
            auth_ref=config.auth.context_auth_ref,
            site_id=config.site_id,
            timeout_seconds=frappe_timeout_seconds,
            transport=frappe_transport,
            allowed_internal_hosts=allowed_internal_hosts,
        )
        connection = connect_postgres(config.postgres, connector=connector)
        worker = build_worker(
            connection=connection,
            config=config,
            frappe_api_key=frappe_api_key,
            frappe_api_secret=frappe_api_secret,
            clock=active_clock,
            frappe_timeout_seconds=frappe_timeout_seconds,
            frappe_transport=frappe_transport,
        )
        active_runner = worker_runner or _run_worker
        active_runner(
            worker,
            config.site_id,
            stop_event or Event(),
            config.worker.idle_delay_seconds,
            sleep,
        )
        return 0
    except (
        FrappeClientError,
        LocalEntrypointDisabled,
        RuntimeSupportError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _frappe_base_url(config: RuntimeConfig) -> str:
    socket_path = config.context_endpoint.unix_socket
    if socket_path is not None:
        return f"unix://{socket_path}"
    return config.context_endpoint.base_url


def _frappe_allowed_internal_hosts(base_url: str) -> frozenset[str]:
    if base_url == _FRAPPE_INTERNAL_BASE_URL:
        return _FRAPPE_INTERNAL_HOSTS
    return frozenset()


def _run_worker(
    worker: MaterializationWorker,
    site_id: str,
    stop_event: Event,
    idle_delay: float,
    sleep: Sleep,
) -> None:
    run_worker(
        worker,
        site_id=site_id,
        stop_event=stop_event,
        idle_delay=idle_delay,
        sleep=sleep,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_FRAPPE_API_KEY_FILE",
    "DEFAULT_FRAPPE_API_SECRET_FILE",
    "build_worker",
    "main",
    "run_worker",
]
