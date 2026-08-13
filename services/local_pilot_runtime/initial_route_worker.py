"""Default-off initial Inbox routing worker for accepted identity projections."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import httpx

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.email_gateway.initial_routing import (
    FRAPPE_INITIAL_ROUTE_URL,
    FrappeInitialRouteClient,
    InitialRouteProcessor,
    InitialRouteStatus,
    InitialRouteTransportTimeout,
    PermanentInitialRouteError,
)
from services.email_gateway.models import PROCESSING_PURPOSES, TenantScope
from services.email_gateway.repositories.identity_route_work import (
    PostgresIdentityRouteWorkRepository,
)

from .email_gateway_config import (
    EmailGatewayConfigError,
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

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_CONFIG = Path("/config/email-gateway-runtime.json")
DEFAULT_EMERGENCY_STOP = Path("/run/gbos/EMERGENCY_STOP")
_SECRET_ROOT = Path("/run/secrets")
_DB_SECRET = "postgres_email_gateway_password"
_FRAPPE_KEY = "frappe_email_gateway_authority_api_key"
_FRAPPE_SECRET = "frappe_email_gateway_authority_api_secret"


class HttpxInitialRouteTransport:
    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        if url != FRAPPE_INITIAL_ROUTE_URL:
            raise PermanentInitialRouteError("authority_url_rejected")
        try:
            with httpx.Client(
                timeout=timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.post(url, headers=dict(headers), json=dict(payload))
        except httpx.TimeoutException:
            raise InitialRouteTransportTimeout("authority_timeout") from None
        except httpx.HTTPError, OSError, ValueError:
            raise PermanentInitialRouteError("authority_transport_rejected") from None
        if len(response.content) > 16_384:
            raise PermanentInitialRouteError("authority_response_invalid")
        try:
            body = response.json()
        except ValueError:
            raise PermanentInitialRouteError("authority_response_invalid") from None
        if not isinstance(body, dict):
            raise PermanentInitialRouteError("authority_response_invalid")
        return response.status_code, body


def run_initial_route_daemon(
    processor: InitialRouteProcessor,
    *,
    site_id: str,
    stop_event: Event,
    idle_delay_seconds: float,
) -> None:
    purposes = tuple(sorted(PROCESSING_PURPOSES))
    while not stop_event.is_set():
        statuses = tuple(
            processor.run_once(TenantScope(site_id, purpose)).status for purpose in purposes
        )
        if all(status is InitialRouteStatus.IDLE for status in statuses):
            stop_event.wait(idle_delay_seconds)


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    config_path: Path = DEFAULT_CONFIG,
    emergency_stop_path: Path = DEFAULT_EMERGENCY_STOP,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    transport_factory: Callable[[], HttpxInitialRouteTransport] | None = None,
    daemon_runner: Callable[..., None] | None = None,
    stop_event: Event | None = None,
    secret_provider: TextSecretProvider | None = None,
) -> int:
    """Preflight every switch, config value, and secret before DB or HTTP."""

    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="email-initial-route-worker",
            environ=environment,
        )
        gateway = manifest.get("email_gateway")
        if (
            environment.get("GBOS_EMAIL_INITIAL_ROUTE_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EMAIL_GATEWAY_KILL_SWITCH", "true") != "false"
            or environment.get("GBOS_EXTERNAL_SEND_ENABLED", "false") != "false"
            or emergency_stop_path.exists()
            or not isinstance(gateway, Mapping)
            or gateway.get("kill_switch") is not False
            or gateway.get("initial_route_kill_switch") is not False
            or gateway.get("external_send") is not False
        ):
            raise LocalEntrypointDisabled("initial route worker is disabled")
        config = load_email_gateway_config(config_path)
        require_gateway_component(config, "email_gateway_worker")
        if (
            config.site_id != manifest.get("site_id")
            or config.external_send is not False
            or config.postgres.user != "gbos_email_gateway_worker"
        ):
            raise EmailGatewayConfigError("initial route worker binding is invalid")

        active_secrets = secret_provider or _secret_provider()
        # All three mounted secrets are validated before connect_postgres or
        # transport construction. connect_postgres reuses the same provider.
        load_secret_file(
            config.postgres.password_file,
            secret_provider=active_secrets,
            logical_name=_DB_SECRET,
        )
        api_key = load_secret_file(
            config.auth.frappe_email_gateway_authority_api_key_file,
            secret_provider=active_secrets,
            logical_name=_FRAPPE_KEY,
        ).reveal()
        api_secret = load_secret_file(
            config.auth.frappe_email_gateway_authority_api_secret_file,
            secret_provider=active_secrets,
            logical_name=_FRAPPE_SECRET,
        ).reveal()

        connection = connect_postgres(
            config.postgres,
            connector=connector,
            secret_provider=active_secrets,
            environ=environment,
        )
        repository = PostgresIdentityRouteWorkRepository(connection)  # type: ignore[arg-type]
        processor = InitialRouteProcessor(
            repository=repository,
            authority=FrappeInitialRouteClient(
                transport=(transport_factory or HttpxInitialRouteTransport)(),
                api_key=api_key,
                api_secret=api_secret,
                auth_ref=config.auth.frappe_email_gateway_authority_auth_ref,
            ),
            worker_id=config.worker.worker_id,
            clock=lambda: datetime.now(UTC),
        )
        (daemon_runner or run_initial_route_daemon)(
            processor,
            site_id=config.site_id,
            stop_event=stop_event or Event(),
            idle_delay_seconds=config.worker.idle_delay_seconds,
        )
        return 0
    except (
        EmailGatewayConfigError,
        LocalEntrypointDisabled,
        PermanentInitialRouteError,
        RuntimeSupportError,
        TypeError,
        ValueError,
    ):
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _secret_provider() -> MountedFileSecretProvider:
    return MountedFileSecretProvider(
        _SECRET_ROOT,
        (
            SecretSpec(_DB_SECRET, _DB_SECRET, "text", 16, 128),
            SecretSpec(_FRAPPE_KEY, _FRAPPE_KEY, "text", 16, 128),
            SecretSpec(_FRAPPE_SECRET, _FRAPPE_SECRET, "text", 16, 128),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HttpxInitialRouteTransport", "main", "run_initial_route_daemon"]
