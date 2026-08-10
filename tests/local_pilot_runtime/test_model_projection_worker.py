from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from services.agent_runtime.local_entrypoint import load_local_manifest
from services.context.context_service.communication_intelligence import (
    PostgresCommunicationIntelligenceRepository,
)
from services.local_pilot_runtime import model_projection_worker
from services.local_pilot_runtime.model_projection_worker import (
    ModelProjectionComponents,
    ModelProjectionWorker,
    ProjectionLeaseConflict,
    ProjectionOutboxClaim,
    ProjectionRunStatus,
    ProjectionSecretPaths,
    TrustedPhraseResolution,
    TrustedProjectionTokenizer,
    build_worker,
    create_production_components,
    main,
    run_worker,
)
from services.local_pilot_runtime.projection_config import ProjectionConfigError
from services.local_pilot_runtime.runtime_support import load_runtime_config
from services.local_pilot_runtime.trusted_phrase_lexicon import (
    TrustedPhraseLexiconResolver,
    load_trusted_phrase_resolver,
)
from services.model_gateway.deepseek import DEEPSEEK_MODEL
from services.model_gateway.observation_provider import DeepSeekObservationProvider
from services.model_gateway.tokenization import InMemoryMappingVault, StableTokenizer
from services.observer.observer.model_fatal_latch import (
    InMemoryModelFatalLatch,
    PostgresModelFatalLatchRepository,
)
from services.observer.observer.model_projection import LocalTokenizationResult, ProjectionFailure
from services.observer.observer.models import TenantScope
from services.observer.observer.projection_outbox import PostgresProjectionOutboxRepository

NOW = datetime(2026, 8, 8, 10, tzinfo=UTC)
SCOPE = TenantScope("gbos.localhost", "observation_processing")


class _ImmediateHeartbeat:
    def run(
        self,
        execute: Callable[[], object],
        heartbeat: Callable[[], object],
    ) -> object:
        heartbeat()
        return execute()


def _claim(
    *,
    attempt: int = 1,
    fence_token: str = "fence-SYNTH-001",
) -> ProjectionOutboxClaim:
    return ProjectionOutboxClaim(
        site_id=SCOPE.site_id,
        outbox_id="outbox-SYNTH-001",
        observation_id="observation-SYNTH-001",
        idempotency_key="context-normalized:SYNTH-001",
        status="leased",
        attempt=attempt,
        max_attempts=3,
        lease_owner="model-projection-worker-1",
        lease_expires_at=NOW + timedelta(minutes=2),
        fence_token=fence_token,
    )


class _Outbox:
    def __init__(
        self,
        *,
        claims: list[ProjectionOutboxClaim | None] | None = None,
        failure_status: str = "retry",
        lose_on: str | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.claims = list(claims or [_claim()])
        self.failure_status = failure_status
        self.lose_on = lose_on
        self.events = [] if events is None else events
        self.published: list[tuple[str, int, str]] = []
        self.failed: list[tuple[str, int, str, str]] = []
        self.claim_count = 0

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProjectionOutboxClaim | None:
        assert scope == SCOPE
        assert worker_id == "model-projection-worker-1"
        assert now == NOW
        assert lease_duration == timedelta(seconds=10)
        self.claim_count += 1
        return self.claims.pop(0) if self.claims else None

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
    ) -> None:
        assert scope == SCOPE
        assert outbox_id == "outbox-SYNTH-001"
        assert worker_id == "model-projection-worker-1"
        assert expected_attempt >= 1
        assert fence_token
        assert now == NOW
        assert lease_duration == timedelta(seconds=10)
        self.events.append("heartbeat")
        if self.lose_on == "heartbeat":
            raise ProjectionLeaseConflict("lease lost")

    def mark_published(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        fence_token: str,
        now: datetime,
    ) -> None:
        assert scope == SCOPE and worker_id == "model-projection-worker-1"
        self.events.append("mark_published")
        if self.lose_on == "publish":
            raise ProjectionLeaseConflict("lease lost")
        self.published.append((outbox_id, expected_attempt, fence_token))

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
    ) -> str:
        assert scope == SCOPE and worker_id == "model-projection-worker-1"
        assert retry_at == NOW + timedelta(seconds=30)
        self.events.append("mark_failed")
        if self.lose_on == "failure":
            raise ProjectionLeaseConflict("lease lost")
        self.failed.append((outbox_id, expected_attempt, fence_token, error_code))
        return self.failure_status


def _worker(
    outbox: _Outbox,
    publisher: Callable[[TenantScope, str, str], object],
    *,
    fatal_latch: InMemoryModelFatalLatch | None = None,
) -> ModelProjectionWorker:
    return ModelProjectionWorker(
        outbox=outbox,
        publisher=publisher,
        worker_id="model-projection-worker-1",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=30),
        heartbeat_runner=_ImmediateHeartbeat(),
        fatal_latch=fatal_latch or InMemoryModelFatalLatch(),
    )


def test_success_heartbeats_and_marks_only_after_context_projection_returns() -> None:
    events: list[str] = []
    outbox = _Outbox(events=events)

    def publish(scope: TenantScope, observation_id: str, idempotency_key: str) -> None:
        assert scope == SCOPE
        assert observation_id == "observation-SYNTH-001"
        assert idempotency_key == "context-normalized:SYNTH-001"
        events.append("context_persisted")

    result = _worker(outbox, publish).run_once(SCOPE)

    assert result.status is ProjectionRunStatus.PUBLISHED
    assert events == ["heartbeat", "context_persisted", "mark_published"]
    assert outbox.published == [("outbox-SYNTH-001", 1, "fence-SYNTH-001")]
    assert outbox.failed == []


@pytest.mark.parametrize(
    ("failure_status", "expected"),
    [
        ("retry", ProjectionRunStatus.RETRY),
        ("dead_letter", ProjectionRunStatus.DEAD_LETTER),
    ],
)
def test_context_failure_uses_safe_retry_or_dead_letter_and_never_marks_published(
    failure_status: str,
    expected: ProjectionRunStatus,
) -> None:
    outbox = _Outbox(failure_status=failure_status)

    def fail(*_: object) -> None:
        raise RuntimeError("plaintext body, mapping and secret must never escape")

    result = _worker(outbox, fail).run_once(SCOPE)

    assert result.status is expected
    assert outbox.published == []
    assert outbox.failed == [("outbox-SYNTH-001", 1, "fence-SYNTH-001", "projection_failed")]
    assert "plaintext" not in repr(result)
    assert "secret" not in repr(result)


@pytest.mark.parametrize("lose_on", ["heartbeat", "publish", "failure"])
def test_lost_lease_is_fenced_without_a_false_terminal_mark(lose_on: str) -> None:
    outbox = _Outbox(lose_on=lose_on)

    def project(*_: object) -> None:
        if lose_on == "failure":
            raise RuntimeError("provider unavailable")

    result = _worker(outbox, project).run_once(SCOPE)

    assert result.status is ProjectionRunStatus.LEASE_LOST
    assert outbox.published == []
    assert outbox.failed == []


@pytest.mark.parametrize(
    "claim",
    [
        replace(_claim(), lease_owner="another-worker"),
        replace(_claim(), lease_expires_at=NOW),
    ],
)
def test_invalid_or_expired_claim_is_reported_as_lost_without_projection(
    claim: ProjectionOutboxClaim,
) -> None:
    outbox = _Outbox(claims=[claim])
    projections: list[str] = []

    result = _worker(outbox, lambda *_: projections.append("called")).run_once(SCOPE)

    assert result.status is ProjectionRunStatus.LEASE_LOST
    assert projections == []
    assert outbox.published == []
    assert outbox.failed == []


def test_restart_reuses_the_persisted_idempotency_key_exactly() -> None:
    claims = [_claim(attempt=1), _claim(attempt=2, fence_token="fence-SYNTH-002")]
    outbox = _Outbox(claims=claims)
    keys: list[str] = []

    def publish(_scope: TenantScope, _observation_id: str, idempotency_key: str) -> None:
        keys.append(idempotency_key)

    first = _worker(outbox, publish).run_once(SCOPE)
    second = _worker(outbox, publish).run_once(SCOPE)

    assert first.status is ProjectionRunStatus.PUBLISHED
    assert second.status is ProjectionRunStatus.PUBLISHED
    assert keys == [
        "context-normalized:SYNTH-001",
        "context-normalized:SYNTH-001",
    ]


def test_fatal_latch_blocks_before_outbox_claim() -> None:
    latch = InMemoryModelFatalLatch()
    latch.trip(SCOPE, error_code="model_mismatch", now=NOW)
    outbox = _Outbox()
    calls: list[str] = []

    result = _worker(
        outbox,
        lambda *_: calls.append("publisher"),
        fatal_latch=latch,
    ).run_once(SCOPE)

    assert result.status is ProjectionRunStatus.FATAL_LATCHED
    assert outbox.claim_count == 0
    assert calls == []
    assert outbox.failed == []


@pytest.mark.parametrize(
    "code",
    ["response_invalid_json", "model_mismatch", "budget_hard_stop"],
)
def test_worker_preserves_safe_fatal_provider_code_and_trips_latch(code: str) -> None:
    latch = InMemoryModelFatalLatch()
    outbox = _Outbox()

    def fail(*_: object) -> None:
        raise ProjectionFailure(code)

    result = _worker(outbox, fail, fatal_latch=latch).run_once(SCOPE)

    assert result.status is ProjectionRunStatus.RETRY
    assert outbox.failed == [("outbox-SYNTH-001", 1, "fence-SYNTH-001", code)]
    assert latch.status(SCOPE).error_code == code


def test_latch_persistence_failure_poison_stops_all_future_claims() -> None:
    class UnavailableLatch(InMemoryModelFatalLatch):
        def trip(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("database detail and secret")

    outbox = _Outbox(claims=[_claim(attempt=1), _claim(attempt=2, fence_token="fence-SYNTH-002")])
    calls: list[str] = []

    def fail(*_: object) -> None:
        calls.append("publisher")
        raise ProjectionFailure(
            "model_fatal_latch_unavailable",
            fatal_code="output_schema_invalid",
        )

    worker = _worker(outbox, fail, fatal_latch=UnavailableLatch())
    first = worker.run_once(SCOPE)
    second = worker.run_once(SCOPE)

    assert first.status is ProjectionRunStatus.LATCH_UNAVAILABLE
    assert second.status is ProjectionRunStatus.LATCH_UNAVAILABLE
    assert outbox.claim_count == 1
    assert calls == ["publisher"]
    assert outbox.failed == []


def test_worker_retries_fatal_latch_persistence_before_any_future_claim() -> None:
    latch = InMemoryModelFatalLatch()
    outbox = _Outbox()

    def fail(*_: object) -> None:
        raise ProjectionFailure(
            "model_fatal_latch_unavailable",
            fatal_code="response_protocol_error",
        )

    worker = _worker(outbox, fail, fatal_latch=latch)
    first = worker.run_once(SCOPE)
    second = worker.run_once(SCOPE)

    assert first.status is ProjectionRunStatus.RETRY
    assert second.status is ProjectionRunStatus.FATAL_LATCHED
    assert outbox.claim_count == 1
    assert outbox.failed == [("outbox-SYNTH-001", 1, "fence-SYNTH-001", "response_protocol_error")]
    assert latch.status(SCOPE).error_code == "response_protocol_error"


def test_daemon_waits_only_when_idle_and_honors_stop_event() -> None:
    stop = Event()
    waits: list[float] = []
    outbox = _Outbox(claims=[None])

    class _StopOnWait:
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout is not None
            waits.append(timeout)
            stop.set()
            return True

    run_worker(
        _worker(outbox, lambda *_: None),
        scope=SCOPE,
        stop_event=stop,
        idle_delay=0.25,
        waiter=_StopOnWait(),
    )

    assert waits == [0.25]


def test_daemon_backs_off_while_fatal_latch_remains_persisted() -> None:
    stop = Event()
    waits: list[float] = []
    latch = InMemoryModelFatalLatch()
    latch.trip(SCOPE, error_code="budget_hard_stop", now=NOW)

    class _StopOnWait:
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout is not None
            waits.append(timeout)
            stop.set()
            return True

    runner = Thread(
        target=lambda: run_worker(
            _worker(_Outbox(), lambda *_: None, fatal_latch=latch),
            scope=SCOPE,
            stop_event=stop,
            idle_delay=0.25,
            waiter=_StopOnWait(),
        )
    )
    runner.start()
    runner.join(0.2)
    if runner.is_alive():
        stop.set()
        runner.join(2)

    assert runner.is_alive() is False
    assert waits == [0.25]


def test_trusted_tokenizer_covers_email_phone_name_and_organization() -> None:
    tokenizer = StableTokenizer(hmac_key=b"h" * 32, vault=InMemoryMappingVault())
    trusted = TrustedProjectionTokenizer(
        tokenizer=tokenizer,
        phrase_resolver=lambda *_: TrustedPhraseResolution(
            names=("Alice Zhang",),
            organizations=("Example Trading LLC",),
            names_complete=True,
            organizations_complete=True,
            resolver_version="trusted-directory-v1",
        ),
        clock=lambda: NOW,
    )
    raw = "Alice Zhang at Example Trading LLC: alice@example.com, phone +971 50 123 4567"

    result = trusted(SCOPE, "observation-SYNTH-001", raw)

    assert isinstance(result, LocalTokenizationResult)
    assert "Alice Zhang" not in result.text
    assert "Example Trading LLC" not in result.text
    assert "alice@example.com" not in result.text
    assert "+971 50 123 4567" not in result.text
    assert "<ENTITY_" in result.text
    assert "<EMAIL_" in result.text
    assert "<PHONE_" in result.text
    assert raw not in repr(trusted)


def test_unproven_name_or_organization_resolution_fails_before_tokenization() -> None:
    tokenizer = StableTokenizer(hmac_key=b"h" * 32, vault=InMemoryMappingVault())
    trusted = TrustedProjectionTokenizer(
        tokenizer=tokenizer,
        phrase_resolver=lambda *_: object(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="trusted phrase"):
        trusted(SCOPE, "observation-SYNTH-001", "DO-NOT-SEND Alice at Example")


class _Gateway:
    def invoke(self, _request: object) -> object:
        raise AssertionError("composition test must not call the model")


class _ProjectionRepository:
    def load_projection_source(self, *_: object) -> object:
        raise AssertionError("runner did not request a task")

    def store_projection(self, *_: object, **__: object) -> None:
        raise AssertionError("runner did not request a task")


class _ContextPublisher:
    def publish(self, *_: object, **__: object) -> None:
        raise AssertionError("runner did not request a task")


def _components() -> ModelProjectionComponents:
    fatal_latch = InMemoryModelFatalLatch()
    return ModelProjectionComponents(
        outbox=_Outbox(claims=[None]),
        projection_repository=_ProjectionRepository(),
        raw_loader=lambda *_: "never-read",
        context_publisher=_ContextPublisher(),
        tokenizer=StableTokenizer(
            hmac_key=b"h" * 32,
            vault=InMemoryMappingVault(),
        ),
        provider=DeepSeekObservationProvider(gateway=_Gateway()),  # type: ignore[arg-type]
        fatal_latch=fatal_latch,
        close=lambda: None,
    )


def _secret(path: Path, value: str) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _private_key(path: Path, value: bytes = b"k" * 32) -> Path:
    path.write_bytes(value)
    os.chmod(path, 0o600)
    return path


def _runtime_files(
    tmp_path: Path,
) -> tuple[Path, Path, ProjectionSecretPaths]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    postgres_password = _secret(secret_dir / "postgres_password", "db-secret")
    for name in ("agent_api_bearer", "context_api_bearer", "context_client_bearer"):
        _secret(secret_dir / name, f"{name}-secret")
    secret_paths = ProjectionSecretPaths(
        deepseek_api_key=_secret(secret_dir / "deepseek_api_key", "api-secret"),
        tokenizer_hmac_key=_private_key(secret_dir / "tokenizer_hmac_key"),
        mapping_vault_key=_private_key(secret_dir / "mapping_vault_key", b"v" * 32),
    )
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "capabilities": {
            "kingdee": False,
            "cloud_server": False,
            "cloud_business_storage": False,
            "external_send": False,
            "formal_business_commands": False,
        },
        "deepseek": {
            "enabled": True,
            "base_url": "https://api.deepseek.com",
            "model": DEEPSEEK_MODEL,
            "keychain_ref": "keychain://gbos/deepseek",
            "kill_switch": False,
            "thinking_default": "disabled",
            "max_input_tokens": 32768,
            "max_output_tokens": 4096,
            "soft_limit_usd": 50,
            "hard_limit_usd": 100,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def component(enabled: bool, kill_switch: bool, mode: str) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
            "provider_mode": mode,
            "synthetic_e2e": False,
        }

    config = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "postgres": {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": "gbos_observer_projection",
            "password_file": str(postgres_password),
            "connect_timeout_seconds": 3,
        },
        "auth": {
            "agent_api_bearer_file": str(secret_dir / "agent_api_bearer"),
            "context_api_bearer_file": str(secret_dir / "context_api_bearer"),
            "context_client_bearer_file": str(secret_dir / "context_client_bearer"),
            "context_auth_ref": "auth-model-projection",
        },
        "context_endpoint": {
            "base_url": "http://127.0.0.1:8001",
            "unix_socket": None,
        },
        "listen": {
            "host": "127.0.0.1",
            "agent_api_port": 8002,
            "context_api_port": 8001,
        },
        "components": {
            "agent_api": component(False, True, "disabled"),
            "context_api": component(False, True, "disabled"),
            "agent_worker": component(False, True, "disabled"),
            "model_worker": component(True, False, "deepseek"),
        },
        "worker": {
            "worker_id": "model-projection-worker-1",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return manifest_path, config_path, secret_paths


def _environment() -> dict[str, str]:
    return {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_MODEL_KILL_SWITCH": "false",
        "GBOS_MODEL_PROJECTION_KILL_SWITCH": "false",
        "GBOS_DEEPSEEK_EGRESS_ENABLED": "true",
    }


def _projection_config(tmp_path: Path) -> Path:
    cas = tmp_path / "projection-cas"
    vault = tmp_path / "projection-vault"
    cas.mkdir()
    vault.mkdir()
    secrets = tmp_path / "projection-secrets"
    secrets.mkdir()
    connections: dict[str, object] = {}
    for role, user in (
        ("observer", "gbos_observer_app"),
        ("context", "gbos_context_app"),
        ("agent", "gbos_agent_app"),
    ):
        connections[role] = {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": user,
            "password_file": str(_secret(secrets / role, f"{role}-password")),
            "connect_timeout_seconds": 3,
        }
    value = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "controlled_egress": True,
        "evidence_cas_root": str(cas),
        "tokenizer_vault_root": str(vault),
        "connections": connections,
    }
    path = tmp_path / "projection-connections.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _phrase_lexicon(
    tmp_path: Path,
    *,
    site_id: str = SCOPE.site_id,
    approved_at: datetime = NOW - timedelta(minutes=5),
    expires_at: datetime = NOW + timedelta(days=7),
) -> Path:
    value = {
        "schema_version": "1.0",
        "site_id": site_id,
        "resolver_version": "manual-attestation-2026-08-08",
        "approved_by": "local-data-steward",
        "approved_at": approved_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "names_complete": True,
        "organizations_complete": True,
        "names": ["Alice Zhang"],
        "organizations": ["Example Trading LLC"],
    }
    path = tmp_path / "trusted-phrase-lexicon"
    path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class _NoNetworkTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("component construction cannot perform HTTP")


class _ClosableConnection:
    def __init__(self, user: str, closed: list[str]) -> None:
        self.user = user
        self.closed = closed

    def close(self) -> None:
        self.closed.append(self.user)


def test_production_factory_preflights_all_roles_then_connects_exactly_three_and_closes(
    tmp_path: Path,
) -> None:
    manifest_path, runtime_path, secret_paths = _runtime_files(tmp_path)
    projection_path = _projection_config(tmp_path)
    users: list[str] = []
    closed: list[str] = []

    def connector(**kwargs: object) -> _ClosableConnection:
        user = str(kwargs["user"])
        users.append(user)
        return _ClosableConnection(user, closed)

    components = create_production_components(
        manifest=load_local_manifest(manifest_path),
        runtime_config=load_runtime_config(runtime_path),
        secret_paths=secret_paths,
        projection_config_path=projection_path,
        phrase_resolver=lambda *_: TrustedPhraseResolution(
            names=(),
            organizations=(),
            names_complete=True,
            organizations_complete=True,
            resolver_version="trusted-directory-v1",
        ),
        connector=connector,
        transport_factory=_NoNetworkTransport,
        clock=lambda: NOW,
    )

    assert users == ["gbos_observer_app", "gbos_context_app", "gbos_agent_app"]
    assert isinstance(components.outbox, PostgresProjectionOutboxRepository)
    assert isinstance(
        components.context_publisher,
        PostgresCommunicationIntelligenceRepository,
    )
    assert isinstance(components.provider, DeepSeekObservationProvider)
    assert isinstance(components.fatal_latch, PostgresModelFatalLatchRepository)
    assert not hasattr(components.provider, "_tokenizer")
    components.close()
    assert closed == ["gbos_agent_app", "gbos_context_app", "gbos_observer_app"]


def test_production_factory_invalid_third_role_secret_connects_nothing(
    tmp_path: Path,
) -> None:
    manifest_path, runtime_path, secret_paths = _runtime_files(tmp_path)
    projection_path = _projection_config(tmp_path)
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    os.chmod(Path(projection["connections"]["agent"]["password_file"]), 0o644)

    def forbidden(**_: object) -> object:
        raise AssertionError("preflight attempted a database connection")

    with pytest.raises(ProjectionConfigError):
        create_production_components(
            manifest=load_local_manifest(manifest_path),
            runtime_config=load_runtime_config(runtime_path),
            secret_paths=secret_paths,
            projection_config_path=projection_path,
            phrase_resolver=lambda *_: TrustedPhraseResolution(
                names=(),
                organizations=(),
                names_complete=True,
                organizations_complete=True,
                resolver_version="trusted-directory-v1",
            ),
            connector=forbidden,
            transport_factory=_NoNetworkTransport,
            clock=lambda: NOW,
        )


@pytest.mark.parametrize(
    "case",
    [
        "default_off",
        "projection_kill_switch",
        "model_kill_switch",
        "wrong_model",
        "wrong_base_url",
        "missing_runtime",
        "missing_factory",
        "missing_phrase_resolver",
        "plaintext_secret_environment",
        "unsafe_db_secret",
        "unsafe_api_key",
        "unsafe_tokenizer_key",
        "unsafe_vault_key",
    ],
)
def test_preflight_failure_exits_78_before_factory_db_or_http(
    tmp_path: Path,
    case: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path, config_path, secret_paths = _runtime_files(tmp_path)
    environment = _environment()

    def make_components(*_: object) -> ModelProjectionComponents:
        return _components()

    def resolve_phrases(*_: object) -> TrustedPhraseResolution:
        return TrustedPhraseResolution(
            names=(),
            organizations=(),
            names_complete=True,
            organizations_complete=True,
            resolver_version="trusted-directory-v1",
        )

    factory: Callable[..., ModelProjectionComponents] | None = make_components
    resolver: Callable[..., TrustedPhraseResolution] | None = resolve_phrases
    if case == "default_off":
        environment = {}
    elif case == "projection_kill_switch":
        environment["GBOS_MODEL_PROJECTION_KILL_SWITCH"] = "true"
    elif case == "model_kill_switch":
        environment["GBOS_MODEL_KILL_SWITCH"] = "true"
    elif case == "wrong_model":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["deepseek"]["model"] = "deepseek-chat"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "wrong_base_url":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["deepseek"]["base_url"] = "https://example.invalid"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "missing_runtime":
        config_path = tmp_path / "absent.json"
    elif case == "missing_factory":
        factory = None
    elif case == "missing_phrase_resolver":
        resolver = None
    elif case == "plaintext_secret_environment":
        environment["DEEPSEEK_API_KEY"] = "must-not-appear"
    elif case == "unsafe_db_secret":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        os.chmod(Path(config["postgres"]["password_file"]), 0o644)
    elif case == "unsafe_api_key":
        os.chmod(secret_paths.deepseek_api_key, 0o644)
    elif case == "unsafe_tokenizer_key":
        os.chmod(secret_paths.tokenizer_hmac_key, 0o644)
    elif case == "unsafe_vault_key":
        secret_paths.mapping_vault_key.unlink()
    calls: list[str] = []

    result = main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        secret_paths=secret_paths,
        environ=environment,
        components_factory=(
            None if factory is None else lambda *args: (calls.append("factory"), factory(*args))[1]
        ),
        phrase_resolver=resolver,
        worker_runner=lambda *_: calls.append("runner"),
    )

    assert result == 78
    assert calls == []
    assert capsys.readouterr() == ("", "")


def test_valid_main_builds_only_after_preflight_and_closes_components(
    tmp_path: Path,
) -> None:
    manifest_path, config_path, secret_paths = _runtime_files(tmp_path)
    components = _components()
    events: list[str] = []
    components = ModelProjectionComponents(
        outbox=components.outbox,
        projection_repository=components.projection_repository,
        raw_loader=components.raw_loader,
        context_publisher=components.context_publisher,
        tokenizer=components.tokenizer,
        provider=components.provider,
        fatal_latch=components.fatal_latch,
        close=lambda: events.append("close"),
    )
    stop = Event()

    def factory(*args: object) -> ModelProjectionComponents:
        assert len(args) == 3
        events.append("factory")
        return components

    def runner(
        worker: ModelProjectionWorker,
        scope: TenantScope,
        stop_event: Event,
        idle_delay: float,
    ) -> None:
        assert isinstance(worker, ModelProjectionWorker)
        assert scope == SCOPE
        assert idle_delay == 0.1
        events.append("runner")
        stop_event.set()

    result = main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        secret_paths=secret_paths,
        environ=_environment(),
        components_factory=factory,
        phrase_resolver=lambda *_: TrustedPhraseResolution(
            names=(),
            organizations=(),
            names_complete=True,
            organizations_complete=True,
            resolver_version="trusted-directory-v1",
        ),
        worker_runner=runner,
        stop_event=stop,
        clock=lambda: NOW,
    )

    assert result == 0
    assert events == ["factory", "runner", "close"]
    assert stop.is_set()


def test_main_loads_site_bound_lexicon_before_factory_when_resolver_is_not_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, config_path, secret_paths = _runtime_files(tmp_path)
    phrase_path = _phrase_lexicon(tmp_path)
    events: list[str] = []

    def load_resolver(
        path: Path,
        *,
        expected_site_id: str,
        clock: Callable[[], datetime],
    ) -> TrustedPhraseLexiconResolver:
        assert path == phrase_path
        assert expected_site_id == SCOPE.site_id
        events.append("lexicon")
        return load_trusted_phrase_resolver(
            path,
            expected_site_id=expected_site_id,
            clock=clock,
        )

    def factory(*_: object) -> ModelProjectionComponents:
        events.append("factory")
        return _components()

    monkeypatch.setattr(model_projection_worker, "load_trusted_phrase_resolver", load_resolver)

    result = main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        trusted_phrase_lexicon_path=phrase_path,
        secret_paths=secret_paths,
        environ=_environment(),
        components_factory=factory,
        worker_runner=lambda *_: events.append("runner"),
        clock=lambda: NOW,
    )

    assert result == 0
    assert events == ["lexicon", "factory", "runner"]


@pytest.mark.parametrize("case", ["absent", "expired", "site_mismatch"])
def test_main_rejects_untrusted_default_lexicon_before_factory_or_http(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_path, config_path, secret_paths = _runtime_files(tmp_path)
    if case == "absent":
        phrase_path = tmp_path / "absent-phrase-lexicon"
    elif case == "expired":
        phrase_path = _phrase_lexicon(tmp_path, expires_at=NOW)
    else:
        phrase_path = _phrase_lexicon(tmp_path, site_id="other.localhost")
    calls: list[str] = []

    def forbidden_factory(*_: object) -> ModelProjectionComponents:
        calls.append("factory")
        return _components()

    result = main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        trusted_phrase_lexicon_path=phrase_path,
        secret_paths=secret_paths,
        environ=_environment(),
        components_factory=forbidden_factory,
        worker_runner=lambda *_: calls.append("runner"),
        clock=lambda: NOW,
    )

    assert result == 78
    assert calls == []


def test_default_main_uses_closed_projection_factory_not_runtime_broad_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, config_path, secret_paths = _runtime_files(tmp_path)
    broad = json.loads(config_path.read_text(encoding="utf-8"))["postgres"]
    os.chmod(Path(broad["password_file"]), 0o644)
    components = _components()
    events: list[str] = []
    stop = Event()

    def production(**kwargs: object) -> ModelProjectionComponents:
        assert kwargs["projection_config_path"] == Path("/config/projection-connections.json")
        events.append("production")
        return components

    monkeypatch.setattr(model_projection_worker, "create_production_components", production)

    result = main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        secret_paths=secret_paths,
        environ=_environment(),
        phrase_resolver=lambda *_: TrustedPhraseResolution(
            names=(),
            organizations=(),
            names_complete=True,
            organizations_complete=True,
            resolver_version="trusted-directory-v1",
        ),
        worker_runner=lambda *_: (events.append("runner"), stop.set()),
        stop_event=stop,
        clock=lambda: NOW,
    )

    assert result == 0
    assert events == ["production", "runner"]


def test_build_worker_rejects_missing_resolver_and_redacts_components() -> None:
    components = _components()
    assert "api-secret" not in repr(components)
    assert "never-read" not in repr(components)

    with pytest.raises(ValueError, match="phrase resolver"):
        build_worker(
            components=components,
            site_id=SCOPE.site_id,
            processing_purpose=SCOPE.processing_purpose,
            worker_id="model-projection-worker-1",
            phrase_resolver=None,
            clock=lambda: NOW,
            heartbeat_interval=1.0,
        )


def test_module_import_is_side_effect_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted DB or HTTP")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)

    module = importlib.reload(model_projection_worker)

    assert callable(module.main)
    assert callable(module.run_worker)
