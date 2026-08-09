from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.local_pilot_runtime.observer_worker import (
    ConnectorPipeline,
    ObserverConnectorWorker,
    main,
    run_worker_daemon,
)
from services.observer.observer.connectors.email_delivery import EmailRawDeliveryDecoder
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.identity_tokens import IdentityTokenError
from services.observer.observer.local_pilot_storage import (
    InboundDeliveryMetadata,
    ProcessingJobMetadata,
)
from services.observer.observer.models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    TenantScope,
)
from services.observer.observer.normalizers import EmailObservationNormalizer

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")


def _job(connector: str, *, instance: str = "primary") -> ProcessingJobMetadata:
    return ProcessingJobMetadata(
        site_id=SCOPE.site_id,
        job_id=f"job-{connector}",
        connector=connector,
        connector_instance_id=instance,
        delivery_id=f"delivery-{connector}",
        stage="normalize",
        status="processing",
        attempt_count=1,
        max_attempts=3,
        idempotency_key=f"delivery:{connector}",
        generation=0,
        lease_owner="observer-worker",
        lease_expires_at=NOW + timedelta(seconds=60),
        lease_generation=1,
        next_retry_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeStorage:
    def __init__(
        self,
        *,
        job: ProcessingJobMetadata,
        delivery: InboundDeliveryMetadata,
    ) -> None:
        self.next_job: ProcessingJobMetadata | None = job
        self.delivery = delivery
        self.completed: list[tuple[str, ...]] = []
        self.quarantines: list[str] = []
        self.retries: list[str] = []

    def claim_processing_job(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata | None:
        assert scope == SCOPE
        claimed, self.next_job = self.next_job, None
        return claimed

    def get_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
    ) -> InboundDeliveryMetadata:
        assert scope == SCOPE
        assert delivery_id == self.delivery.delivery_id
        return self.delivery

    def heartbeat_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata:
        assert self.delivery.delivery_id
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
        )

    def complete_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        provider_event_ids: tuple[str, ...],
    ) -> ProcessingJobMetadata:
        self.completed.append(provider_event_ids)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="succeeded",
            lease_owner=None,
            lease_expires_at=None,
        )

    def quarantine_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        reason_code: str,
    ) -> ProcessingJobMetadata:
        self.quarantines.append(reason_code)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="quarantined",
            lease_owner=None,
            lease_expires_at=None,
        )

    def retry_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        next_retry_at: datetime,
        error_code: str,
    ) -> ProcessingJobMetadata:
        self.retries.append(error_code)
        return replace(
            _job(self.delivery.connector, instance=self.delivery.connector_instance_id),
            job_id=job_id,
            status="retry_wait",
            lease_owner=None,
            lease_expires_at=None,
        )


class Decoder:
    def __init__(self, connector: str) -> None:
        self.connector = connector
        self.received: list[bytes] = []

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]:
        self.received.append(exact_bytes)
        return (
            ConnectorItem(
                provider_event_id=f"{self.connector}-event-001",
                occurred_at=NOW,
                source_cursor="cursor-001",
                payload={"kind": self.connector},
            ),
        )


class Normalizer:
    def __init__(self, connector: str) -> None:
        self.connector = connector
        self.items: list[ConnectorItem] = []

    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput:
        self.items.append(item)
        return NormalizedObservationInput(
            channel="email" if self.connector == "email" else "chat",
            participants=(
                Participant(
                    "unknown",
                    f"unresolved:delivery:{self.connector}-event-001",
                ),
            ),
            evidence=(
                EvidenceArtifact(
                    media_type="application/octet-stream",
                    locator="delivery",
                    role="source",
                    reference=source_ref,
                ),
            ),
            consent_basis="pilot_deferred_review",
            data_classification="Restricted",
            retention_class="R1-operational",
            original_language="und",
            correlation_id=f"corr-{self.connector}",
        )


class Sink:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def accept_batch(self, *args: object) -> None:
        self.calls.append(args)


def _storage(
    tmp_path: Path,
    *,
    connector: str,
) -> tuple[FakeStorage, ContentAddressedEvidenceStore]:
    evidence = ContentAddressedEvidenceStore(tmp_path)
    stored = evidence.put(
        SCOPE,
        f"private-{connector}-payload".encode(),
        media_type="application/octet-stream",
    )
    job = _job(connector)
    delivery = InboundDeliveryMetadata(
        site_id=SCOPE.site_id,
        connector=connector,
        connector_instance_id="primary",
        delivery_id=job.delivery_id,
        exact_body_sha256=stored.sha256,
        object_ref=stored.object_ref,
        byte_size=stored.size,
        media_type=stored.media_type,
        received_at=NOW,
        processing_status="processing",
        attempt_count=1,
        correlation_id=f"corr-{connector}",
        last_attempt_at=NOW,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return FakeStorage(job=job, delivery=delivery), evidence


@pytest.mark.parametrize("connector", ["whatsapp", "email", "wecom"])
def test_worker_dispatches_claimed_connector_and_restart_does_not_repeat(
    tmp_path: Path,
    connector: str,
) -> None:
    storage, evidence = _storage(tmp_path, connector=connector)
    decoder = Decoder(connector)
    normalizer = Normalizer(connector)
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            connector: ConnectorPipeline(
                decoder=decoder,
                normalizer=normalizer,
            )
        },
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    first = worker.run_once(SCOPE)
    restarted = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            connector: ConnectorPipeline(
                decoder=decoder,
                normalizer=normalizer,
            )
        },
        sink=sink,
        worker_id="observer-worker-restarted",
        clock=lambda: NOW,
    ).run_once(SCOPE)

    assert first is not None
    assert first.status == "succeeded"
    assert first.normalized_count == 1
    assert restarted is None
    assert len(decoder.received) == 1
    assert len(normalizer.items) == 1
    assert len(sink.calls) == 1
    assert storage.completed == [(f"{connector}-event-001",)]


def test_unknown_connector_is_quarantined_without_reading_or_sinking(
    tmp_path: Path,
) -> None:
    storage, evidence = _storage(tmp_path, connector="phone")
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={},
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None
    assert result.status == "quarantined"
    assert result.normalized_count == 0
    assert storage.quarantines == ["unsupported_connector"]
    assert storage.completed == []
    assert sink.calls == []
    assert "storage=<redacted>" in repr(worker)
    assert main() == 78


def test_email_decoder_receives_durable_metadata_and_hands_transient_evidence_to_sink(
    tmp_path: Path,
) -> None:
    evidence = ContentAddressedEvidenceStore(tmp_path)
    raw = (
        b"From: private@example.invalid\r\n"
        b"To: pilot@example.invalid\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\nprivate email body"
    )
    stored = evidence.put(SCOPE, raw, media_type="message/rfc822")
    job = _job("email")
    delivery = InboundDeliveryMetadata(
        site_id=SCOPE.site_id,
        connector="email",
        connector_instance_id="primary",
        delivery_id=job.delivery_id,
        exact_body_sha256=stored.sha256,
        object_ref=stored.object_ref,
        byte_size=stored.size,
        media_type="message/rfc822",
        received_at=NOW,
        processing_status="processing",
        attempt_count=1,
        correlation_id="corr-email",
        last_attempt_at=NOW,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    storage = FakeStorage(job=job, delivery=delivery)
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            "email": ConnectorPipeline(
                decoder=EmailRawDeliveryDecoder(),
                normalizer=EmailObservationNormalizer(),
            )
        },
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None
    assert result.status == "succeeded"
    assert storage.quarantines == []
    assert len(sink.calls) == 1
    normalized = sink.calls[0][4]
    assert normalized[0].evidence[0].reference == stored.object_ref
    assert normalized[0].evidence[1].content == b"private email body"
    assert storage.completed == [(job.delivery_id,)]


class _Stop:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        assert seconds == 1.0
        self.stopped = True
        return True


class _DaemonWorker:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self, scope: TenantScope) -> None:
        assert scope == SCOPE
        self.calls += 1


def test_worker_daemon_honors_stop_event_during_idle_wait() -> None:
    worker = _DaemonWorker()
    stop = _Stop()

    run_worker_daemon(
        worker,  # type: ignore[arg-type]
        SCOPE,
        stop_event=stop,  # type: ignore[arg-type]
        idle_delay_seconds=1.0,
    )

    assert worker.calls == 1


def _private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def _worker_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    password = tmp_path / "postgres-password"
    password.write_text("not-a-real-password", encoding="utf-8")
    password.chmod(0o600)
    channel_names = ("email", "wecom", "whatsapp", "media")
    enabled = {"email", "whatsapp"}
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": SCOPE.site_id,
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {"enabled": False},
        "channels": {
            name: {
                "enabled": name in enabled,
                "activation_time": "2026-08-08T09:00:00Z" if name in enabled else None,
                "backfill_history": False,
                **({"credential_ref": None} if name != "media" else {"local_only": True}),
            }
            for name in channel_names
        },
    }
    runtime = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos",
            "user": "gbos_observer_app",
            "password_file": str(password),
            "connect_timeout_seconds": 2,
        },
        "auth": {
            "agent_api_bearer_file": str(password),
            "context_api_bearer_file": str(password),
            "context_client_bearer_file": str(password),
            "context_auth_ref": "local",
        },
        "context_endpoint": {"base_url": "http://context-api:8001", "unix_socket": None},
        "listen": {"host": "127.0.0.1", "agent_api_port": 8002, "context_api_port": 8001},
        "components": {
            name: {
                "enabled": True,
                "kill_switch": False,
                "provider_mode": "disabled",
                "synthetic_e2e": False,
            }
            for name in ("agent_api", "context_api", "agent_worker", "model_worker")
        },
        "worker": {
            "worker_id": "observer-worker",
            "idle_delay_seconds": 1,
            "heartbeat_interval_seconds": 5,
        },
    }
    email = tmp_path / "email.json"
    _private_json(
        email,
        {
            "instance_id": "email-primary",
            "team_ref": None,
            "agent_task_type": None,
            "account_user_ref": "email-owner@example.invalid",
            "host": "imap.example.invalid",
            "port": 993,
            "mailbox": "pilot-primary",
            "folder": "INBOX",
            "username": "private@example.invalid",
            "password": "not-a-real-password",
            "poll_limit": 10,
            "max_message_bytes": 1_000_000,
            "max_attachment_bytes": 100_000,
            "max_attachments": 5,
            "rescan_max_window_seconds": 86_400,
            "rescan_max_uids": 100,
            "initial_checkpoint": None,
        },
    )
    whatsapp = tmp_path / "whatsapp.json"
    _private_json(
        whatsapp,
        {
            "instance_id": "wa-primary",
            "team_ref": None,
            "agent_task_type": None,
            "account_user_ref": "USER-WA-OWNER",
            "app_secret": "not-a-real-secret",
            "verify_token": "not-a-real-token",
            "path": "/webhooks/whatsapp",
            "max_body_bytes": 1_024,
        },
    )
    connectors = {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "external_send": False,
        "evidence_cas_root": str(tmp_path / "cas"),
        "channels": {
            name: {
                "enabled": name in enabled,
                "kill_switch": name not in enabled,
                "activation_time": "2026-08-08T09:00:00Z" if name in enabled else None,
                "backfill_history": False,
                "credential_file": str(
                    email
                    if name == "email"
                    else whatsapp
                    if name == "whatsapp"
                    else tmp_path / name
                ),
            }
            for name in channel_names
        },
    }
    return (
        _private_json(tmp_path / "manifest.json", manifest),
        _private_json(tmp_path / "runtime.json", runtime),
        _private_json(tmp_path / "connectors.json", connectors),
    )


class _EntryConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _EntryStorage:
    def __init__(self) -> None:
        self.registered: list[tuple[ConnectorKey, object]] = []

    def register_connector_instance(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        **kwargs: object,
    ) -> None:
        assert scope == SCOPE
        self.registered.append((key, kwargs.get("account_user_ref")))


def test_main_composes_email_and_whatsapp_only_and_closes_connection(
    tmp_path: Path,
) -> None:
    manifest, runtime, connectors = _worker_files(tmp_path)
    identity_key = tmp_path / "identity-hmac-key"
    identity_key.write_bytes(b"i" * 32)
    identity_key.chmod(0o600)
    connection = _EntryConnection()
    storage = _EntryStorage()
    daemon_calls: list[ObserverConnectorWorker] = []

    result = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        identity_hmac_key_path=identity_key,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: connection,
        storage_factory=lambda _connection: storage,  # type: ignore[arg-type]
        daemon_runner=lambda worker, scope, **kwargs: (
            daemon_calls.append(worker),
            scope == SCOPE or (_ for _ in ()).throw(AssertionError("scope mismatch")),
        ),
        clock=lambda: NOW,
    )

    assert result == 0
    assert storage.registered == [
        (ConnectorKey("email", "email-primary"), "email-owner@example.invalid"),
        (ConnectorKey("whatsapp", "wa-primary"), "USER-WA-OWNER"),
    ]
    assert len(daemon_calls) == 1
    assert connection.closed is True


def test_main_plaintext_secret_rejects_before_database() -> None:
    database_calls: list[object] = []

    result = main(
        environ={"GBOS_LOCAL_RUNTIME_ENABLED": "true", "EMAIL_PASSWORD": "forbidden"},
        connector=lambda **kwargs: database_calls.append(kwargs),
    )

    assert result == 78
    assert database_calls == []


def test_main_missing_identity_key_exits_before_database_or_storage(tmp_path: Path) -> None:
    manifest, runtime, connectors = _worker_files(tmp_path)
    database_calls: list[object] = []
    storage_calls: list[object] = []

    result = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        identity_hmac_key_path=tmp_path / "missing-identity-key",
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: database_calls.append(kwargs),
        storage_factory=lambda connection: storage_calls.append(connection),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    assert result == 78
    assert database_calls == []
    assert storage_calls == []


def test_main_injects_scoped_identity_resolver_into_supported_normalizers(
    tmp_path: Path,
) -> None:
    manifest, runtime, connectors = _worker_files(tmp_path)
    identity_key = tmp_path / "identity-hmac-key"
    identity_key.write_bytes(b"i" * 32)
    identity_key.chmod(0o600)
    connection = _EntryConnection()
    storage = _EntryStorage()
    daemon_calls: list[ObserverConnectorWorker] = []

    result = main(
        manifest_path=manifest,
        runtime_config_path=runtime,
        connectors_path=connectors,
        identity_hmac_key_path=identity_key,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_CONNECTOR_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: connection,
        storage_factory=lambda _connection: storage,  # type: ignore[arg-type]
        daemon_runner=lambda worker, scope, **kwargs: daemon_calls.append(worker),
        clock=lambda: NOW,
    )

    assert result == 0
    pipelines = daemon_calls[0]._pipelines
    for name in ("email", "whatsapp"):
        normalizer = pipelines[name].normalizer
        assert normalizer._site_id == SCOPE.site_id
        assert normalizer._purpose == "observation_processing"
        rendered = repr(normalizer._identity_resolver)
        assert "key=<redacted" in rendered
        assert "i" * 32 not in rendered


def test_worker_tokenization_failure_quarantines_before_sink_or_completion(
    tmp_path: Path,
) -> None:
    class RejectingResolver:
        def resolve(self, *args: object) -> str:
            raise IdentityTokenError("identity_token.invalid_subject")

    evidence = ContentAddressedEvidenceStore(tmp_path)
    raw = (
        b"From: pii-sentinel@example.invalid\r\n"
        b"To: pilot@example.invalid\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\nprivate email body"
    )
    stored = evidence.put(SCOPE, raw, media_type="message/rfc822")
    job = _job("email")
    delivery = InboundDeliveryMetadata(
        site_id=SCOPE.site_id,
        connector="email",
        connector_instance_id="primary",
        delivery_id=job.delivery_id,
        exact_body_sha256=stored.sha256,
        object_ref=stored.object_ref,
        byte_size=stored.size,
        media_type="message/rfc822",
        received_at=NOW,
        processing_status="processing",
        attempt_count=1,
        correlation_id="corr-email",
        last_attempt_at=NOW,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    storage = FakeStorage(job=job, delivery=delivery)
    sink = Sink()
    worker = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence,
        pipelines={
            "email": ConnectorPipeline(
                decoder=EmailRawDeliveryDecoder(),
                normalizer=EmailObservationNormalizer(
                    identity_resolver=RejectingResolver(),
                    site_id=SCOPE.site_id,
                    purpose=SCOPE.processing_purpose,
                ),
            )
        },
        sink=sink,
        worker_id="observer-worker",
        clock=lambda: NOW,
    )

    result = worker.run_once(SCOPE)

    assert result is not None and result.status == "quarantined"
    assert sink.calls == []
    assert storage.completed == []
    assert storage.quarantines == ["email.invalid_subject"]
    assert "pii-sentinel@example.invalid" not in repr(result)
