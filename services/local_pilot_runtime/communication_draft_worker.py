"""Default-off local worker for Context communication AI Drafts."""

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
from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.materialization import FrappeDraftClient
from services.agent_runtime.models import IdempotencyConflict, canonical_payload_digest
from services.agent_runtime.proposals import MaterializationIntent
from services.context.context_service.communication_intelligence import (
    CommunicationDraftClaim,
    CommunicationDraftLeaseConflict,
    CommunicationDraftRepository,
    CommunicationDraftRunResult,
    Connection,
    PostgresCommunicationIntelligenceRepository,
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
DEFAULT_FRAPPE_API_KEY_FILE = Path("/run/secrets/frappe_materializer_api_key")
DEFAULT_FRAPPE_API_SECRET_FILE = Path("/run/secrets/frappe_materializer_api_secret")
_FRAPPE_INTERNAL_BASE_URL = "http://frappe-backend:8000"
_FRAPPE_INTERNAL_HOSTS = frozenset({"frappe-backend"})
_LEASE_INTERVAL_MULTIPLIER = 10

Sleep = Callable[[float], None]
Clock = Callable[[], datetime]
FrappeClientFactory = Callable[[str], FrappeDraftClient]
WorkerRunner = Callable[["CommunicationDraftWorker", str, Event, float, Sleep], None]


class CommunicationDraftRunWorker(Protocol):
    def run_once(self, site_id: str) -> CommunicationDraftRunResult: ...


class CommunicationDraftWorker:
    """Fenced delivery of reversible Context intelligence to Frappe."""

    __slots__ = (
        "_client_factory",
        "_clock",
        "_lease_duration",
        "_repository",
        "_retry_delay",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        repository: CommunicationDraftRepository,
        client_factory: FrappeClientFactory,
        worker_id: str,
        clock: Clock,
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if not worker_id or not callable(client_factory) or not callable(clock):
            raise ValueError("communication draft worker dependencies are required")
        if lease_duration <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("communication draft worker durations must be positive")
        self._repository = repository
        self._client_factory = client_factory
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return (
            "CommunicationDraftWorker("
            f"worker_id={self._worker_id!r}, repository=<redacted>, "
            "client_factory=<redacted>, payload=<redacted>)"
        )

    def run_once(self, site_id: str) -> CommunicationDraftRunResult:
        claim = self._repository.claim_draft(
            site_id,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return CommunicationDraftRunResult("idle", None, None)
        try:
            self._repository.heartbeat_draft(
                claim.site_id,
                claim.draft_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                now=self._clock(),
                lease_duration=self._lease_duration,
            )
            intent = _intent(claim)
            request_digest = canonical_payload_digest(
                {
                    "operation": intent.operation,
                    "doctype": intent.doctype,
                    "values": dict(intent.values),
                }
            )
            if request_digest != claim.payload_digest:
                raise IdempotencyConflict("communication draft payload digest conflicts")
            client = self._client_factory(claim.processing_purpose)
            receipt = client.apply(
                intent,
                request_id=claim.draft_id,
                request_digest=request_digest,
            )
            if (
                receipt.doctype != intent.doctype
                or receipt.request_id != claim.draft_id
                or receipt.request_digest != request_digest
            ):
                raise IdempotencyConflict("Frappe communication draft receipt conflicts")
            self._repository.acknowledge_draft(
                claim.site_id,
                claim.draft_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                now=self._clock(),
                receipt=receipt,
            )
        except CommunicationDraftLeaseConflict:
            return CommunicationDraftRunResult(
                "lease_lost",
                claim.draft_id,
                claim.attempt,
                "lease_lost",
            )
        except Exception as exc:
            error_code = (
                "frappe_body_conflict"
                if isinstance(exc, IdempotencyConflict)
                else "communication_draft_failed"
            )
            try:
                status = self._repository.fail_draft(
                    claim.site_id,
                    claim.draft_id,
                    worker_id=self._worker_id,
                    expected_attempt=claim.attempt,
                    now=self._clock(),
                    retry_at=self._clock() + self._retry_delay,
                    error_code=error_code,
                )
            except CommunicationDraftLeaseConflict:
                return CommunicationDraftRunResult(
                    "lease_lost",
                    claim.draft_id,
                    claim.attempt,
                    "lease_lost",
                )
            return CommunicationDraftRunResult(
                status,
                claim.draft_id,
                claim.attempt,
                error_code,
            )
        return CommunicationDraftRunResult(
            "succeeded",
            claim.draft_id,
            claim.attempt,
        )


def build_worker(
    *,
    connection: object,
    config: RuntimeConfig,
    frappe_api_key: SecretValue,
    frappe_api_secret: SecretValue,
    clock: Clock,
    frappe_timeout_seconds: float,
    frappe_transport: FrappeJsonTransport | None = None,
) -> CommunicationDraftWorker:
    lease_duration = timedelta(
        seconds=config.worker.heartbeat_interval_seconds * _LEASE_INTERVAL_MULTIPLIER
    )
    if not 0 < frappe_timeout_seconds < lease_duration.total_seconds():
        raise RuntimeSupportError("Frappe timeout must be strictly below the worker lease")
    base_url = _frappe_base_url(config)
    allowed_internal_hosts = _frappe_allowed_internal_hosts(base_url)

    def client_factory(processing_purpose: str) -> FrappeDraftClient:
        return HttpFrappeDraftClient(
            base_url=base_url,
            api_key=frappe_api_key.reveal(),
            api_secret=frappe_api_secret.reveal(),
            auth_ref=config.auth.context_auth_ref,
            site_id=config.site_id,
            processing_purpose=processing_purpose,
            timeout_seconds=frappe_timeout_seconds,
            transport=frappe_transport,
            allowed_internal_hosts=allowed_internal_hosts,
        )

    repository = PostgresCommunicationIntelligenceRepository(
        cast(Connection, connection),
        team_ref_resolver=lambda _scope, _observation_id: None,
    )
    return CommunicationDraftWorker(
        repository=repository,
        client_factory=client_factory,
        worker_id=config.worker.worker_id,
        clock=clock,
        lease_duration=lease_duration,
        retry_delay=timedelta(seconds=30),
    )


def run_worker(
    worker: CommunicationDraftRunWorker,
    *,
    site_id: str,
    stop_event: Event,
    idle_delay: float,
    sleep: Sleep = time.sleep,
) -> None:
    if idle_delay <= 0:
        raise ValueError("idle_delay must be positive")
    while not stop_event.is_set():
        result = worker.run_once(site_id)
        if result.status == "idle":
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
            component="communication-draft-worker",
            environ=environment,
        )
        if environment.get("GBOS_COMMUNICATION_DRAFT_KILL_SWITCH", "true") != "false":
            raise LocalEntrypointDisabled("communication-draft-worker is disabled by kill switch")
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        component_settings(config, "agent_worker")
        lease_seconds = config.worker.heartbeat_interval_seconds * _LEASE_INTERVAL_MULTIPLIER
        if not 0 < frappe_timeout_seconds < lease_seconds:
            raise RuntimeSupportError("Frappe timeout must be strictly below the worker lease")
        frappe_api_key = load_secret_file(frappe_api_key_path)
        frappe_api_secret = load_secret_file(frappe_api_secret_path)
        base_url = _frappe_base_url(config)
        allowed_internal_hosts = _frappe_allowed_internal_hosts(base_url)
        HttpFrappeDraftClient(
            base_url=base_url,
            api_key=frappe_api_key.reveal(),
            api_secret=frappe_api_secret.reveal(),
            auth_ref=config.auth.context_auth_ref,
            site_id=config.site_id,
            processing_purpose="observation_processing",
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
            clock=clock or (lambda: datetime.now(UTC)),
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


def _intent(claim: CommunicationDraftClaim) -> MaterializationIntent:
    return MaterializationIntent(
        operation="create",
        doctype="GBOS Informal Observation",
        values={
            "subject": claim.subject,
            "summary_zh": claim.summary_zh,
            "team": claim.team_ref,
            "evidence_refs": [
                {"evidence_ref": value, "locator_ref": value} for value in claim.evidence_refs
            ],
            "model_name": claim.model_name,
            "model_version": claim.model_version,
            "is_official_metric": False,
            "origin": "AI",
            "origin_reference": claim.observation_id,
            "review_status": "AI Draft",
        },
    )


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
    worker: CommunicationDraftWorker,
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
    "CommunicationDraftWorker",
    "build_worker",
    "main",
    "run_worker",
]
