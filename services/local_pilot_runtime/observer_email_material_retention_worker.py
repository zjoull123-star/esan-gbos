"""Production-local Observer email-material retention deletion worker."""

from __future__ import annotations

import json
import os
import re
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Protocol

import httpx

from services.email_gateway.terminal_retention import (
    EmailMaterialTombstoneCallback,
    GatewayTombstoneCallbackReceipt,
)
from services.observer.observer.email_material_retention import (
    CasDelete,
    EmailMaterialRetentionDeletionRunner,
    EmailMaterialRetentionService,
)
from services.observer.observer.email_material_retention_callback import (
    PostgresEmailMaterialRetentionCallbackRepository,
)
from services.observer.observer.email_material_retention_repository import (
    PostgresEmailMaterialRetentionRepository,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import TenantScope

from .email_material_retention_relay import ObserverTombstoneCallbackRelay
from .runtime_support import (
    PostgresSettings,
    close_connection,
    load_secret_file,
    reject_plaintext_secret_environment,
)

DEFAULT_CONFIG = Path("/config/runtime-observer-email-material-retention.json")
DEFAULT_BEARER_FILE = Path("/run/secrets/email_gateway_retention_bearer")
DEFAULT_CAS_ROOT = Path("/var/lib/gbos/evidence")
DEFAULT_STOP_FILE = Path("/run/gbos/EMERGENCY_STOP")
AUTHORITY_ENDPOINT = (
    "http://email-gateway-retention-worker:9102/internal/v1/retention/"
    "email-material/authority/resolve"
)
CALLBACK_ENDPOINT = (
    "http://email-gateway-retention-worker:9102/internal/v1/retention/"
    "email-material/tombstone-callback"
)
AUTH_REF = "email-gateway-retention-v1"
_PURPOSE = "email_draft_material"
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_MAX_CONFIG_BYTES = 65_536
_MAX_RESPONSE_BYTES = 65_536


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class DeletionRunner(Protocol):
    def run_once(self, scope: TenantScope, *, batch_size: int) -> tuple[object, ...]: ...


class CallbackRelay(Protocol):
    def run_once(self, scope: TenantScope) -> bool: ...


CallbackTransport = Callable[..., tuple[int, Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class GatewayAPISettings:
    authority_endpoint: str
    callback_endpoint: str
    bearer_file: Path
    auth_ref: str


@dataclass(frozen=True, slots=True)
class RetentionWorkerSettings:
    worker_id: str
    batch_size: int
    interval_seconds: int


@dataclass(frozen=True, slots=True)
class ObserverEmailMaterialRetentionConfig:
    site_id: str
    postgres: PostgresSettings
    gateway_api: GatewayAPISettings
    worker: RetentionWorkerSettings
    schema_version: str = "1.0"
    external_send: bool = False


def load_observer_email_material_retention_config(
    path: Path,
) -> ObserverEmailMaterialRetentionConfig:
    config_path = Path(path)
    try:
        if (
            not config_path.is_file()
            or config_path.is_symlink()
            or config_path.stat().st_size > _MAX_CONFIG_BYTES
        ):
            raise ValueError("observer email material retention config rejected")
        value = json.loads(config_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("observer email material retention config rejected") from exc
    expected = {
        "schema_version",
        "site_id",
        "external_send",
        "postgres",
        "gateway_api",
        "worker",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != "1.0"
        or value.get("external_send") is not False
        or not isinstance(value.get("site_id"), str)
        or _SITE.fullmatch(value["site_id"]) is None
        or value.get("postgres")
        != {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_observer_app",
            "password_file": "/run/secrets/postgres_observer_password",
            "connect_timeout_seconds": 5,
        }
        or value.get("gateway_api")
        != {
            "authority_endpoint": AUTHORITY_ENDPOINT,
            "callback_endpoint": CALLBACK_ENDPOINT,
            "bearer_file": str(DEFAULT_BEARER_FILE),
            "auth_ref": AUTH_REF,
        }
        or value.get("worker")
        != {
            "worker_id": "observer-email-material-retention-worker",
            "batch_size": 100,
            "interval_seconds": 3600,
        }
    ):
        raise ValueError("observer email material retention config rejected")
    return ObserverEmailMaterialRetentionConfig(
        site_id=value["site_id"],
        postgres=PostgresSettings(
            host="postgres",
            port=5432,
            database="gbos_local_pilot",
            user="gbos_observer_app",
            password_file=Path("/run/secrets/postgres_observer_password"),
            connect_timeout_seconds=5,
        ),
        gateway_api=GatewayAPISettings(
            authority_endpoint=AUTHORITY_ENDPOINT,
            callback_endpoint=CALLBACK_ENDPOINT,
            bearer_file=DEFAULT_BEARER_FILE,
            auth_ref=AUTH_REF,
        ),
        worker=RetentionWorkerSettings(
            worker_id="observer-email-material-retention-worker",
            batch_size=100,
            interval_seconds=3600,
        ),
    )


class HttpGatewayTombstoneCallback:
    """Strict proxy-free transport for Gateway tombstone acknowledgement."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        auth_ref: str,
        transport: CallbackTransport | None = None,
    ) -> None:
        if endpoint != CALLBACK_ENDPOINT or not _valid_secret(bearer_token) or auth_ref != AUTH_REF:
            raise ValueError("Gateway tombstone callback configuration rejected")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._transport = transport or self._post

    def __repr__(self) -> str:
        return "HttpGatewayTombstoneCallback(credentials=<redacted>)"

    def deliver(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            callback = EmailMaterialTombstoneCallback.from_wire(payload)
        except Exception as exc:
            raise ValueError("Gateway tombstone callback request rejected") from exc
        status, response = self._transport(
            url=self._endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "X-GBOS-Local-Auth-Ref": self._auth_ref,
                "X-Processing-Purpose": _PURPOSE,
                "X-Request-ID": callback.observer_request_ref,
                "X-Site-ID": callback.site_id,
            },
            payload=payload,
            timeout_seconds=3.0,
        )
        expected_fields = {
            "schema_version",
            "site_id",
            "authority_receipt_ref",
            "tombstone_receipt_ref",
            "callback_receipt_ref",
            "accepted",
        }
        if (
            status != 200
            or not isinstance(response, Mapping)
            or set(response) != expected_fields
            or response.get("schema_version") != "1.0"
            or response.get("accepted") is not True
            or response.get("site_id") != callback.site_id
            or response.get("authority_receipt_ref") != callback.authority_receipt_ref
            or response.get("tombstone_receipt_ref") != callback.tombstone_receipt_ref
            or not isinstance(response.get("callback_receipt_ref"), str)
        ):
            raise ValueError("Gateway tombstone callback response rejected")
        callback_receipt_ref = response.get("callback_receipt_ref")
        if not isinstance(callback_receipt_ref, str):
            raise ValueError("Gateway tombstone callback response rejected")
        try:
            receipt = GatewayTombstoneCallbackReceipt(
                callback_receipt_ref=callback_receipt_ref,
                site_id=callback.site_id,
                authority_receipt_ref=callback.authority_receipt_ref,
                tombstone_receipt_ref=callback.tombstone_receipt_ref,
            )
        except Exception as exc:
            raise ValueError("Gateway tombstone callback response rejected") from exc
        exact = receipt.to_wire()
        if response != exact:
            raise ValueError("Gateway tombstone callback response rejected")
        return exact

    @staticmethod
    def _post(
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object]]:
        with httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.post(url, headers=headers, json=payload)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json" or len(response.content) > _MAX_RESPONSE_BYTES:
            return response.status_code, {}
        try:
            body = response.json()
        except ValueError, json.JSONDecodeError:
            return response.status_code, {}
        return response.status_code, body if isinstance(body, dict) else {}


class ObserverEmailMaterialRetentionCycle:
    """One bounded deletion-then-callback iteration."""

    def __init__(
        self,
        *,
        deletion_runner: DeletionRunner,
        callback_relay: CallbackRelay,
        site_id: str,
        batch_size: int,
        max_callbacks: int,
    ) -> None:
        if (
            _SITE.fullmatch(site_id) is None
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 100
            or isinstance(max_callbacks, bool)
            or not 1 <= max_callbacks <= 100
        ):
            raise ValueError("invalid Observer retention cycle configuration")
        self._deletion_runner = deletion_runner
        self._callback_relay = callback_relay
        self._scope = TenantScope(site_id, "observation_processing")
        self._batch_size = batch_size
        self._max_callbacks = max_callbacks

    def run_once(self) -> tuple[int, int]:
        deleted = self._deletion_runner.run_once(
            self._scope,
            batch_size=self._batch_size,
        )
        callback_count = 0
        while callback_count < self._max_callbacks:
            if not self._callback_relay.run_once(self._scope):
                break
            callback_count += 1
        return len(deleted), callback_count


def cycle_allowed(
    environ: Mapping[str, str],
    *,
    stop_file: Path = DEFAULT_STOP_FILE,
) -> bool:
    return (
        environ.get("GBOS_LOCAL_RUNTIME_ENABLED") == "true"
        and environ.get("GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_ENABLED") == "true"
        and environ.get("GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_KILL_SWITCH") == "false"
        and environ.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH") == "false"
        and environ.get("GBOS_GLOBAL_KILL_SWITCH") == "false"
        and not _latched(stop_file)
    )


def run_loop(
    *,
    run_cycle: Callable[[], tuple[int, int]],
    allowed: Callable[[], bool],
    interval_seconds: int,
    stop_event: StopSignal,
) -> int:
    if isinstance(interval_seconds, bool) or not 60 <= interval_seconds <= 86_400:
        raise ValueError("invalid Observer retention interval")
    failures = 0
    while not stop_event.is_set():
        if allowed():
            try:
                run_cycle()
            except Exception:
                failures += 1
        stop_event.wait(interval_seconds)
    return failures


def main(
    *,
    config_path: Path = DEFAULT_CONFIG,
    bearer_file: Path = DEFAULT_BEARER_FILE,
    cas_root: Path = DEFAULT_CAS_ROOT,
    stop_file: Path = DEFAULT_STOP_FILE,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    stop_event: StopSignal | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    cas_factory: Callable[[Path], CasDelete] = ContentAddressedEvidenceStore,
    callback_transport_factory: Callable[..., Any] = HttpGatewayTombstoneCallback,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        config = load_observer_email_material_retention_config(config_path)
        bearer = load_secret_file(bearer_file)
        postgres_password = load_secret_file(config.postgres.password_file)
        if not cycle_allowed(environment, stop_file=stop_file):
            return 78
        active_connector = connector
        if active_connector is None:
            import psycopg

            active_connector = psycopg.connect
        connection = active_connector(
            host=config.postgres.host,
            port=config.postgres.port,
            dbname=config.postgres.database,
            user=config.postgres.user,
            password=postgres_password.reveal(),
            connect_timeout=config.postgres.connect_timeout_seconds,
        )
        retention_repository = PostgresEmailMaterialRetentionRepository(connection)
        callback_repository = PostgresEmailMaterialRetentionCallbackRepository(connection)
        retention_repository.preflight()
        callback_repository.preflight()
        cas = cas_factory(cas_root)
        service = EmailMaterialRetentionService(
            repository=retention_repository,
            cas=cas,
            authoritative_registrar=None,
            worker_id=config.worker.worker_id,
            clock=clock,
        )
        deletion_runner = EmailMaterialRetentionDeletionRunner(
            service=service,
            max_batch_size=config.worker.batch_size,
        )
        callback_transport = callback_transport_factory(
            endpoint=config.gateway_api.callback_endpoint,
            bearer_token=bearer.reveal(),
            auth_ref=config.gateway_api.auth_ref,
        )
        callback_relay = ObserverTombstoneCallbackRelay(
            repository=callback_repository,
            transport=callback_transport,
            worker_id=config.worker.worker_id,
            clock=clock,
        )
        cycle = ObserverEmailMaterialRetentionCycle(
            deletion_runner=deletion_runner,
            callback_relay=callback_relay,
            site_id=config.site_id,
            batch_size=config.worker.batch_size,
            max_callbacks=100,
        )
        if stop_event is None:
            created_event = Event()
            _install_signals(created_event)
            active_stop: StopSignal = created_event
        else:
            active_stop = stop_event
        failures = run_loop(
            run_cycle=cycle.run_once,
            allowed=lambda: cycle_allowed(environment, stop_file=stop_file),
            interval_seconds=config.worker.interval_seconds,
            stop_event=active_stop,
        )
        return 0 if failures == 0 else 78
    except Exception:
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _valid_secret(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 4_096
        and value == value.strip()
        and all(char not in value for char in "\x00\r\n")
    )


def _latched(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return True


def _install_signals(event: Event) -> None:
    def stop(_signum: int, _frame: object) -> None:
        event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTHORITY_ENDPOINT",
    "AUTH_REF",
    "CALLBACK_ENDPOINT",
    "HttpGatewayTombstoneCallback",
    "ObserverEmailMaterialRetentionConfig",
    "ObserverEmailMaterialRetentionCycle",
    "cycle_allowed",
    "load_observer_email_material_retention_config",
    "main",
    "run_loop",
]
