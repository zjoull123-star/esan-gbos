from __future__ import annotations

import json
import os
from pathlib import Path

from services.email_gateway.provider import ProviderOutcome, ProviderSubmissionResult
from services.email_gateway.send_outbox import PostgresEmailSendRepository
from services.email_gateway.worker import WorkerAuthorityState
from services.local_pilot_runtime.email_send_worker import main
from services.local_pilot_runtime.secret_provider import MountedFileSecretProvider, SecretSpec
from tests.email_gateway.fakes.provider import FakeEmailProvider


def test_runtime_send_worker_is_default_off_before_database_or_provider_factory(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    result = main(
        manifest_path=tmp_path / "missing.json",
        config_path=tmp_path / "missing-runtime.json",
        environ={},
        connector=lambda **_kwargs: calls.append("database"),
        provider_factory=lambda: calls.append("provider"),  # type: ignore[arg-type,return-value]
    )

    assert result == 78
    assert calls == []


def test_runtime_module_contains_no_real_provider_or_transport_dependency() -> None:
    source = Path("services/local_pilot_runtime/email_send_worker.py").read_text()
    for forbidden in ("smtplib", "wecom_app_mail", "requests", "httpx", "SMTP"):
        assert forbidden not in source


def test_explicit_fake_runtime_composes_durable_worker_role_and_runs_consumer(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {},
        "email_gateway": {
            "send_kill_switch": False,
            "external_send": False,
        },
    }
    config = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "enabled": True,
        "kill_switch": False,
        "external_send": False,
        "provider_mode": "fake",
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_send_worker",
            "password_file": "/run/secrets/postgres_email_send_worker_password",
            "connect_timeout_seconds": 5,
        },
        "worker": {
            "worker_id": "local-pilot-email-send-worker",
            "lease_seconds": 30,
            "idle_delay_seconds": 0.1,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    config_path = tmp_path / "config.json"
    manifest_path.write_text(json.dumps(manifest))
    config_path.write_text(json.dumps(config))
    secret = tmp_path / "postgres_email_send_worker_password"
    secret.write_text("send-worker-password-1")
    os.chmod(secret, 0o600)
    secret_provider = MountedFileSecretProvider(
        tmp_path,
        (
            SecretSpec(
                "postgres_email_send_worker_password",
                "postgres_email_send_worker_password",
                "text",
                16,
                128,
            ),
        ),
    )
    connector_calls: list[dict[str, object]] = []
    runner_calls: list[tuple[object, object, float]] = []

    class Connection:
        def close(self) -> None:
            return None

    def connector(**kwargs: object) -> Connection:
        connector_calls.append(kwargs)
        return Connection()

    result = main(
        manifest_path=manifest_path,
        config_path=config_path,
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_EMAIL_SEND_KILL_SWITCH": "false",
            "GBOS_FAKE_EMAIL_SEND_ENABLED": "true",
            "GBOS_EXTERNAL_SEND_ENABLED": "false",
        },
        connector=connector,
        provider_factory=lambda: FakeEmailProvider(
            ProviderSubmissionResult(
                outcome=ProviderOutcome.ACCEPTED,
                safe_code="fake_accepted",
                provider_receipt_ref="fake-receipt",
            )
        ),
        authority_check=lambda _envelope: WorkerAuthorityState(
            emergency_stop_active=False,
            execution_enabled=True,
            command_unexpired=True,
            identity_active=True,
            mailbox_revision_current=True,
            route_authority_current=True,
        ),
        worker_runner=lambda worker, scope, delay: runner_calls.append((worker, scope, delay)),
        secret_provider=secret_provider,
    )

    assert result == 0
    assert connector_calls[0]["user"] == "gbos_email_send_worker"
    assert isinstance(runner_calls[0][0]._repository, PostgresEmailSendRepository)
    assert runner_calls[0][1].site_id == "gbos.localhost"


def test_explicit_fake_runtime_injects_dynamic_stop_reader_into_constructed_worker(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "production_go": False,
        "local_pilot_go": True,
        "local_pilot_status": "ready",
        "deepseek": {},
        "email_gateway": {"send_kill_switch": False, "external_send": False},
    }
    config = {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "enabled": True,
        "kill_switch": False,
        "external_send": False,
        "provider_mode": "fake",
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_send_worker",
            "password_file": "/run/secrets/postgres_email_send_worker_password",
            "connect_timeout_seconds": 5,
        },
        "worker": {
            "worker_id": "local-pilot-email-send-worker",
            "lease_seconds": 30,
            "idle_delay_seconds": 0.1,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    config_path = tmp_path / "config.json"
    manifest_path.write_text(json.dumps(manifest))
    config_path.write_text(json.dumps(config))
    secret = tmp_path / "postgres_email_send_worker_password"
    secret.write_text("send-worker-password-1")
    os.chmod(secret, 0o600)
    secret_provider = MountedFileSecretProvider(
        tmp_path,
        (
            SecretSpec(
                "postgres_email_send_worker_password",
                "postgres_email_send_worker_password",
                "text",
                16,
                128,
            ),
        ),
    )
    stop_reads: list[str] = []

    class Connection:
        def close(self) -> None:
            return None

    def inspect(worker: object, _scope: object, _delay: float) -> None:
        stop_reads.append(worker._runtime_stop_reader())  # type: ignore[attr-defined]

    assert (
        main(
            manifest_path=manifest_path,
            config_path=config_path,
            environ={
                "GBOS_LOCAL_RUNTIME_ENABLED": "true",
                "GBOS_EMAIL_SEND_KILL_SWITCH": "false",
                "GBOS_FAKE_EMAIL_SEND_ENABLED": "true",
                "GBOS_EXTERNAL_SEND_ENABLED": "false",
            },
            connector=lambda **_kwargs: Connection(),
            provider_factory=lambda: FakeEmailProvider(),
            authority_check=lambda _envelope: WorkerAuthorityState(
                emergency_stop_active=False,
                execution_enabled=True,
                command_unexpired=True,
                identity_active=True,
                mailbox_revision_current=True,
                route_authority_current=True,
            ),
            runtime_stop_reader=lambda: "emergency_stop_active",
            worker_runner=inspect,  # type: ignore[arg-type]
            secret_provider=secret_provider,
        )
        == 0
    )
    assert stop_reads == ["emergency_stop_active"]


def test_module_entrypoint_invokes_fail_closed_main() -> None:
    source = Path("services/local_pilot_runtime/email_send_worker.py").read_text()

    assert 'if __name__ == "__main__":' in source
    assert "raise SystemExit(main())" in source
