"""Default-off, credential-free Email Gateway draft-reference retention scheduler."""

from __future__ import annotations

import hmac
import json
import os
import re
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
from services.email_gateway.repositories.terminal_retention import (
    PostgresTerminalRetentionRepository,
)
from services.email_gateway.retention import RetentionScheduler
from services.email_gateway.terminal_retention import (
    EMAIL_MATERIAL_PURPOSE,
    EmailMaterialTerminalRetentionService,
    EmailMaterialTombstoneCallback,
    GatewayTombstoneCallbackReceipt,
    TerminalMaterialAuthority,
)

from .email_material_retention_relay import GatewayAuthorityRegistrationRelay
from .runtime_support import close_connection, load_secret_file, reject_plaintext_secret_environment

DEFAULT_CONFIG = Path("/config/email-gateway-retention-runtime.json")
DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_STOP_FILE = Path("/run/gbos/EMERGENCY_STOP")
_PURPOSE = "audit_compliance"
_MAX_JSON_BYTES = 8192
_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")
_RESOLVE_PATH = "/internal/v1/retention/email-material/authority/resolve"
_CALLBACK_PATH = "/internal/v1/retention/email-material/tombstone-callback"
_REGISTER_ENDPOINT = "http://observer-api:8003/internal/v1/retention/email-material/register"


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


VerifierTransport = Callable[..., tuple[int, Mapping[str, object]]]
RegistrationTransport = Callable[..., tuple[int, Mapping[str, object]]]


class RegistrationRelay(Protocol):
    def run_once(self, scope: TenantScope) -> bool: ...


class TerminalRetentionService(Protocol):
    def resolve_terminal(
        self, scope: TenantScope, authority_receipt_ref: str
    ) -> TerminalMaterialAuthority: ...

    def accept_tombstone_callback(
        self, scope: TenantScope, *, payload: object
    ) -> GatewayTombstoneCallbackReceipt: ...


class HttpObserverEmailMaterialRegistration:
    """Fixed, bounded local-only transport for Observer authority registration."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        auth_ref: str,
        transport: RegistrationTransport | None = None,
    ) -> None:
        if (
            endpoint != _REGISTER_ENDPOINT
            or not bearer_token
            or bearer_token != bearer_token.strip()
            or auth_ref != "observer-retention-verifier-v1"
        ):
            raise ValueError("Observer registration configuration rejected")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._transport = transport or self._post

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        fields = {"schema_version", "site_id", "authority_receipt_ref"}
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or payload.get("schema_version") != "1.0"
            or not isinstance(payload.get("site_id"), str)
            or not isinstance(payload.get("authority_receipt_ref"), str)
            or _REF.fullmatch(str(payload["authority_receipt_ref"])) is None
        ):
            raise ValueError("invalid Observer registration request")
        status, response = self._transport(
            url=self._endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "X-GBOS-Local-Auth-Ref": self._auth_ref,
                "X-Processing-Purpose": _PURPOSE,
                "X-Request-ID": str(payload["authority_receipt_ref"]),
                "X-Site-ID": str(payload["site_id"]),
            },
            payload=payload,
            timeout_seconds=3.0,
        )
        response_fields = {
            "schema_version",
            "site_id",
            "evidence_ref",
            "request_ref",
            "not_before",
        }
        if status != 200 or not isinstance(response, Mapping) or set(response) != response_fields:
            raise ValueError("invalid Observer registration response")
        return dict(response)

    @staticmethod
    def _post(
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> tuple[int, Mapping[str, object]]:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_JSON_BYTES:
            raise ValueError("Observer registration request is unbounded")
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream("POST", url, headers=headers, content=encoded) as response,
            ):
                if 300 <= response.status_code < 400:
                    raise ValueError("Observer registration redirect rejected")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_JSON_BYTES:
                        raise ValueError("Observer registration response is unbounded")
                body = json.loads(bytes(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid Observer registration response") from error
        if not isinstance(body, dict):
            raise ValueError("invalid Observer registration response")
        return int(response.status_code), body

    def __repr__(self) -> str:
        return "HttpObserverEmailMaterialRegistration(credentials=<redacted>)"


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
            endpoint != "http://observer-api:8003/internal/v1/retention/tombstones/verify"
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
        except ValueError, json.JSONDecodeError:
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
        registration_relay: RegistrationRelay | None = None,
    ) -> None:
        self._repository = repository
        self._verifier = verifier
        self._metrics = metrics
        self._scope = TenantScope(site_id, _PURPOSE)
        self._terminal_scope = TenantScope(site_id, _PURPOSE)
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._execute = execute
        self._clock = clock
        self._registration_relay = registration_relay

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
        if self._registration_relay is not None:
            try:
                for _ in range(self._batch_size):
                    if not self._registration_relay.run_once(self._terminal_scope):
                        break
            except Exception:
                return 78
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
        terminal_service: TerminalRetentionService | None = None,
        site_id: str | None = None,
        bearer_token: str | None = None,
        auth_ref: str | None = None,
    ) -> None:
        api_values = (terminal_service, site_id, bearer_token, auth_ref)
        api_enabled = all(value is not None for value in api_values)
        if any(value is not None for value in api_values) and not api_enabled:
            raise ValueError("retention API dependencies must be complete")
        if api_enabled and (
            auth_ref != "email-gateway-retention-v1"
            or not isinstance(site_id, str)
            or not site_id
            or not isinstance(bearer_token, str)
            or not bearer_token
            or bearer_token != bearer_token.strip()
        ):
            raise ValueError("retention API dependencies rejected")

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

            def do_POST(self) -> None:  # noqa: N802
                if self.path not in {_RESOLVE_PATH, _CALLBACK_PATH}:
                    self._send_json(404, {"error": "not_found"})
                    return
                if not api_enabled or not self._authenticated():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                payload = self._read_json()
                if payload is None:
                    self._send_json(400, {"error": "invalid_request"})
                    return
                try:
                    if self.path == _RESOLVE_PATH:
                        self._resolve(payload)
                    else:
                        self._callback(payload)
                except Exception:
                    self._send_json(503, {"error": "unavailable"})

            def _authenticated(self) -> bool:
                authorization = self.headers.get("Authorization")
                local_auth_ref = self.headers.get("X-GBOS-Local-Auth-Ref")
                header_site = self.headers.get("X-Site-ID")
                purpose = self.headers.get("X-Processing-Purpose")
                request_id = self.headers.get("X-Request-ID")
                return (
                    isinstance(authorization, str)
                    and isinstance(bearer_token, str)
                    and hmac.compare_digest(authorization, f"Bearer {bearer_token}")
                    and isinstance(local_auth_ref, str)
                    and isinstance(auth_ref, str)
                    and hmac.compare_digest(local_auth_ref, auth_ref)
                    and header_site == site_id
                    and purpose == EMAIL_MATERIAL_PURPOSE
                    and isinstance(request_id, str)
                    and _REF.fullmatch(request_id) is not None
                )

            def _read_json(self) -> dict[str, object] | None:
                if self.headers.get("Content-Type") != "application/json":
                    return None
                content_length = self.headers.get("Content-Length")
                if (
                    not isinstance(content_length, str)
                    or not content_length.isascii()
                    or not content_length.isdigit()
                ):
                    return None
                length = int(content_length)
                if not 1 <= length <= _MAX_JSON_BYTES:
                    return None
                try:
                    raw = self.rfile.read(length)
                    value = json.loads(raw)
                except UnicodeDecodeError, json.JSONDecodeError, OSError:
                    return None
                return value if isinstance(value, dict) else None

            def _resolve(self, payload: dict[str, object]) -> None:
                fields = {
                    "schema_version",
                    "site_id",
                    "authority_receipt_ref",
                    "request_id",
                }
                authority_receipt_ref = payload.get("authority_receipt_ref")
                if (
                    set(payload) != fields
                    or payload.get("schema_version") != "1.0"
                    or payload.get("site_id") != site_id
                    or payload.get("request_id") != self.headers.get("X-Request-ID")
                    or not isinstance(authority_receipt_ref, str)
                    or _REF.fullmatch(authority_receipt_ref) is None
                ):
                    self._send_json(400, {"error": "invalid_request"})
                    return
                assert terminal_service is not None
                assert site_id is not None
                authority = terminal_service.resolve_terminal(
                    TenantScope(site_id, _PURPOSE),
                    authority_receipt_ref,
                )
                if (
                    getattr(authority, "authority_receipt_ref", None) != authority_receipt_ref
                    or getattr(authority, "site_id", None) != site_id
                    or getattr(authority, "purpose", None) != EMAIL_MATERIAL_PURPOSE
                ):
                    raise ValueError("terminal authority response conflict")
                terminal_at = authority.terminal_at
                if (
                    not isinstance(terminal_at, datetime)
                    or terminal_at.tzinfo is None
                    or terminal_at.utcoffset() is None
                ):
                    raise ValueError("terminal authority response conflict")
                self._send_json(
                    200,
                    {
                        "schema_version": "1.0",
                        "authority_receipt_ref": authority_receipt_ref,
                        "site_id": site_id,
                        "purpose": EMAIL_MATERIAL_PURPOSE,
                        "evidence_ref": authority.evidence_ref,
                        "terminal_state": authority.terminal_state,
                        "terminal_at": terminal_at.astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "draft_ref": authority.draft_ref,
                        "draft_revision": authority.draft_revision,
                    },
                )

            def _callback(self, payload: dict[str, object]) -> None:
                try:
                    callback = EmailMaterialTombstoneCallback.from_wire(payload)
                except Exception:
                    self._send_json(400, {"error": "invalid_request"})
                    return
                if (
                    callback.site_id != site_id
                    or callback.purpose != EMAIL_MATERIAL_PURPOSE
                    or callback.observer_request_ref != self.headers.get("X-Request-ID")
                ):
                    self._send_json(400, {"error": "invalid_request"})
                    return
                assert terminal_service is not None
                assert site_id is not None
                receipt = terminal_service.accept_tombstone_callback(
                    TenantScope(site_id, _PURPOSE),
                    payload=payload,
                )
                response = receipt.to_wire()
                self._send_json(200, response)

            def _send(self, status: int, body: str) -> None:
                encoded = body.encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _send_json(self, status: int, body: Mapping[str, object]) -> None:
                encoded = json.dumps(
                    body,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

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
        gateway_api_token = load_secret_file(Path(config["gateway_retention_api"]["bearer_file"]))
        observer_token = load_secret_file(Path(config["observer_registration"]["bearer_file"]))
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
        repository = PostgresRetentionRepository(connection)  # type: ignore[arg-type]
        terminal_repository = PostgresTerminalRetentionRepository(
            connection,  # type: ignore[arg-type]
            actual_database_role="gbos_email_gateway_retention_worker",
        )
        terminal_repository.preflight()
        metrics = GatewayMetrics(required_workers=frozenset(config["required_workers"]))
        verifier = HttpObserverTombstoneVerifier(
            endpoint=config["observer_verifier"]["endpoint"],
            bearer_token=observer_token.reveal(),
            auth_ref=config["observer_verifier"]["auth_ref"],
        )
        terminal_service = EmailMaterialTerminalRetentionService(
            repository=terminal_repository,
            clock=clock,
        )
        registration_transport = HttpObserverEmailMaterialRegistration(
            endpoint=config["observer_registration"]["endpoint"],
            bearer_token=observer_token.reveal(),
            auth_ref=config["observer_registration"]["auth_ref"],
        )
        registration_relay = GatewayAuthorityRegistrationRelay(
            service=terminal_service,
            transport=registration_transport,
            worker_id=config["worker_id"],
            clock=clock,
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
            registration_relay=registration_relay,
        )
        if stop_event is None:
            created_event = Event()
            _install_signals(created_event)
            event: StopSignal = created_event
        else:
            event = stop_event
        server = MetricsServer(
            metrics,
            port=config["metrics_port"],
            clock=clock,
            terminal_service=terminal_service,
            site_id=config["site_id"],
            bearer_token=gateway_api_token.reveal(),
            auth_ref=config["gateway_retention_api"]["auth_ref"],
        )
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
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 65_536:
        raise ValueError("retention runtime config rejected")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version",
        "site_id",
        "external_send",
        "postgres",
        "observer_verifier",
        "gateway_retention_api",
        "observer_registration",
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
        or value.get("gateway_retention_api")
        != {
            "bearer_file": "/run/secrets/email_gateway_retention_bearer",
            "auth_ref": "email-gateway-retention-v1",
        }
        or value.get("observer_registration")
        != {
            "endpoint": _REGISTER_ENDPOINT,
            "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
            "auth_ref": "observer-retention-verifier-v1",
        }
        or value.get("required_workers") != ["retention"]
        or not isinstance(value.get("site_id"), str)
        or not value["site_id"]
        or len(value["site_id"]) > 140
        or not isinstance(value.get("worker_id"), str)
        or not value["worker_id"]
        or len(value["worker_id"]) > 256
        or "@" in value["worker_id"]
        or not isinstance(value.get("batch_size"), int)
        or not 1 <= value["batch_size"] <= 100
        or not isinstance(value.get("interval_seconds"), int)
        or not 60 <= value["interval_seconds"] <= 86_400
        or value.get("metrics_port") != 9102
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
