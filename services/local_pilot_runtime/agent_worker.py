"""Default-off local Agent worker entrypoint."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
from services.agent_runtime.invocations import PostgresModelInvocationRepository
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
from services.model_gateway.runtime import (
    PostgresMonthlyUsageLedger,
    _read_exact_private_key_file,
    create_deepseek_agent_provider_factory,
)
from services.model_gateway.tokenization import EncryptedFileMappingVault

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
from .trusted_phrase_lexicon import (
    TrustedPhraseLexiconError,
    TrustedPhraseLexiconResolver,
    load_trusted_phrase_resolver,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_DEEPSEEK_API_KEY_FILE = Path("/run/secrets/deepseek_api_key")
DEFAULT_TOKENIZER_HMAC_KEY_FILE = Path("/run/secrets/tokenizer_hmac_key")
DEFAULT_MAPPING_VAULT_KEY_FILE = Path("/run/secrets/mapping_vault_key")
DEFAULT_TRUSTED_PHRASE_LEXICON_FILE = Path("/run/secrets/trusted_phrase_lexicon")
DEFAULT_TOKENIZER_VAULT_ROOT = Path("/var/lib/gbos/tokenizer-vault")
_CONTEXT_INTERNAL_BASE_URL = "http://context-api:8001"
_CONTEXT_INTERNAL_HOSTS = frozenset({"context-api"})
WorkerRunner = Callable[[AgentWorker, Event, float], None]
DeepSeekProviderFactory = Callable[[Mapping[str, Any], RuntimeConfig], ModelProvider]
TransportFactory = Callable[[], httpx.BaseTransport]
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True, repr=False)
class AgentSecretPaths:
    deepseek_api_key: Path
    tokenizer_hmac_key: Path
    mapping_vault_key: Path

    def __repr__(self) -> str:
        return "AgentSecretPaths(paths=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _DeepSeekPreflight:
    secret_paths: AgentSecretPaths
    vault: EncryptedFileMappingVault
    phrase_resolver: TrustedPhraseLexiconResolver

    def __repr__(self) -> str:
        return "_DeepSeekPreflight(secrets=<redacted>, vault=<redacted>, phrases=<redacted>)"


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
    secret_paths: AgentSecretPaths | None = None,
    trusted_phrase_lexicon_path: Path | None = None,
    tokenizer_vault_root: Path | None = None,
    transport_factory: TransportFactory | None = None,
    clock: Clock | None = None,
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
        configured = component_settings(config, "agent_worker")
        default_preflight: _DeepSeekPreflight | None = None
        if configured.provider_mode == "deepseek":
            _require_deepseek_network_enabled(environment)
            validate_deepseek_manifest(manifest)
            if deepseek_provider_factory is None:
                if transport_factory is not None and not callable(transport_factory):
                    raise RuntimeSupportError("DeepSeek transport factory is invalid")
                active_clock = clock or _utc_now
                active_paths = secret_paths or AgentSecretPaths(
                    deepseek_api_key=DEFAULT_DEEPSEEK_API_KEY_FILE,
                    tokenizer_hmac_key=DEFAULT_TOKENIZER_HMAC_KEY_FILE,
                    mapping_vault_key=DEFAULT_MAPPING_VAULT_KEY_FILE,
                )
                default_preflight = _preflight_default_deepseek(
                    config=config,
                    secret_paths=active_paths,
                    trusted_phrase_lexicon_path=(
                        trusted_phrase_lexicon_path or DEFAULT_TRUSTED_PHRASE_LEXICON_FILE
                    ),
                    tokenizer_vault_root=(tokenizer_vault_root or DEFAULT_TOKENIZER_VAULT_ROOT),
                    clock=active_clock,
                )
        context_bearer = load_secret_file(config.auth.context_client_bearer_file)
        load_secret_file(config.postgres.password_file)
        provider: ModelProvider | None = None
        if default_preflight is None:
            provider = compose_agent_provider(
                manifest,
                config,
                deepseek_provider_factory=deepseek_provider_factory,
            )
        connection = connect_postgres(config.postgres, connector=connector)
        if default_preflight is not None:
            provider = _compose_default_deepseek_provider(
                manifest=manifest,
                config=config,
                connection=cast(Connection, connection),
                preflight=default_preflight,
                transport_factory=transport_factory,
                clock=clock or _utc_now,
            )
        assert provider is not None
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
        TrustedPhraseLexiconError,
        ValueError,
        OSError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_deepseek_network_enabled(environment: Mapping[str, str]) -> None:
    if environment.get("GBOS_MODEL_KILL_SWITCH", "true") != "false":
        raise LocalEntrypointDisabled("Agent model path is disabled by kill switch")
    if environment.get("GBOS_DEEPSEEK_EGRESS_ENABLED") != "true":
        raise LocalEntrypointDisabled("DeepSeek controlled egress is disabled by default")


def _preflight_default_deepseek(
    *,
    config: RuntimeConfig,
    secret_paths: AgentSecretPaths,
    trusted_phrase_lexicon_path: Path,
    tokenizer_vault_root: Path,
    clock: Clock,
) -> _DeepSeekPreflight:
    """Validate every local boundary before PostgreSQL or HTTP construction."""

    load_secret_file(secret_paths.deepseek_api_key)
    _read_exact_private_key_file(secret_paths.tokenizer_hmac_key)
    _read_exact_private_key_file(secret_paths.mapping_vault_key)
    phrase_resolver = load_trusted_phrase_resolver(
        trusted_phrase_lexicon_path,
        expected_site_id=config.site_id,
        clock=clock,
    )
    root = _validated_vault_root(tokenizer_vault_root)
    vault = EncryptedFileMappingVault.from_key_file(
        root=root,
        key_file=secret_paths.mapping_vault_key,
        clock=clock,
    )
    return _DeepSeekPreflight(
        secret_paths=secret_paths,
        vault=vault,
        phrase_resolver=phrase_resolver,
    )


def _compose_default_deepseek_provider(
    *,
    manifest: Mapping[str, Any],
    config: RuntimeConfig,
    connection: Connection,
    preflight: _DeepSeekPreflight,
    transport_factory: TransportFactory | None,
    clock: Clock,
) -> ModelProvider:
    ledger = PostgresMonthlyUsageLedger(
        connection=connection,
        site_id=config.site_id,
        clock=clock,
    )
    audit = PostgresModelInvocationRepository(connection)
    factory = create_deepseek_agent_provider_factory(
        tokenizer_hmac_key_file=preflight.secret_paths.tokenizer_hmac_key,
        phrase_resolver=preflight.phrase_resolver.agent_phrases,
        audit_repository=audit,
        transport_factory=transport_factory,
        network_enabled=True,
        clock=clock,
    )
    provider = compose_local_provider(
        manifest,
        runtime_enabled=True,
        provider_mode="deepseek",
        model_kill_switch=False,
        key_file=preflight.secret_paths.deepseek_api_key,
        budget_ledger=ledger,
        tokenizer_vault=preflight.vault,
        controlled_egress=True,
        deepseek_factory=factory,
    )
    if not isinstance(provider, ModelProvider) or provider.tool_version != "no-tools-v1":
        raise RuntimeSupportError("Agent worker provider must enforce no-tools")
    return provider


def _validated_vault_root(path: Path) -> Path:
    candidate = Path(path)
    try:
        details = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeSupportError("tokenizer vault root is absent or unsafe") from exc
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or not candidate.is_dir()
        or resolved != candidate
        or details.st_mode & 0o002
    ):
        raise RuntimeSupportError("tokenizer vault root is absent or unsafe")
    return candidate


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
    "AgentSecretPaths",
    "PostgresTaskContextBindingResolver",
    "ScopedAgentExecutor",
    "build_worker",
    "compose_agent_provider",
    "main",
]
