"""Default-off fenced worker for Observer communication model projection."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, Protocol, TypeVar

import httpx

from services.agent_runtime.invocations import PostgresModelInvocationRepository
from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.local_runtime import DeepSeekAssembly, validate_deepseek_manifest
from services.context.context_service.communication_intelligence import (
    PostgresCommunicationIntelligenceRepository,
)
from services.model_gateway.observation_provider import DeepSeekObservationProvider
from services.model_gateway.runtime import (
    PostgresMonthlyUsageLedger,
    create_deepseek_observation_provider,
)
from services.model_gateway.tokenization import EncryptedFileMappingVault, StableTokenizer
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.model_projection import (
    ContentAddressedEvidenceTextLoader,
    ContextIntelligencePublisher,
    LocalTokenizationResult,
    ObservationProjectionPublisher,
    ObservationProjectionRepository,
    PostgresObservationProjectionRepository,
    RawObservationLoader,
)
from services.observer.observer.models import TenantScope, _require_aware
from services.observer.observer.projection_outbox import (
    PostgresProjectionOutboxRepository,
    ProjectionLeaseConflict,
)
from services.observer.observer.read_service import PostgresCommunicationRepository

from .projection_config import load_projection_config
from .runtime_support import (
    RuntimeConfig,
    RuntimeSupportError,
    component_settings,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_PROJECTION_CONFIG = Path("/config/projection-connections.json")
DEFAULT_DEEPSEEK_API_KEY_FILE = Path("/run/secrets/deepseek_api_key")
DEFAULT_TOKENIZER_HMAC_KEY_FILE = Path("/run/secrets/tokenizer_hmac_key")
DEFAULT_MAPPING_VAULT_KEY_FILE = Path("/run/secrets/mapping_vault_key")
MODEL_PROJECTION_PROCESSING_PURPOSE = "observation_processing"
_LEASE_INTERVAL_MULTIPLIER = 10
_RETRY_DELAY = timedelta(seconds=30)
_MAX_PHRASES_PER_KIND = 1_000

T = TypeVar("T")
Clock = Callable[[], datetime]
ProjectionPublisher = Callable[[TenantScope, str, str], object]
TrustedPhraseResolver = Callable[
    [TenantScope, str, str],
    "TrustedPhraseResolution",
]


class ProjectionRunStatus(StrEnum):
    IDLE = "idle"
    PUBLISHED = "published"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionOutboxClaim:
    site_id: str
    outbox_id: str
    observation_id: str
    idempotency_key: str
    status: Literal["leased"]
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: datetime
    fence_token: str

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.site_id, 140),
            (self.outbox_id, 256),
            (self.observation_id, 256),
            (self.idempotency_key, 256),
            (self.lease_owner, 256),
            (self.fence_token, 256),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > maximum
                or any(character in value for character in ("\x00", "\r", "\n"))
            ):
                raise ValueError("invalid projection outbox claim")
        if (
            self.status != "leased"
            or not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or not 1 <= self.attempt <= self.max_attempts <= 10
        ):
            raise ValueError("invalid projection outbox claim")
        _require_aware(self.lease_expires_at, "lease_expires_at")

    def __repr__(self) -> str:
        return (
            "ProjectionOutboxClaim("
            f"status={self.status!r}, attempt={self.attempt}, "
            "identity=<redacted>, fence=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionRunResult:
    status: ProjectionRunStatus
    attempt: int | None = None

    def __repr__(self) -> str:
        return f"ProjectionRunResult(status={self.status!r}, attempt={self.attempt!r})"


class ProjectionOutboxRepository(Protocol):
    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProjectionOutboxClaim | None: ...

    def heartbeat(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> None: ...

    def mark_published(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]: ...


class HeartbeatRunner(Protocol):
    def run(
        self,
        execute: Callable[[], T],
        heartbeat: Callable[[], object],
    ) -> T: ...


class ThreadedProjectionHeartbeatRunner:
    """Renew a fenced lease while a blocking model projection is in progress."""

    def __init__(self, *, interval_seconds: float) -> None:
        if not 0 < interval_seconds <= 60:
            raise ValueError("heartbeat interval must be positive and bounded")
        self._interval_seconds = interval_seconds

    def run(
        self,
        execute: Callable[[], T],
        heartbeat: Callable[[], object],
    ) -> T:
        stop = Event()
        failure: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self._interval_seconds):
                try:
                    heartbeat()
                except BaseException as exc:
                    failure.append(exc)
                    stop.set()
                    return

        thread = Thread(
            target=renew,
            name="model-projection-lease-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            result = execute()
        finally:
            stop.set()
            thread.join()
        if failure:
            raise failure[0]
        return result


class ModelProjectionWorker:
    """Process one communication projection under an attempt-bound fence."""

    __slots__ = (
        "_clock",
        "_heartbeat_runner",
        "_lease_duration",
        "_outbox",
        "_publisher",
        "_retry_delay",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        outbox: ProjectionOutboxRepository,
        publisher: ProjectionPublisher,
        worker_id: str,
        clock: Clock,
        lease_duration: timedelta,
        retry_delay: timedelta = _RETRY_DELAY,
        heartbeat_runner: HeartbeatRunner | None = None,
    ) -> None:
        if (
            not worker_id
            or worker_id != worker_id.strip()
            or len(worker_id) > 256
            or not callable(publisher)
            or not callable(clock)
            or lease_duration <= timedelta(0)
            or retry_delay <= timedelta(0)
        ):
            raise ValueError("invalid model projection worker configuration")
        self._outbox = outbox
        self._publisher = publisher
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._heartbeat_runner = heartbeat_runner or ThreadedProjectionHeartbeatRunner(
            interval_seconds=max(0.1, lease_duration.total_seconds() / 3)
        )

    def __repr__(self) -> str:
        return (
            "ModelProjectionWorker(outbox=<redacted>, publisher=<redacted>, worker_id=<redacted>)"
        )

    def run_once(self, scope: TenantScope) -> ProjectionRunResult:
        claim = self._outbox.claim(
            scope,
            worker_id=self._worker_id,
            now=self._now(),
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return ProjectionRunResult(status=ProjectionRunStatus.IDLE)
        try:
            self._validate_claim(scope, claim)
            self._heartbeat_runner.run(
                lambda: self._publisher(
                    scope,
                    claim.observation_id,
                    claim.idempotency_key,
                ),
                lambda: self._outbox.heartbeat(
                    scope,
                    claim.outbox_id,
                    worker_id=self._worker_id,
                    expected_attempt=claim.attempt,
                    fence_token=claim.fence_token,
                    now=self._now(),
                    lease_duration=self._lease_duration,
                ),
            )
        except ProjectionLeaseConflict:
            return ProjectionRunResult(
                status=ProjectionRunStatus.LEASE_LOST,
                attempt=claim.attempt,
            )
        except Exception:
            return self._mark_failed(scope, claim)

        try:
            self._outbox.mark_published(
                scope,
                claim.outbox_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                fence_token=claim.fence_token,
                now=self._now(),
            )
        except ProjectionLeaseConflict:
            return ProjectionRunResult(
                status=ProjectionRunStatus.LEASE_LOST,
                attempt=claim.attempt,
            )
        return ProjectionRunResult(
            status=ProjectionRunStatus.PUBLISHED,
            attempt=claim.attempt,
        )

    def _mark_failed(
        self,
        scope: TenantScope,
        claim: ProjectionOutboxClaim,
    ) -> ProjectionRunResult:
        now = self._now()
        try:
            status = self._outbox.mark_failed(
                scope,
                claim.outbox_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                fence_token=claim.fence_token,
                now=now,
                retry_at=now + self._retry_delay,
                error_code="projection_failed",
            )
        except ProjectionLeaseConflict:
            return ProjectionRunResult(
                status=ProjectionRunStatus.LEASE_LOST,
                attempt=claim.attempt,
            )
        if status == "retry":
            run_status = ProjectionRunStatus.RETRY
        elif status == "dead_letter":
            run_status = ProjectionRunStatus.DEAD_LETTER
        else:
            raise ValueError("projection outbox returned an invalid failure status")
        return ProjectionRunResult(status=run_status, attempt=claim.attempt)

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "clock")
        return now.astimezone(UTC)

    def _validate_claim(
        self,
        scope: TenantScope,
        claim: ProjectionOutboxClaim,
    ) -> None:
        if (
            claim.site_id != scope.site_id
            or claim.status != "leased"
            or claim.lease_owner != self._worker_id
            or claim.lease_expires_at <= self._now()
        ):
            raise ProjectionLeaseConflict("projection claim is outside its live fence")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedPhraseResolution:
    names: tuple[str, ...]
    organizations: tuple[str, ...]
    names_complete: Literal[True]
    organizations_complete: Literal[True]
    resolver_version: str

    def __post_init__(self) -> None:
        if self.names_complete is not True or self.organizations_complete is not True:
            raise ValueError("trusted phrase coverage must be complete")
        if (
            not isinstance(self.names, tuple)
            or not isinstance(self.organizations, tuple)
            or len(self.names) > _MAX_PHRASES_PER_KIND
            or len(self.organizations) > _MAX_PHRASES_PER_KIND
            or len(self.names) != len(set(self.names))
            or len(self.organizations) != len(set(self.organizations))
            or not self.resolver_version
            or self.resolver_version != self.resolver_version.strip()
            or len(self.resolver_version) > 80
        ):
            raise ValueError("trusted phrase resolution is invalid")
        if any(not _valid_phrase(value) for value in self.names + self.organizations):
            raise ValueError("trusted phrase resolution is invalid")

    def __repr__(self) -> str:
        return (
            "TrustedPhraseResolution("
            "names=<redacted>, organizations=<redacted>, "
            f"resolver_version={self.resolver_version!r})"
        )


class TrustedProjectionTokenizer:
    """Tokenize built-in email/phone plus resolver-proven names/organizations."""

    __slots__ = ("_clock", "_phrase_resolver", "_tokenizer")

    def __init__(
        self,
        *,
        tokenizer: StableTokenizer,
        phrase_resolver: TrustedPhraseResolver,
        clock: Clock,
    ) -> None:
        if not isinstance(tokenizer, StableTokenizer):
            raise TypeError("projection tokenizer must be the stable local tokenizer")
        if not callable(phrase_resolver) or not callable(clock):
            raise TypeError("trusted phrase resolver and clock are required")
        self._tokenizer = tokenizer
        self._phrase_resolver = phrase_resolver
        self._clock = clock

    def __repr__(self) -> str:
        return "TrustedProjectionTokenizer(tokenizer=<redacted>, phrase_resolver=<redacted>)"

    def __call__(
        self,
        scope: TenantScope,
        observation_id: str,
        raw_text: str,
    ) -> LocalTokenizationResult:
        resolution = self._phrase_resolver(scope, observation_id, raw_text)
        if not isinstance(resolution, TrustedPhraseResolution):
            raise ValueError("trusted phrase resolution proof is missing")
        result = self._tokenizer.tokenize(
            raw_text,
            site_id=scope.site_id,
            purpose=scope.processing_purpose,
            phrases=resolution.names + resolution.organizations,
            now=self._clock(),
        )
        return LocalTokenizationResult(
            text=result.text,
            receipt_ref=result.receipt.receipt_id,
            tokenizer_version=result.receipt.tokenizer_version,
            mapping_digest=result.receipt.mapping_digest,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelProjectionComponents:
    """Injected least-privilege pieces; no broad connection is accepted here."""

    outbox: ProjectionOutboxRepository
    projection_repository: ObservationProjectionRepository
    raw_loader: RawObservationLoader
    context_publisher: ContextIntelligencePublisher
    tokenizer: StableTokenizer
    provider: DeepSeekObservationProvider
    close: Callable[[], None]

    def __repr__(self) -> str:
        return (
            "ModelProjectionComponents("
            "outbox=<redacted>, projection_repository=<redacted>, "
            "raw_loader=<redacted>, context_publisher=<redacted>, "
            "tokenizer=<redacted>, provider=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionSecretPaths:
    deepseek_api_key: Path
    tokenizer_hmac_key: Path
    mapping_vault_key: Path

    def __repr__(self) -> str:
        return "ProjectionSecretPaths(paths=<redacted>)"


ComponentsFactory = Callable[
    [Mapping[str, Any], RuntimeConfig, ProjectionSecretPaths],
    ModelProjectionComponents,
]
WorkerRunner = Callable[[ModelProjectionWorker, TenantScope, Event, float], None]


class _ObserverRouteTeamResolver:
    """Resolve Context team authority from the Observer event and connector route."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "_ObserverRouteTeamResolver(connection=<redacted>)"

    def __call__(self, scope: TenantScope, observation_id: str) -> str | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            cursor.execute(
                "SELECT set_config('app.processing_purpose', %s, true)",
                (scope.processing_purpose,),
            )
            cursor.execute(
                """
                SELECT event.team_ref, route.team_ref
                FROM observer.observation_events AS event
                JOIN observer.connector_instances AS route
                  ON route.site_id = event.site_id
                 AND route.connector = event.connector
                 AND route.connector_instance_id = event.connector_instance_id
                WHERE event.site_id = %s
                  AND event.event_id = %s
                  AND event.processing_purpose = %s
                """,
                (
                    scope.site_id,
                    observation_id,
                    scope.processing_purpose,
                ),
            )
            row = cursor.fetchone()
        if row is None or row[0] != row[1]:
            raise ValueError("Observer publication route binding is invalid")
        return None if row[0] is None else str(row[0])


def create_production_components(
    *,
    manifest: Mapping[str, Any],
    runtime_config: RuntimeConfig,
    secret_paths: ProjectionSecretPaths,
    projection_config_path: Path = DEFAULT_PROJECTION_CONFIG,
    phrase_resolver: TrustedPhraseResolver,
    connector: Callable[..., object] | None = None,
    transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    clock: Clock | None = None,
) -> ModelProjectionComponents:
    """Preflight then compose three-role production projection components."""

    if not callable(phrase_resolver):
        raise RuntimeSupportError("trusted phrase resolver is not injected")
    validate_manifest_binding(manifest, runtime_config)
    validate_deepseek_manifest(manifest)
    configured = component_settings(runtime_config, "model_worker")
    if configured.provider_mode != "deepseek":
        raise RuntimeSupportError("model projection requires DeepSeek provider mode")
    projection = load_projection_config(
        projection_config_path,
        expected_site_id=runtime_config.site_id,
    )
    if not projection.controlled_egress:
        raise RuntimeSupportError("projection controlled egress is disabled")

    # Every config, secret and local storage boundary is validated before connect.
    for settings in projection.connections.values():
        load_secret_file(settings.password_file)
    api_key = load_secret_file(secret_paths.deepseek_api_key)
    hmac_key = _read_exact_private_key_file(secret_paths.tokenizer_hmac_key)
    _read_exact_private_key_file(secret_paths.mapping_vault_key)
    evidence_store = ContentAddressedEvidenceStore(projection.evidence_cas_root)
    active_clock = clock or (lambda: datetime.now(UTC))
    vault = EncryptedFileMappingVault.from_key_file(
        root=projection.tokenizer_vault_root,
        key_file=secret_paths.mapping_vault_key,
        clock=active_clock,
    )

    connections: list[Any] = []
    try:
        for role in ("observer", "context", "agent"):
            connections.append(
                connect_postgres(
                    projection.connections[role],
                    connector=connector,
                )
            )
        observer_connection, context_connection, agent_connection = connections
        raw_loader = ContentAddressedEvidenceTextLoader(evidence_store)
        projection_store = PostgresCommunicationRepository(
            connection=observer_connection,
        )
        projection_repository = PostgresObservationProjectionRepository(
            connection=observer_connection,
            projection_store=projection_store,
            raw_loader=raw_loader,
        )
        outbox = PostgresProjectionOutboxRepository(observer_connection)
        context_publisher = PostgresCommunicationIntelligenceRepository(
            context_connection,
            team_ref_resolver=_ObserverRouteTeamResolver(observer_connection),
        )
        ledger = PostgresMonthlyUsageLedger(
            connection=agent_connection,
            site_id=projection.site_id,
            clock=active_clock,
        )
        audit = PostgresModelInvocationRepository(agent_connection)
        assembly = DeepSeekAssembly(
            base_url=str(manifest["deepseek"]["base_url"]),
            model=str(manifest["deepseek"]["model"]),
            api_key=api_key.reveal(),
            budget_ledger=ledger,
            tokenizer_vault=vault,
            controlled_egress=True,
        )
        provider = create_deepseek_observation_provider(
            assembly=assembly,
            audit_repository=audit,
            transport_factory=transport_factory,
            network_enabled=True,
            clock=active_clock,
        )
        tokenizer = StableTokenizer(hmac_key=hmac_key, vault=vault)
    except Exception:
        for connection in reversed(connections):
            with suppress(Exception):
                connection.close()
        raise

    def close() -> None:
        for connection in reversed(connections):
            with suppress(Exception):
                connection.close()

    return ModelProjectionComponents(
        outbox=outbox,
        projection_repository=projection_repository,
        raw_loader=raw_loader,
        context_publisher=context_publisher,
        tokenizer=tokenizer,
        provider=provider,
        close=close,
    )


def build_worker(
    *,
    components: ModelProjectionComponents,
    site_id: str,
    processing_purpose: str,
    worker_id: str,
    phrase_resolver: TrustedPhraseResolver | None,
    clock: Clock,
    heartbeat_interval: float,
) -> ModelProjectionWorker:
    if phrase_resolver is None or not callable(phrase_resolver):
        raise ValueError("trusted phrase resolver is required")
    if not isinstance(components.provider, DeepSeekObservationProvider):
        raise ValueError("the exact DeepSeek observation provider is required")
    if not isinstance(components.tokenizer, StableTokenizer):
        raise ValueError("the stable local tokenizer is required")
    for candidate, message in (
        (components.raw_loader, "raw loader"),
        (components.close, "component closer"),
        (getattr(components.context_publisher, "publish", None), "Context publisher"),
        (
            getattr(components.projection_repository, "load_projection_source", None),
            "projection source repository",
        ),
        (
            getattr(components.projection_repository, "store_projection", None),
            "projection store repository",
        ),
    ):
        if not callable(candidate):
            raise ValueError(f"{message} is required")
    for method in ("claim", "heartbeat", "mark_published", "mark_failed"):
        if not callable(getattr(components.outbox, method, None)):
            raise ValueError("a fenced projection outbox repository is required")
    if not 0 < heartbeat_interval <= 60:
        raise ValueError("heartbeat interval must be positive and bounded")
    lease_duration = timedelta(seconds=heartbeat_interval * _LEASE_INTERVAL_MULTIPLIER)
    _ = TenantScope(site_id, processing_purpose)
    tokenizer = TrustedProjectionTokenizer(
        tokenizer=components.tokenizer,
        phrase_resolver=phrase_resolver,
        clock=clock,
    )
    publisher = ObservationProjectionPublisher(
        repository=components.projection_repository,
        raw_loader=components.raw_loader,
        tokenizer=tokenizer,
        provider=components.provider,
        context_publisher=components.context_publisher,
        clock=clock,
        restricted_policy="local_tokenized",
    )
    worker = ModelProjectionWorker(
        outbox=components.outbox,
        publisher=publisher,
        worker_id=worker_id,
        clock=clock,
        lease_duration=lease_duration,
        heartbeat_runner=ThreadedProjectionHeartbeatRunner(interval_seconds=heartbeat_interval),
    )
    return worker


class Waiter(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


def run_worker(
    worker: ModelProjectionWorker,
    *,
    scope: TenantScope,
    stop_event: Event,
    idle_delay: float,
    waiter: Waiter | None = None,
) -> None:
    if not 0 < idle_delay <= 60:
        raise ValueError("idle delay must be positive and bounded")
    active_waiter = stop_event if waiter is None else waiter
    while not stop_event.is_set():
        result = worker.run_once(scope)
        if result.status is ProjectionRunStatus.IDLE:
            active_waiter.wait(idle_delay)


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    projection_config_path: Path = DEFAULT_PROJECTION_CONFIG,
    secret_paths: ProjectionSecretPaths | None = None,
    environ: Mapping[str, str] | None = None,
    components_factory: ComponentsFactory | None = None,
    phrase_resolver: TrustedPhraseResolver | None = None,
    worker_runner: WorkerRunner | None = None,
    stop_event: Event | None = None,
    clock: Clock | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    components: ModelProjectionComponents | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="model-worker",
            environ=environment,
        )
        if environment.get("GBOS_MODEL_PROJECTION_KILL_SWITCH", "true") != "false":
            raise LocalEntrypointDisabled("model projection worker is disabled by kill switch")
        if environment.get("GBOS_DEEPSEEK_EGRESS_ENABLED") != "true":
            raise LocalEntrypointDisabled("DeepSeek egress is disabled by default")
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        configured = component_settings(config, "model_worker")
        if configured.provider_mode != "deepseek":
            raise RuntimeSupportError("model projection requires DeepSeek provider mode")
        validate_deepseek_manifest(manifest)
        if phrase_resolver is None:
            raise RuntimeSupportError("trusted phrase resolver is not injected")
        active_paths = secret_paths or ProjectionSecretPaths(
            deepseek_api_key=DEFAULT_DEEPSEEK_API_KEY_FILE,
            tokenizer_hmac_key=DEFAULT_TOKENIZER_HMAC_KEY_FILE,
            mapping_vault_key=DEFAULT_MAPPING_VAULT_KEY_FILE,
        )
        if components_factory is not None:
            load_secret_file(config.postgres.password_file)
        load_secret_file(active_paths.deepseek_api_key)
        _read_exact_private_key_file(active_paths.tokenizer_hmac_key)
        _read_exact_private_key_file(active_paths.mapping_vault_key)

        if components_factory is None:
            components = create_production_components(
                manifest=manifest,
                runtime_config=config,
                secret_paths=active_paths,
                projection_config_path=projection_config_path,
                phrase_resolver=phrase_resolver,
            )
        else:
            components = components_factory(manifest, config, active_paths)
        active_clock = clock or (lambda: datetime.now(UTC))
        worker = build_worker(
            components=components,
            site_id=config.site_id,
            processing_purpose=MODEL_PROJECTION_PROCESSING_PURPOSE,
            worker_id=config.worker.worker_id,
            phrase_resolver=phrase_resolver,
            clock=active_clock,
            heartbeat_interval=config.worker.heartbeat_interval_seconds,
        )
        active_stop = stop_event or Event()
        active_runner = worker_runner or _run_worker
        active_runner(
            worker,
            TenantScope(config.site_id, MODEL_PROJECTION_PROCESSING_PURPOSE),
            active_stop,
            config.worker.idle_delay_seconds,
        )
        return 0
    except Exception:
        return 78
    finally:
        if components is not None:
            with suppress(Exception):
                components.close()


def _run_worker(
    worker: ModelProjectionWorker,
    scope: TenantScope,
    stop_event: Event,
    idle_delay: float,
) -> None:
    run_worker(
        worker,
        scope=scope,
        stop_event=stop_event,
        idle_delay=idle_delay,
    )


def _read_exact_private_key_file(path: Path) -> bytes:
    candidate = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise RuntimeSupportError("required private key file is absent") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size != 32
        ):
            raise RuntimeSupportError(
                "private key file must be a 32-byte regular non-symlink with mode 0600"
            )
        value = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(value) != 32:
        raise RuntimeSupportError("private key file length changed while reading")
    return value


def _valid_phrase(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 512
        and not any(character in value for character in ("\x00", "\r", "\n"))
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_DEEPSEEK_API_KEY_FILE",
    "DEFAULT_MAPPING_VAULT_KEY_FILE",
    "DEFAULT_PROJECTION_CONFIG",
    "DEFAULT_TOKENIZER_HMAC_KEY_FILE",
    "ModelProjectionComponents",
    "ModelProjectionWorker",
    "ProjectionLeaseConflict",
    "ProjectionOutboxClaim",
    "ProjectionRunResult",
    "ProjectionRunStatus",
    "ProjectionSecretPaths",
    "ThreadedProjectionHeartbeatRunner",
    "TrustedPhraseResolution",
    "TrustedProjectionTokenizer",
    "build_worker",
    "create_production_components",
    "main",
    "run_worker",
]
