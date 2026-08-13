"""Default-off, credential-free Email Gateway draft-reference retention scheduler."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any, Protocol

import httpx

from services.email_gateway.metrics import GatewayMetrics
from services.email_gateway.models import (
    ContentProjection,
    TenantScope,
    canonical_digest,
    stable_ref,
)
from services.email_gateway.repositories.retention_runtime import PostgresRetentionRepository
from services.email_gateway.retention import RetentionScheduler

from .runtime_support import close_connection, load_secret_file, reject_plaintext_secret_environment

DEFAULT_CONFIG = Path("/config/email-gateway-retention-runtime.json")
DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_STOP_FILE = Path("/run/gbos/EMERGENCY_STOP")
_PURPOSE = "audit_compliance"


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


VerifierTransport = Callable[..., tuple[int, Mapping[str, object]]]


class HttpObserverTombstoneVerifier:
    """Authenticated local-only verifier; it never asks Observer to delete anything."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        auth_ref: str,
        transport: VerifierTransport | None = None,
    ) -> None:
        if (
            endpoint
            != "http://observer-api:8003/internal/v1/retention/tombstones/verify"
            or not bearer_token
            or bearer_token != bearer_token.strip()
            or auth_ref != "observer-retention-verifier-v1"
        ):
            raise ValueError("Observer retention verifier configuration rejected")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._transport = transport or self._post

    def verify_tombstone(
        self,
        scope: TenantScope,
        projection: ContentProjection,
        *,
        now: datetime,
    ) -> bool:
        receipt_ref = projection.observer_expiration_receipt_ref
        if receipt_ref is None:
            return False
        try:
            status, response = self._transport(
                url=self._endpoint,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._bearer_token}",
                    "Content-Type": "application/json",
                    "X-GBOS-Local-Auth-Ref": self._auth_ref,
                    "X-Processing-Purpose": "audit_compliance",
                    "X-Site-ID": scope.site_id,
                },
                payload={
                    "schema_version": "1.0",
                    "site_id": scope.site_id,
                    "evidence_ref": projection.evidence_ref,
                    "tombstone_receipt_ref": receipt_ref,
                    "checked_at": now.astimezone(UTC).isoformat(),
                },
                timeout_seconds=3.0,
            )
        except Exception:
            return False
        return status == 200 and response == {
            "schema_version": "1.0",
            "site_id": scope.site_id,
            "evidence_ref": projection.evidence_ref,
            "tombstone_receipt_ref": receipt_ref,
            "verified": True,
        }

    @staticmethod
    def _post(
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object]]:
        with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
            response = client.post(url, headers=headers, json=payload)
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return response.status_code, {}
        return response.status_code, body if isinstance(body, dict) else {}


def execution_enabled(environ: Mapping[str, str]) -> bool:
    return (
        environ.get("GBOS_EMAIL_GATEWAY_RETENTION_ENABLED") == "true"
        and environ.get("GBOS_EMAIL_GATEWAY_RETENTION_EXECUTE_ACKNOWLEDGED") == "true"
        and environ.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH") == "false"
        and environ.get("GBOS_GLOBAL_KILL_SWITCH") == "false"
    )


def cycle_allowed(environ: Mapping[str, str], *, stop_file: Path = DEFAULT_STOP_FILE) -> bool:
    return (
        environ.get("GBOS_LOCAL_RUNTIME_ENABLED") == "true"
        and environ.get("GBOS_EMAIL_GATEWAY_RETENTION_SCHEDULER_ENABLED") == "true"
        and environ.get("GBOS_EMAIL_GATEWAY_RETENTION_SCHEDULER_KILL_SWITCH") == "false"
        and environ.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH") == "false"
        and environ.get("GBOS_GLOBAL_KILL_SWITCH") == "false"
        and not _latched(stop_file)
    )


def run_loop(
    *,
    run_cycle: Callable[[], int],
    cycle_allowed: Callable[[], bool],
    interval_seconds: int,
    stop_event: StopSignal,
) -> int:
    failures = 0
    while not stop_event.is_set():
        if cycle_allowed():
            try:
                status = run_cycle()
            except Exception:
                status = 78
            if status != 0:
                failures += 1
        stop_event.wait(interval_seconds)
    return failures


class RetentionCycle:
    def __init__(
        self,
        *,
        repository: PostgresRetentionRepository,
        verifier: HttpObserverTombstoneVerifier,
        metrics: GatewayMetrics,
        site_id: str,
        worker_id: str,
        batch_size: int,
        execute: Callable[[], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._verifier = verifier
        self._metrics = metrics
        self._scope = TenantScope(site_id, _PURPOSE)
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._execute = execute
        self._clock = clock

    def __call__(self) -> int:
        now = self._clock()
        self._repository.record_worker_heartbeat(
            self._scope,
            worker_kind="retention",
            at=now,
        )
        projections = self._repository.discover_due_projections(
            self._scope,
            now=now,
            limit=self._batch_size,
        )
        backlog, failures = self._repository.retention_health(self._scope, now=now)
        self._metrics.set_gauge("gbos_email_gateway_retention_backlog", backlog, labels={})
        self._metrics.set_gauge("gbos_email_gateway_retention_failures", failures, labels={})
        for kind, heartbeat in self._repository.heartbeat_snapshot(self._scope).items():
            self._metrics.record_persisted_heartbeat(kind, at=heartbeat)
        if not projections:
            return 0
        projection_digest = canonical_digest(
            tuple((item.projection_ref, item.payload_digest) for item in projections)
        )
        run_ref = stable_ref("RTR", self._scope.site_id, projection_digest)
        scheduler = RetentionScheduler(
            self._repository,
            emergency_stop=lambda: False,
            observer_tombstone_verifier=self._verifier,
            metrics=self._metrics,
        )
        scheduler.schedule(
            self._scope,
            run_ref=run_ref,
            idempotency_key=f"retention-{projection_digest.removeprefix('sha256:')}",
            projections=projections,
            dry_run=not self._execute(),
            now=now,
        )
        result = scheduler.run_once(
            self._scope,
            worker_id=self._worker_id,
            now=now,
            limit=self._batch_size,
        )
        return 0 if result is not None and result.status == "completed" else 78


class MetricsServer:
    def __init__(
        self,
        metrics: GatewayMetrics,
        *,
        port: int,
        clock: Callable[[], datetime],
    ) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/metrics":
                    self._send(200, metrics.render_prometheus(now=clock()))
                    return
                if self.path == "/readyz":
                    ready = metrics.readiness(now=clock()).ready
                    self._send(200 if ready else 503, "ready\n" if ready else "not ready\n")
                    return
                if self.path == "/healthz":
                    self._send(200, "ok\n")
                    return
                self._send(404, "not found\n")

            def _send(self, status: int, body: str) -> None:
                encoded = body.encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def main(
    *,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    stop_event: StopSignal | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    server: MetricsServer | None = None
    try:
        reject_plaintext_secret_environment(environment)
        config = _load_config(
            Path(environment.get("GBOS_EMAIL_GATEWAY_RETENTION_CONFIG", DEFAULT_CONFIG))
        )
        manifest = _load_manifest(
            Path(environment.get("GBOS_LOCAL_PILOT_MANIFEST", DEFAULT_MANIFEST))
        )
        if manifest.get("site_id") != config["site_id"] or manifest.get("retention_days") != 30:
            return 78
        password = load_secret_file(Path(config["postgres"]["password_file"]))
        active_connector = connector
        if active_connector is None:
            import psycopg

            active_connector = psycopg.connect
        connection = active_connector(
            host="postgres",
            port=5432,
            dbname="gbos_local_pilot",
            user="gbos_email_gateway_retention_worker",
            password=password.reveal(),
            connect_timeout=5,
        )
        verifier_token = load_secret_file(Path(config["observer_verifier"]["bearer_file"]))
        repository = PostgresRetentionRepository(connection)  # type: ignore[arg-type]
        metrics = GatewayMetrics(required_workers=frozenset(config["required_workers"]))
        verifier = HttpObserverTombstoneVerifier(
            endpoint=config["observer_verifier"]["endpoint"],
            bearer_token=verifier_token.reveal(),
            auth_ref=config["observer_verifier"]["auth_ref"],
        )
        cycle = RetentionCycle(
            repository=repository,
            verifier=verifier,
            metrics=metrics,
            site_id=config["site_id"],
            worker_id=config["worker_id"],
            batch_size=config["batch_size"],
            execute=lambda: execution_enabled(environment),
            clock=clock,
        )
        if stop_event is None:
            created_event = Event()
            _install_signals(created_event)
            event: StopSignal = created_event
        else:
            event = stop_event
        server = MetricsServer(metrics, port=config["metrics_port"], clock=clock)
        server.start()
        run_loop(
            run_cycle=cycle,
            cycle_allowed=lambda: cycle_allowed(environment),
            interval_seconds=config["interval_seconds"],
            stop_event=event,
        )
        return 0
    except Exception:
        return 78
    finally:
        if server is not None:
            server.close()
        if connection is not None:
            close_connection(connection)


def _load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "site_id",
        "external_send",
        "postgres",
        "observer_verifier",
        "worker_id",
        "batch_size",
        "interval_seconds",
        "metrics_port",
        "required_workers",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema_version") != "1.0"
        or value.get("external_send") is not False
        or value.get("postgres")
        != {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_gateway_retention_worker",
            "password_file": "/run/secrets/postgres_email_gateway_retention_worker_password",
            "connect_timeout_seconds": 5,
        }
        or value.get("observer_verifier")
        != {
            "endpoint": "http://observer-api:8003/internal/v1/retention/tombstones/verify",
            "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
            "auth_ref": "observer-retention-verifier-v1",
        }
        or value.get("required_workers") != ["retention"]
        or not isinstance(value.get("site_id"), str)
        or not isinstance(value.get("worker_id"), str)
        or not isinstance(value.get("batch_size"), int)
        or not 1 <= value["batch_size"] <= 100
        or not isinstance(value.get("interval_seconds"), int)
        or not 60 <= value["interval_seconds"] <= 86_400
        or not isinstance(value.get("metrics_port"), int)
        or not 1024 <= value["metrics_port"] <= 65_535
    ):
        raise ValueError("retention runtime config rejected")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "1.0"
        or value.get("mode") != "local_pilot"
        or value.get("production_go") is not False
        or value.get("local_pilot_go") is not True
    ):
        raise ValueError("retention manifest rejected")
    return value


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
    "HttpObserverTombstoneVerifier",
    "RetentionCycle",
    "cycle_allowed",
    "execution_enabled",
    "main",
    "run_loop",
]
