"""Default-off local Agent worker entrypoint."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any, cast

import httpx

from services.action_guard.policy import ActionGuard
from services.agent_runtime.agents import (
    AgentExecutionResult,
    AgentInput,
    AgentOrchestrator,
    ModelProvider,
)
from services.agent_runtime.context_resolver import (
    ContextBinding,
    ContextEndpoint,
    HttpContextResolver,
)
from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.local_runtime import (
    LocalRuntimeError,
    compose_local_provider,
    validate_deepseek_manifest,
)
from services.agent_runtime.postgres import (
    Connection,
    PostgresAgentTaskRepository,
)
from services.agent_runtime.worker import (
    AgentWorker,
    ContextResolutionRequest,
    ThreadedHeartbeatRunner,
)

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
_CONTEXT_INTERNAL_BASE_URL = "http://context-api:8001"
_CONTEXT_INTERNAL_HOSTS = frozenset({"context-api"})
WorkerRunner = Callable[[AgentWorker, Event, float], None]
DeepSeekProviderFactory = Callable[[Mapping[str, Any], RuntimeConfig], ModelProvider]


class PostgresTaskContextBindingResolver:
    """Resolve purpose and exact decision ref from the claimed task payload."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresTaskContextBindingResolver(connection=<redacted>)"

    def __call__(self, request: ContextResolutionRequest) -> ContextBinding:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (request.site_id,),
            )
            cursor.execute(
                """
                SELECT processing_purpose, payload
                FROM agent_runtime.agent_tasks
                WHERE site_id = %s AND task_id = %s
                """,
                (request.site_id, request.task_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeSupportError("Agent task context binding is missing")
        payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        purpose = row[0]
        decision_ref = payload.get("decision_ref") if isinstance(payload, dict) else None
        if (
            not isinstance(purpose, str)
            or not purpose
            or not isinstance(decision_ref, str)
            or not decision_ref
        ):
            raise RuntimeSupportError("Agent task context binding is invalid")
        return ContextBinding(
            processing_purpose=purpose,
            decision_ref=decision_ref,
            request_id=request.task_id,
        )


class ScopedAgentExecutor:
    """Create a no-tools orchestrator scoped to the resolver-verified request refs."""

    __slots__ = ("_provider",)

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    def execute(
        self,
        request: AgentInput,
        *,
        now: datetime,
    ) -> AgentExecutionResult:
        return AgentOrchestrator(
            provider=self._provider,
            guard=ActionGuard(),
            known_evidence_refs=set(request.evidence_refs),
            known_fact_refs={
                (reference.fact_id, reference.fact_version)
                for reference in request.fact_version_refs
            },
            known_subject_refs={(request.subject_type, request.subject_ref)},
        ).execute(request, now=now)


def compose_agent_provider(
    manifest: Mapping[str, Any],
    config: RuntimeConfig,
    *,
    deepseek_provider_factory: DeepSeekProviderFactory | None = None,
) -> ModelProvider:
    component = component_settings(config, "agent_worker")
    if component.provider_mode == "deterministic":
        provider = compose_local_provider(
            manifest,
            runtime_enabled=True,
            provider_mode="deterministic",
            synthetic_e2e=component.synthetic_e2e,
        )
    elif component.provider_mode == "deepseek":
        if deepseek_provider_factory is None:
            raise RuntimeSupportError("DeepSeek provider composition is not injected")
        validate_deepseek_manifest(manifest)
        provider = deepseek_provider_factory(manifest, config)
    else:
        raise RuntimeSupportError("Agent worker provider mode is disabled")
    if not isinstance(provider, ModelProvider) or provider.tool_version != "no-tools-v1":
        raise RuntimeSupportError("Agent worker provider must enforce no-tools")
    return provider


def build_worker(
    *,
    connection: object,
    config: RuntimeConfig,
    provider: ModelProvider,
    context_bearer: SecretValue,
    context_transport: httpx.BaseTransport | None = None,
) -> AgentWorker:
    typed_connection = cast(Connection, connection)
    endpoint = _context_endpoint(config)
    resolver = HttpContextResolver(
        endpoint=endpoint,
        bearer_token=context_bearer.reveal(),
        auth_ref=config.auth.context_auth_ref,
        binding_resolver=PostgresTaskContextBindingResolver(typed_connection),
        transport=context_transport,
    )
    return AgentWorker(
        repository=PostgresAgentTaskRepository(typed_connection),
        site_id=config.site_id,
        worker_id=config.worker.worker_id,
        resolver=resolver,
        executor=ScopedAgentExecutor(provider),
        heartbeat_runner=ThreadedHeartbeatRunner(
            interval_seconds=config.worker.heartbeat_interval_seconds
        ),
    )


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    worker_runner: WorkerRunner | None = None,
    stop_event: Event | None = None,
    deepseek_provider_factory: DeepSeekProviderFactory | None = None,
    context_transport: httpx.BaseTransport | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="agent-worker",
            environ=environment,
        )
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        _context_endpoint(config)
        provider = compose_agent_provider(
            manifest,
            config,
            deepseek_provider_factory=deepseek_provider_factory,
        )
        context_bearer = load_secret_file(config.auth.context_client_bearer_file)
        connection = connect_postgres(config.postgres, connector=connector)
        worker = build_worker(
            connection=connection,
            config=config,
            provider=provider,
            context_bearer=context_bearer,
            context_transport=context_transport,
        )
        active_stop = stop_event or Event()
        active_runner = worker_runner or _run_worker
        active_runner(
            worker,
            active_stop,
            config.worker.idle_delay_seconds,
        )
        return 0
    except (
        LocalEntrypointDisabled,
        LocalRuntimeError,
        RuntimeSupportError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _context_endpoint(config: RuntimeConfig) -> ContextEndpoint:
    base_url = config.context_endpoint.base_url
    allowed_internal_hosts = (
        _CONTEXT_INTERNAL_HOSTS if base_url == _CONTEXT_INTERNAL_BASE_URL else frozenset()
    )
    return ContextEndpoint(
        base_url,
        unix_socket=config.context_endpoint.unix_socket,
        allowed_internal_hosts=allowed_internal_hosts,
    )


def _run_worker(
    worker: AgentWorker,
    stop_event: Event,
    idle_delay: float,
) -> None:
    worker.run(stop_event=stop_event, idle_delay=idle_delay)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PostgresTaskContextBindingResolver",
    "ScopedAgentExecutor",
    "build_worker",
    "compose_agent_provider",
    "main",
]
