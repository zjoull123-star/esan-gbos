"""Default-off fake-only Send Outbox runtime composition."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import ApprovedOutboundEnvelope
from services.email_gateway.provider import EmailProvider
from services.email_gateway.send_outbox import PostgresEmailSendRepository
from services.email_gateway.worker import EmailSendWorker, WorkerAuthorityState

from .runtime_support import (
    PostgresSettings,
    RuntimeSupportError,
    TextSecretProvider,
    close_connection,
    connect_postgres,
    reject_plaintext_secret_environment,
)
from .secret_provider import MountedFileSecretProvider, SecretSpec

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_CONFIG = Path("/config/runtime-email-send-worker.json")
_MAX_CONFIG_BYTES = 65_536

WorkerRunner = Callable[[EmailSendWorker, TenantScope, float], None]


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    config_path: Path = DEFAULT_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    provider_factory: Callable[[], EmailProvider] | None = None,
    authority_check: Callable[[ApprovedOutboundEnvelope], WorkerAuthorityState] | None = None,
    worker_runner: WorkerRunner | None = None,
    secret_provider: TextSecretProvider | None = None,
    runtime_stop_reader: Callable[[], str | None] | None = None,
    reconciliation_refs: tuple[str, ...] = (),
) -> int:
    """Compose only an explicitly enabled local fake worker; never a real adapter."""

    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(manifest, component="email-send-worker", environ=environment)
        gateway = manifest.get("email_gateway")
        if (
            environment.get("GBOS_EMAIL_SEND_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_FAKE_EMAIL_SEND_ENABLED", "false") != "true"
            or not isinstance(gateway, Mapping)
            or gateway.get("send_kill_switch") is not False
            or gateway.get("external_send") is not False
        ):
            raise LocalEntrypointDisabled("fake email send worker is disabled")
        if connector is None or provider_factory is None or authority_check is None:
            raise LocalEntrypointDisabled("fake email send dependencies are absent")
        config = _load_config(config_path)
        if (
            config["site_id"] != manifest.get("site_id")
            or config["enabled"] is not True
            or config["kill_switch"] is not False
            or config["external_send"] is not False
            or config["provider_mode"] != "fake"
        ):
            raise LocalEntrypointDisabled("fake email send config is closed")
        postgres = cast(dict[str, Any], config["postgres"])
        worker_config = cast(dict[str, Any], config["worker"])
        settings = PostgresSettings(
            host=str(postgres["host"]),
            port=int(postgres["port"]),
            database=str(postgres["database"]),
            user=str(postgres["user"]),
            password_file=Path(str(postgres["password_file"])),
            connect_timeout_seconds=int(postgres["connect_timeout_seconds"]),
        )
        if settings.user != "gbos_email_send_worker":
            raise LocalEntrypointDisabled("fake email send database role is invalid")
        active_provider = secret_provider or _secret_provider()
        connection = connect_postgres(
            settings,
            connector=connector,
            secret_provider=active_provider,
            environ=environment,
        )
        repository = PostgresEmailSendRepository(
            connection,  # type: ignore[arg-type]
            actual_database_role="gbos_email_send_worker",
        )
        worker = EmailSendWorker(
            repository=repository,
            provider=provider_factory(),
            worker_id=str(worker_config["worker_id"]),
            clock=lambda: datetime.now(UTC),
            authority_check=authority_check,
            lease_duration=timedelta(seconds=int(worker_config["lease_seconds"])),
            runtime_stop_reader=runtime_stop_reader or (lambda: _runtime_stop(environment)),
        )
        scope = TenantScope(str(config["site_id"]), "customer_service")
        worker.consume_manual_reconciliations(scope, reconciliation_refs)
        (worker_runner or _run_loop)(
            worker,
            scope,
            float(worker_config["idle_delay_seconds"]),
        )
        return 0
    except LocalEntrypointDisabled, RuntimeSupportError, ValueError, OSError:
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _load_config(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_CONFIG_BYTES:
        raise LocalEntrypointDisabled("fake email send config is unavailable")
    value = json.loads(path.read_bytes())
    required = {
        "schema_version",
        "site_id",
        "enabled",
        "kill_switch",
        "external_send",
        "provider_mode",
        "postgres",
        "worker",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != "1.0":
        raise LocalEntrypointDisabled("fake email send config is invalid")
    if not isinstance(value["postgres"], dict) or not isinstance(value["worker"], dict):
        raise LocalEntrypointDisabled("fake email send config is invalid")
    return value


def _run_loop(worker: EmailSendWorker, scope: TenantScope, idle_delay: float) -> None:
    result = worker.run_once(scope)
    if result.state == "idle":
        time.sleep(idle_delay)


def _runtime_stop(environment: Mapping[str, str]) -> str | None:
    if environment.get("GBOS_EMAIL_SEND_KILL_SWITCH", "true") != "false":
        return "emergency_stop_active"
    if environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false":
        return "external_send_disabled"
    return None


def _secret_provider() -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        Path("/run/secrets"),
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
