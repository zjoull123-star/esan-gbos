"""Import-safe, receive-only WhatsApp webhook composition."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.observer.observer.connectors.whatsapp_cloud import (
    WhatsAppCloudDeliveryAuthenticator,
    WhatsAppCloudDurableReceiver,
    WhatsAppCloudRequestError,
    verify_webhook_challenge,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_storage import (
    LocalPilotStorage,
    PostgresLocalPilotStorage,
)
from services.observer.observer.models import ConnectorKey, TenantScope, stable_ulid
from services.observer.observer.runtime import (
    KillSwitchEngaged,
    LocalPilotRuntimeGuard,
    map_whatsapp_durable_accept,
)
from services.observer.observer.storage import Connection

from .channel_config import (
    ChannelConfigError,
    WhatsAppCredentialConfig,
    load_channel_config,
    load_channel_credential,
    require_active_channel,
)
from .runtime_support import (
    RuntimeSupportError,
    close_connection,
    connect_postgres,
    load_runtime_config,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)
from .server import ServerBindingError, run_server, validate_server_binding

_WEBHOOK_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_/-]{0,254}$")
_DEFAULT_PATH = "/webhooks/whatsapp"
DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
DEFAULT_CONNECTORS_CONFIG = Path("/config/connectors.json")
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8000
ServerRunner = Callable[..., None]
StorageFactory = Callable[[object], LocalPilotStorage]


@dataclass(frozen=True, slots=True, repr=False)
class WhatsAppWebhookConfig:
    """Bounded route configuration; the verification value is always redacted."""

    path: str
    verify_token: str
    max_body_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or _WEBHOOK_PATH.fullmatch(self.path) is None:
            raise ValueError("invalid webhook path")
        if (
            not isinstance(self.verify_token, str)
            or not self.verify_token
            or len(self.verify_token.encode()) > 4_096
        ):
            raise ValueError("invalid verification token")
        if (
            isinstance(self.max_body_bytes, bool)
            or not isinstance(self.max_body_bytes, int)
            or not 1 <= self.max_body_bytes <= 16_777_216
        ):
            raise ValueError("invalid webhook body boundary")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(path={self.path!r}, "
            "verify_token=<redacted>, "
            f"max_body_bytes={self.max_body_bytes})"
        )


def _safe_error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code}},
    )


def _require_runtime(
    *,
    config: WhatsAppWebhookConfig | None,
    receiver: WhatsAppCloudDurableReceiver | None,
    guard: LocalPilotRuntimeGuard | None,
    clock: Callable[[], datetime] | None,
) -> bool:
    if config is None or receiver is None or guard is None or clock is None:
        return False
    try:
        guard.require_running()
    except KillSwitchEngaged:
        return False
    return True


async def _bounded_exact_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise WhatsAppCloudRequestError(
                status_code=400,
                reason_code="invalid_request",
            ) from None
        if declared_length < 0:
            raise WhatsAppCloudRequestError(
                status_code=400,
                reason_code="invalid_request",
            )
        if declared_length > maximum:
            raise WhatsAppCloudRequestError(
                status_code=413,
                reason_code="payload_too_large",
            )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            raise WhatsAppCloudRequestError(
                status_code=400,
                reason_code="invalid_request",
            )
        size += len(chunk)
        if size > maximum:
            raise WhatsAppCloudRequestError(
                status_code=413,
                reason_code="payload_too_large",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def create_whatsapp_webhook_app(
    *,
    config: WhatsAppWebhookConfig | None = None,
    receiver: WhatsAppCloudDurableReceiver | None = None,
    guard: LocalPilotRuntimeGuard | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Compose an inert FastAPI receiver without starting I/O or loading credentials."""

    route_path = _DEFAULT_PATH if config is None else config.path
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(route_path)
    async def challenge(request: Request) -> Response:
        if not _require_runtime(
            config=config,
            receiver=receiver,
            guard=guard,
            clock=clock,
        ):
            return _safe_error(503, "runtime_disabled")
        assert config is not None
        try:
            result = verify_webhook_challenge(
                mode=request.query_params.get("hub.mode", ""),
                supplied_token=request.query_params.get("hub.verify_token", ""),
                challenge=request.query_params.get("hub.challenge", ""),
                expected_token=config.verify_token,
            )
        except WhatsAppCloudRequestError as exc:
            return _safe_error(exc.status_code, exc.reason_code)
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers={"content-type": result.content_type},
        )

    @app.post(route_path)
    async def receive(request: Request) -> JSONResponse:
        if not _require_runtime(
            config=config,
            receiver=receiver,
            guard=guard,
            clock=clock,
        ):
            return _safe_error(503, "runtime_disabled")
        assert config is not None
        assert receiver is not None
        assert clock is not None
        try:
            exact_body = await _bounded_exact_body(request, config.max_body_bytes)
            received_at = clock()
            result = receiver.receive(
                exact_body=exact_body,
                signature_header=request.headers.get("x-hub-signature-256"),
                delivery_id=("whatsapp-webhook:" + hashlib.sha256(exact_body).hexdigest()),
                received_at=received_at,
            )
        except WhatsAppCloudRequestError as exc:
            return _safe_error(exc.status_code, exc.reason_code)
        return JSONResponse(
            status_code=result.status_code,
            content={"status": result.disposition},
        )

    return app


app = create_whatsapp_webhook_app()


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    connectors_path: Path = DEFAULT_CONNECTORS_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    storage_factory: StorageFactory | None = None,
    server_runner: ServerRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Preflight and run the receive-only durable WhatsApp webhook."""

    environment = os.environ if environ is None else environ
    active_clock = clock or _utc_now
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        if (
            environment.get("GBOS_CONNECTOR_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
        ):
            raise ChannelConfigError("WhatsApp runtime remains kill-switched")
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="whatsapp",
            environ=environment,
        )
        runtime = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, runtime)
        channels = load_channel_config(
            connectors_path,
            expected_site_id=runtime.site_id,
            manifest=manifest,
        )
        require_active_channel(channels, "whatsapp", now=active_clock())
        credential = load_channel_credential(channels, "whatsapp")
        if not isinstance(credential, WhatsAppCredentialConfig):
            raise ChannelConfigError("WhatsApp credential type is invalid")
        validate_server_binding(
            host=DEFAULT_WEBHOOK_HOST,
            port=DEFAULT_WEBHOOK_PORT,
            network_mode="internal_network",
        )

        connection = connect_postgres(runtime.postgres, connector=connector)
        storage = (
            PostgresLocalPilotStorage(cast(Connection, connection))
            if storage_factory is None
            else storage_factory(connection)
        )
        scope = TenantScope(runtime.site_id, "observation_processing")
        key = ConnectorKey("whatsapp", credential.instance_id)
        storage.register_connector_instance(
            scope,
            key,
            now=active_clock(),
            replay_window_seconds=300,
            team_ref=credential.team_ref,
            agent_task_type=credential.agent_task_type,
        )
        inbox = DurableDeliveryInbox(
            storage=storage,
            evidence_store=ContentAddressedEvidenceStore(channels.evidence_cas_root),
        )

        def accept_authenticated(
            delivery: Any,
            *,
            nonce: str,
            nonce_expires_at: datetime,
            now: datetime,
        ) -> str:
            accepted = inbox.accept_authenticated(
                scope,
                key,
                delivery,
                correlation_id=stable_ulid(
                    "whatsapp-delivery",
                    scope.site_id,
                    key.instance_id,
                    delivery.delivery_id,
                ),
                nonce=nonce,
                nonce_expires_at=nonce_expires_at,
                now=now,
            )
            return accepted.disposition

        receiver = WhatsAppCloudDurableReceiver(
            authenticator=WhatsAppCloudDeliveryAuthenticator(
                app_secret=credential.app_secret,
                max_body_bytes=credential.max_body_bytes,
            ),
            authenticated_accept=map_whatsapp_durable_accept(accept_authenticated),
            clock=active_clock,
        )
        configured_app = create_whatsapp_webhook_app(
            config=WhatsAppWebhookConfig(
                path=credential.path,
                verify_token=credential.verify_token,
                max_body_bytes=credential.max_body_bytes,
            ),
            receiver=receiver,
            guard=LocalPilotRuntimeGuard(enabled=True, kill_switch=False),
            clock=active_clock,
        )
        active_runner = server_runner or _run_server
        active_runner(
            configured_app,
            host=DEFAULT_WEBHOOK_HOST,
            port=DEFAULT_WEBHOOK_PORT,
            network_mode="internal_network",
        )
        return 0
    except (
        ChannelConfigError,
        LocalEntrypointDisabled,
        RuntimeSupportError,
        ServerBindingError,
        OSError,
        TypeError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_server(
    application: FastAPI,
    *,
    host: str,
    port: int,
    network_mode: str,
) -> None:
    run_server(
        application,
        host=host,
        port=port,
        network_mode=network_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "WhatsAppWebhookConfig",
    "app",
    "create_whatsapp_webhook_app",
    "main",
]
