from __future__ import annotations

import importlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Literal

import pytest

from services.agent_runtime.materialization import FrappeDraftReceipt
from services.agent_runtime.models import canonical_payload_digest
from services.agent_runtime.proposals import MaterializationIntent
from services.context.context_service.communication_intelligence import (
    CommunicationDraftClaim,
    CommunicationDraftLeaseConflict,
    CommunicationDraftRunResult,
    PostgresCommunicationIntelligenceRepository,
)
from services.local_pilot_runtime import communication_draft_worker

NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _claim() -> CommunicationDraftClaim:
    values = {
        "subject": "Communication event event-001",
        "summary_zh": "客户希望确认样品交期。",
        "team": "team-sales",
        "evidence_refs": [{"evidence_ref": "evidence-001", "locator_ref": "evidence-001"}],
        "model_name": "deepseek-v4-flash",
        "model_version": "deepseek-v4-flash",
        "is_official_metric": False,
        "origin": "AI",
        "origin_reference": "event-001",
        "review_status": "AI Draft",
    }
    return CommunicationDraftClaim(
        site_id="alpha.example",
        draft_id="communication-draft-001",
        intelligence_id="communication-intelligence-001",
        observation_id="event-001",
        processing_purpose="observation_processing",
        subject="Communication event event-001",
        summary_zh="客户希望确认样品交期。",
        team_ref="team-sales",
        evidence_refs=("evidence-001",),
        model_name="deepseek-v4-flash",
        model_version="deepseek-v4-flash",
        payload_digest=canonical_payload_digest(
            {
                "operation": "create",
                "doctype": "GBOS Informal Observation",
                "values": values,
            }
        ),
        attempt=1,
        max_attempts=5,
        lease_owner="communication-worker",
        lease_expires_at=NOW + timedelta(seconds=30),
    )


class _Repository:
    def __init__(self, claim: CommunicationDraftClaim | None = None) -> None:
        self.claim = claim
        self.events: list[str] = []
        self.failures: list[str] = []
        self.receipt: FrappeDraftReceipt | None = None
        self.lose_heartbeat = False
        self.failure_state: Literal["retry", "dead_letter"] = "retry"

    def claim_draft(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> CommunicationDraftClaim | None:
        assert site_id == "alpha.example"
        self.events.append("claim")
        return self.claim

    def heartbeat_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        self.events.append("heartbeat")
        if self.lose_heartbeat:
            raise CommunicationDraftLeaseConflict("lease lost")

    def acknowledge_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        receipt: FrappeDraftReceipt,
    ) -> FrappeDraftReceipt:
        self.events.append("ack")
        self.receipt = receipt
        return receipt

    def fail_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"]:
        self.events.append("fail")
        self.failures.append(error_code)
        return self.failure_state


class _Client:
    def __init__(self, repository: _Repository, *, fail: bool = False) -> None:
        self.repository = repository
        self.fail = fail
        self.calls: list[tuple[MaterializationIntent, str, str]] = []

    def apply(
        self,
        intent: MaterializationIntent,
        *,
        request_id: str,
        request_digest: str,
    ) -> FrappeDraftReceipt:
        self.repository.events.append("apply")
        self.calls.append((intent, request_id, request_digest))
        if self.fail:
            raise RuntimeError("provider body with secret")
        return FrappeDraftReceipt(
            doctype=intent.doctype,
            name="INFO-0001",
            revision=0,
            request_id=request_id,
            request_digest=request_digest,
        )


def test_worker_heartbeats_then_applies_exact_reversible_informal_observation() -> None:
    repository = _Repository(_claim())
    client = _Client(repository)
    purposes: list[str] = []

    def client_factory(purpose: str) -> _Client:
        purposes.append(purpose)
        return client

    worker = communication_draft_worker.CommunicationDraftWorker(
        repository=repository,
        client_factory=client_factory,
        worker_id="communication-worker",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(seconds=10),
    )

    result = worker.run_once("alpha.example")

    assert result.status == "succeeded"
    assert repository.events == ["claim", "heartbeat", "apply", "ack"]
    assert purposes == ["observation_processing"]
    assert len(client.calls) == 1
    intent, request_id, request_digest = client.calls[0]
    assert intent.operation == "create"
    assert intent.doctype == "GBOS Informal Observation"
    assert intent.values == {
        "subject": "Communication event event-001",
        "summary_zh": "客户希望确认样品交期。",
        "team": "team-sales",
        "evidence_refs": [{"evidence_ref": "evidence-001", "locator_ref": "evidence-001"}],
        "model_name": "deepseek-v4-flash",
        "model_version": "deepseek-v4-flash",
        "is_official_metric": False,
        "origin": "AI",
        "origin_reference": "event-001",
        "review_status": "AI Draft",
    }
    assert request_id == "communication-draft-001"
    assert len(request_digest) == 64
    assert repository.receipt is not None
    for sensitive in (_claim().summary_zh, "evidence-001", "team-sales"):
        assert sensitive not in repr(worker)


def test_worker_stops_before_frappe_when_heartbeat_loses_lease() -> None:
    repository = _Repository(_claim())
    repository.lose_heartbeat = True
    client = _Client(repository)
    worker = communication_draft_worker.CommunicationDraftWorker(
        repository=repository,
        client_factory=lambda _purpose: client,
        worker_id="communication-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once("alpha.example")

    assert result.status == "lease_lost"
    assert repository.events == ["claim", "heartbeat"]
    assert client.calls == []


def test_worker_rejects_outbox_payload_digest_drift_before_frappe() -> None:
    repository = _Repository(replace(_claim(), payload_digest="f" * 64))
    client = _Client(repository)
    worker = communication_draft_worker.CommunicationDraftWorker(
        repository=repository,
        client_factory=lambda _purpose: client,
        worker_id="communication-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once("alpha.example")

    assert result.status == "retry"
    assert result.error_code == "frappe_body_conflict"
    assert repository.events == ["claim", "heartbeat", "fail"]
    assert client.calls == []


@pytest.mark.parametrize("failure_state", ["retry", "dead_letter"])
def test_worker_delegates_frappe_failure_to_outbox_state_machine(
    failure_state: Literal["retry", "dead_letter"],
) -> None:
    repository = _Repository(_claim())
    repository.failure_state = failure_state
    client = _Client(repository, fail=True)
    worker = communication_draft_worker.CommunicationDraftWorker(
        repository=repository,
        client_factory=lambda _purpose: client,
        worker_id="communication-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once("alpha.example")

    assert result.status == failure_state
    assert repository.events == ["claim", "heartbeat", "apply", "fail"]
    assert repository.receipt is None
    assert repository.failures == ["communication_draft_failed"]
    assert "secret" not in repr(result)


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        raise AssertionError("preflight/composition must not perform HTTP")


def _secret(path: Path, value: str, *, mode: int = 0o600) -> Path:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def _files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    postgres_password = _secret(secret_dir / "postgres_password", "db-secret")
    for name in (
        "agent_api_bearer",
        "context_api_bearer",
        "context_client_bearer",
    ):
        _secret(secret_dir / name, f"{name}-secret")
    frappe_key = _secret(secret_dir / "frappe_materializer_api_key", "frappe-key")
    frappe_secret = _secret(
        secret_dir / "frappe_materializer_api_secret",
        "frappe-secret",
    )
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "alpha.example",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {"enabled": False, "kill_switch": True},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def component(enabled: bool, kill_switch: bool) -> dict[str, object]:
        return {
            "enabled": enabled,
            "kill_switch": kill_switch,
            "provider_mode": "disabled",
            "synthetic_e2e": False,
        }

    config = {
        "schema_version": "1.0",
        "site_id": "alpha.example",
        "postgres": {
            "host": "127.0.0.1",
            "port": 55432,
            "database": "gbos_local_pilot",
            "user": "gbos_context_app",
            "password_file": str(postgres_password),
            "connect_timeout_seconds": 3,
        },
        "auth": {
            "agent_api_bearer_file": str(secret_dir / "agent_api_bearer"),
            "context_api_bearer_file": str(secret_dir / "context_api_bearer"),
            "context_client_bearer_file": str(secret_dir / "context_client_bearer"),
            "context_auth_ref": "auth-communication-materializer",
        },
        "context_endpoint": {
            "base_url": "http://127.0.0.1:8000",
            "unix_socket": None,
        },
        "listen": {
            "host": "127.0.0.1",
            "agent_api_port": 8002,
            "context_api_port": 8001,
        },
        "components": {
            "agent_api": component(False, True),
            "context_api": component(False, True),
            "agent_worker": component(True, False),
            "model_worker": component(False, True),
        },
        "worker": {
            "worker_id": "communication-worker",
            "idle_delay_seconds": 0.1,
            "heartbeat_interval_seconds": 1.0,
        },
    }
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return manifest_path, config_path, frappe_key, frappe_secret


def _enabled_environment() -> dict[str, str]:
    return {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_COMMUNICATION_DRAFT_KILL_SWITCH": "false",
    }


def test_entrypoint_import_is_side_effect_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted DB or network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)
    module = importlib.reload(communication_draft_worker)
    assert callable(module.main)
    assert callable(module.run_worker)


@pytest.mark.parametrize("endpoint", ["loopback", "unix", "internal"])
def test_main_composes_real_repository_and_injectable_loop(
    tmp_path: Path,
    endpoint: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    if endpoint != "loopback":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if endpoint == "unix":
            config["context_endpoint"]["unix_socket"] = "/run/gbos/sockets/frappe.sock"
        else:
            config["context_endpoint"]["base_url"] = "http://frappe-backend:8000"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    connection = _Connection()
    transport = _Transport()
    seen: list[tuple[object, str, Event, float]] = []

    def runner(
        worker: object,
        site_id: str,
        stop_event: Event,
        idle_delay: float,
        sleep: Any,
    ) -> None:
        seen.append((worker, site_id, stop_event, idle_delay))
        stop_event.set()

    result = communication_draft_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=_enabled_environment(),
        connector=lambda **_: connection,
        worker_runner=runner,
        clock=lambda: NOW,
        frappe_transport=transport,
    )

    assert result == 0
    assert len(seen) == 1
    worker, site_id, stop_event, idle_delay = seen[0]
    assert isinstance(worker, communication_draft_worker.CommunicationDraftWorker)
    assert isinstance(worker._repository, PostgresCommunicationIntelligenceRepository)
    assert worker._lease_duration.total_seconds() == 10
    assert site_id == "alpha.example"
    assert stop_event.is_set()
    assert idle_delay == 0.1
    assert transport.calls == []
    assert connection.closed is True
    assert "frappe-secret" not in repr(worker)
    client = worker._client_factory("observation_processing")
    assert "frappe-secret" not in repr(client)


@pytest.mark.parametrize(
    "base_url",
    (
        "http://frappe-backend.evil:8000",
        "http://frappe-backend:8001",
        "http://user:secret@frappe-backend:8000",
        "http://frappe-backend:8000?next=http://evil.invalid",
    ),
)
def test_internal_endpoint_confusion_exits_78_before_database_or_http(
    tmp_path: Path,
    base_url: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["context_endpoint"]["base_url"] = base_url
    config_path.write_text(json.dumps(config), encoding="utf-8")
    connector_calls: list[dict[str, Any]] = []
    transport = _Transport()

    result = communication_draft_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=_enabled_environment(),
        connector=lambda **kwargs: connector_calls.append(kwargs),
        worker_runner=lambda *_: None,
        frappe_transport=transport,
    )

    assert result == 78
    assert connector_calls == []
    assert transport.calls == []


@pytest.mark.parametrize(
    "case",
    [
        "kill_switch",
        "missing_kill_switch",
        "missing_config",
        "non_local_manifest",
        "unsafe_secret",
        "non_local_endpoint",
        "plaintext_secret_env",
        "timeout_not_below_lease",
    ],
)
def test_preflight_failure_exits_78_before_database_or_http(
    tmp_path: Path,
    case: str,
) -> None:
    manifest_path, config_path, frappe_key, frappe_secret = _files(tmp_path)
    environment = _enabled_environment()
    timeout = 3.0
    if case == "kill_switch":
        environment["GBOS_COMMUNICATION_DRAFT_KILL_SWITCH"] = "true"
    elif case == "missing_kill_switch":
        environment.pop("GBOS_COMMUNICATION_DRAFT_KILL_SWITCH")
    elif case == "missing_config":
        config_path = tmp_path / "missing-runtime.json"
    elif case == "non_local_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["mode"] = "production"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif case == "unsafe_secret":
        os.chmod(frappe_secret, 0o640)
    elif case == "non_local_endpoint":
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["context_endpoint"]["base_url"] = "http://192.0.2.10:8000"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    elif case == "plaintext_secret_env":
        environment["FRAPPE_API_SECRET"] = "must-not-be-read"
    elif case == "timeout_not_below_lease":
        timeout = 10.0
    connector_calls: list[dict[str, Any]] = []
    transport = _Transport()

    result = communication_draft_worker.main(
        manifest_path=manifest_path,
        runtime_config_path=config_path,
        frappe_api_key_path=frappe_key,
        frappe_api_secret_path=frappe_secret,
        environ=environment,
        connector=lambda **kwargs: connector_calls.append(kwargs),
        worker_runner=lambda *_: None,
        frappe_timeout_seconds=timeout,
        frappe_transport=transport,
    )

    assert result == 78
    assert connector_calls == []
    assert transport.calls == []


def test_run_worker_uses_injected_stop_event_and_sleep_only_when_idle() -> None:
    stop_event = Event()
    sleeps: list[float] = []

    class IdleWorker:
        def run_once(self, site_id: str) -> CommunicationDraftRunResult:
            assert site_id == "alpha.example"
            return CommunicationDraftRunResult("idle", None, None)

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        stop_event.set()

    communication_draft_worker.run_worker(
        IdleWorker(),
        site_id="alpha.example",
        stop_event=stop_event,
        idle_delay=0.25,
        sleep=sleep,
    )

    assert sleeps == [0.25]
