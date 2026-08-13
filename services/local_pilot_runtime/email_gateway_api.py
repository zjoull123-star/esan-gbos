"""Import-safe Email Gateway API composition."""

from __future__ import annotations

import inspect
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeGuard
from urllib import error as urlerror
from urllib import request as urlrequest

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.email_gateway.api import create_email_gateway_app
from services.email_gateway.intake import GatewayIntakeService
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.phase1_read import ConnectorHealth, Phase1Mailbox
from services.email_gateway.repositories.intake import PostgresIntakeRepository
from services.email_gateway.repositories.mailboxes import PostgresMailboxRepository
from services.email_gateway.repositories.phase1_read import PostgresPhase1ReadRepository

from .email_gateway_config import (
    EmailGatewayConfigError,
    EmailGatewayRuntimeConfig,
    load_email_gateway_config,
    require_gateway_component,
)
from .runtime_support import (
    RuntimeSupportError,
    TextSecretProvider,
    close_connection,
    connect_postgres,
    load_secret_file,
    reject_plaintext_secret_environment,
)
from .secret_provider import MountedFileSecretProvider, SecretSpec
from .server import ServerBindingError, run_server, validate_server_binding

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_CONFIG = Path("/config/email-gateway-runtime.json")
DEFAULT_EMERGENCY_STOP = Path("/run/gbos/EMERGENCY_STOP")
OBSERVER_CONNECTOR_HEALTH_URL = "http://observer-api:8003/internal/v1/email-connectors/health"
_MAX_HEALTH_RESPONSE_BYTES = 65_536
_HEALTH_TIMEOUT_SECONDS = 3.0


ApplicationFactory = Callable[..., FastAPI]


class _HttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int) -> bytes: ...

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, *args: object) -> object: ...


class _HttpOpener(Protocol):
    def open(self, request: object, *, timeout: float) -> _HttpResponse: ...


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class ObserverConnectorHealthClient:
    """Bounded, proxy-free reader for Observer's safe connector-health projection."""

    def __init__(
        self,
        *,
        bearer_token: str,
        auth_ref: str,
        opener: _HttpOpener | None = None,
    ) -> None:
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or len(bearer_token) > 4096
            or auth_ref != "gateway-mailbox-projection-v1"
        ):
            raise EmailGatewayConfigError("Observer connector health credentials are invalid")
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._opener = opener or urlrequest.build_opener(
            urlrequest.ProxyHandler({}),
            _RejectRedirects(),
        )

    def fetch(self, *, site_id: str, request_id: str) -> tuple[Mapping[str, object], ...]:
        if not _bounded_header(site_id, 140) or not _bounded_header(request_id, 256):
            raise RuntimeSupportError("Observer connector health request rejected")
        request = urlrequest.Request(
            OBSERVER_CONNECTOR_HEALTH_URL,
            data=b"{}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "X-GBOS-Local-Auth-Ref": self._auth_ref,
                "X-Site-ID": site_id,
                "X-Processing-Purpose": "email_connector_health_read",
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=_HEALTH_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                body = response.read(_MAX_HEALTH_RESPONSE_BYTES + 1)
                if (
                    response.status != 200
                    or content_type != "application/json"
                    or len(body) > _MAX_HEALTH_RESPONSE_BYTES
                ):
                    raise RuntimeSupportError("Observer connector health request rejected")
        except RuntimeSupportError:
            raise
        except (OSError, TimeoutError, urlerror.URLError, ValueError) as exc:
            raise RuntimeSupportError("Observer connector health request rejected") from exc
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeSupportError("Observer connector health response rejected") from exc
        if not isinstance(payload, dict) or set(payload) != {"site_id", "data", "meta"}:
            raise RuntimeSupportError("Observer connector health response rejected")
        data = payload.get("data")
        meta = payload.get("meta")
        if (
            payload.get("site_id") != site_id
            or not isinstance(data, dict)
            or set(data) != {"connectors"}
            or not isinstance(meta, dict)
            or set(meta) != {"request_id", "schema_version"}
            or meta.get("request_id") != request_id
            or meta.get("schema_version") != "1.0"
        ):
            raise RuntimeSupportError("Observer connector health response rejected")
        connectors = data.get("connectors")
        if (
            not isinstance(connectors, list)
            or len(connectors) > 1000
            or any(not isinstance(item, dict) for item in connectors)
        ):
            raise RuntimeSupportError("Observer connector health response rejected")
        return tuple(connectors)


class ObserverConnectorHealthReader:
    """Phase 1 adapter that refuses health reads until connector identity is projected."""

    def __init__(self, client: ObserverConnectorHealthClient) -> None:
        self._client = client

    def read(
        self,
        site_id: str,
        mailboxes: tuple[Phase1Mailbox, ...],
    ) -> tuple[ConnectorHealth, ...]:
        connector_refs: dict[str, Phase1Mailbox] = {}
        for mailbox in mailboxes:
            connector_ref = getattr(mailbox, "observer_connector_instance_ref", None)
            if not _bounded_header(connector_ref, 140) or connector_ref in connector_refs:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "connector_health_unavailable"},
                )
            connector_refs[connector_ref] = mailbox
        request_id = f"email-health-{secrets.token_hex(16)}"
        try:
            rows = self._client.fetch(site_id=site_id, request_id=request_id)
        except RuntimeSupportError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "connector_health_unavailable"},
            ) from exc
        by_ref: dict[str, Mapping[str, object]] = {}
        for row in rows:
            connector_ref = row.get("observer_connector_instance_ref")
            if (
                not _bounded_header(connector_ref, 140)
                or connector_ref not in connector_refs
                or connector_ref in by_ref
            ):
                raise HTTPException(
                    status_code=503,
                    detail={"code": "connector_health_unavailable"},
                )
            by_ref[connector_ref] = row
        if set(by_ref) != set(connector_refs):
            raise HTTPException(
                status_code=503,
                detail={"code": "connector_health_unavailable"},
            )
        return tuple(
            _connector_health(connector_refs[reference], by_ref[reference])
            for reference in connector_refs
        )


def _connector_health(mailbox: Phase1Mailbox, row: Mapping[str, object]) -> ConnectorHealth:
    if set(row) != {
        "observer_connector_instance_ref",
        "status",
        "freshness",
        "backlog",
        "last_success_at",
        "safe_error_code",
    }:
        raise HTTPException(status_code=503, detail={"code": "connector_health_unavailable"})
    raw_status = row.get("status")
    raw_freshness = row.get("freshness")
    raw_backlog = row.get("backlog")
    raw_last_success = row.get("last_success_at")
    raw_error = row.get("safe_error_code")
    if (
        not isinstance(raw_status, str)
        or not isinstance(raw_freshness, str)
        or not isinstance(raw_backlog, int)
        or isinstance(raw_backlog, bool)
        or not (raw_last_success is None or isinstance(raw_last_success, str))
        or not (raw_error is None or isinstance(raw_error, str))
    ):
        raise HTTPException(status_code=503, detail={"code": "connector_health_unavailable"})
    status: str | None = {
        "enabled": "healthy",
        "error": "degraded",
        "paused": "paused",
        "disabled": "unknown",
    }.get(raw_status)
    if status is None:
        raise HTTPException(status_code=503, detail={"code": "connector_health_unavailable"})
    try:
        parsed_success = (
            None
            if raw_last_success is None
            else datetime.fromisoformat(raw_last_success.replace("Z", "+00:00"))
        )
        return ConnectorHealth(
            mailbox_ref=mailbox.mailbox_ref,
            mailbox_label=mailbox.display_label,
            status=status,
            freshness=raw_freshness,
            backlog=raw_backlog,
            last_success_at=parsed_success,
            safe_error_code=raw_error,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "connector_health_unavailable"},
        ) from exc


def _bounded_header(value: object, maximum: int) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and not any(character in value for character in "\x00\r\n")
    )


class GatewayDataCipher:
    """Versioned AES-GCM envelope for restricted Gateway display projections."""

    def __init__(self, key_text: str) -> None:
        if re.fullmatch(r"[a-fA-F0-9]{64}", key_text) is None:
            raise EmailGatewayConfigError("Gateway data key is invalid")
        self._cipher = AESGCM(bytes.fromhex(key_text))

    def encrypt(self, value: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return b"eg1" + nonce + self._cipher.encrypt(nonce, value.encode(), b"email-gateway:v1")

    def decrypt(self, value: bytes) -> str:
        if not isinstance(value, bytes) or not value.startswith(b"eg1") or len(value) < 32:
            raise ValueError("Gateway protected text is invalid")
        return self._cipher.decrypt(value[3:15], value[15:], b"email-gateway:v1").decode()


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    config_path: Path = DEFAULT_CONFIG,
    emergency_stop_path: Path = DEFAULT_EMERGENCY_STOP,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    application_factory: ApplicationFactory | None = None,
    server_runner: Callable[..., None] | None = None,
    secret_provider: TextSecretProvider | None = None,
    internal_network: bool = False,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(manifest, component="email-gateway-api", environ=environment)
        if environment.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH", "true") != "false":
            raise LocalEntrypointDisabled("email gateway is killed by default")
        if environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false":
            raise LocalEntrypointDisabled("external send must remain disabled")
        if emergency_stop_path.exists():
            raise LocalEntrypointDisabled("email gateway emergency stop is active")
        config = load_email_gateway_config(config_path)
        _validate_manifest(manifest, config)
        require_gateway_component(config, "email_gateway_api")
        if config.postgres.user != "gbos_email_gateway_app":
            raise EmailGatewayConfigError("email gateway API database role is invalid")
        validate_server_binding(
            host=config.listen.host,
            port=config.listen.port,
            network_mode="internal_network" if internal_network else "loopback",
        )
        active_secret_provider = secret_provider or _secret_provider()
        bearer = load_secret_file(
            config.auth.email_publication_bearer_file,
            secret_provider=active_secret_provider,
            logical_name="email_publication_bearer",
        )
        bff_bearer = load_secret_file(
            config.auth.email_gateway_bff_bearer_file,
            secret_provider=active_secret_provider,
            logical_name="email_gateway_bff_bearer",
        )
        mailbox_projection_bearer = load_secret_file(
            config.auth.mailbox_projection_bearer_file,
            secret_provider=active_secret_provider,
            logical_name="mailbox_projection_bearer",
        )
        data_key = load_secret_file(
            config.auth.email_gateway_data_key_file,
            secret_provider=active_secret_provider,
            logical_name="email_gateway_data_key",
        )
        cipher = GatewayDataCipher(data_key.reveal())
        connection = connect_postgres(
            config.postgres,
            connector=connector,
            secret_provider=active_secret_provider,
            environ=environment,
        )
        mailboxes = PostgresMailboxRepository(
            connection,  # type: ignore[arg-type]
            encrypt_restricted_text=cipher.encrypt,
            decrypt_restricted_text=cipher.decrypt,
        )
        intake = PostgresIntakeRepository(
            connection,  # type: ignore[arg-type]
            encrypt_restricted_text=cipher.encrypt,
            decrypt_restricted_text=cipher.decrypt,
        )
        phase1_read = PostgresPhase1ReadRepository(
            connection,  # type: ignore[arg-type]
            decrypt_restricted_text=cipher.decrypt,
        )
        connector_health = ObserverConnectorHealthReader(
            ObserverConnectorHealthClient(
                bearer_token=mailbox_projection_bearer.reveal(),
                auth_ref=config.auth.mailbox_projection_auth_ref,
            )
        )
        factory = application_factory or create_email_gateway_app
        application = _build_application(
            factory,
            intake=GatewayIntakeService(intake, MailboxRegistry(mailboxes)),  # type: ignore[arg-type]
            publication_bearer_token=bearer.reveal(),
            publication_auth_ref=config.auth.email_publication_auth_ref,
            bff_bearer_token=bff_bearer.reveal(),
            bff_auth_ref=config.auth.email_gateway_bff_auth_ref,
            mailbox_registry=MailboxRegistry(mailboxes),
            read_repository=phase1_read,
            connector_health_reader=connector_health,
        )
        active_runner = server_runner or run_server
        active_runner(
            application,
            host=config.listen.host,
            port=config.listen.port,
            unix_socket=None,
            network_mode="internal_network" if internal_network else "loopback",
        )
        return 0
    except (
        LocalEntrypointDisabled,
        EmailGatewayConfigError,
        RuntimeSupportError,
        ServerBindingError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _validate_manifest(manifest: Mapping[str, object], config: EmailGatewayRuntimeConfig) -> None:
    gateway = manifest.get("email_gateway")
    if (
        manifest.get("site_id") != config.site_id
        or manifest.get("production_go") is not False
        or not isinstance(gateway, Mapping)
        or gateway.get("kill_switch") is not False
        or gateway.get("external_send") is not False
    ):
        raise EmailGatewayConfigError("Gateway config does not match the local manifest")


def _build_application(factory: ApplicationFactory, **kwargs: object) -> FastAPI:
    parameters = inspect.signature(factory).parameters
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return factory(**kwargs)
    current = {
        "intake",
        "publication_bearer_token",
        "publication_auth_ref",
        "bff_bearer_token",
        "bff_auth_ref",
        "mailbox_registry",
        "read_repository",
        "connector_health_reader",
    }
    if current.issubset(parameters):
        selected = {name: value for name, value in kwargs.items() if name in parameters}
        return factory(**selected)
    raise EmailGatewayConfigError("Email Gateway application factory is incompatible")


def _secret_provider() -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        Path("/run/secrets"),
        (
            SecretSpec(
                "postgres_email_gateway_password",
                "postgres_email_gateway_password",
                "text",
                16,
                128,
            ),
            SecretSpec(
                "email_publication_bearer",
                "email_publication_bearer",
                "text",
                16,
                4096,
            ),
            SecretSpec(
                "email_gateway_bff_bearer",
                "email_gateway_bff_bearer",
                "text",
                16,
                4096,
            ),
            SecretSpec(
                "mailbox_projection_bearer",
                "mailbox_projection_bearer",
                "text",
                16,
                4096,
            ),
            SecretSpec(
                "email_gateway_data_key",
                "email_gateway_data_key",
                "text",
                64,
                64,
            ),
        ),
    )


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def _disabled_health() -> dict[str, object]:
    return {"ready": False, "external_send": False}


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GatewayDataCipher",
    "ObserverConnectorHealthClient",
    "ObserverConnectorHealthReader",
    "app",
    "main",
]
