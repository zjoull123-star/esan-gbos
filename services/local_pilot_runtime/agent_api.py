"""Import-safe local Agent read API entrypoint."""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi import FastAPI

from services.agent_runtime.api import (
    AgentReadService,
    AgentRequestAuthorizer,
    create_agent_runtime_app,
)
from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.agent_runtime.materialization import MaterializationHealth
from services.agent_runtime.postgres import PostgresAgentTaskRepository
from services.agent_runtime.read_service import PostgresAgentReadService

from .runtime_support import (
    RuntimeSupportError,
    SecretValue,
    close_connection,
    component_settings,
    connect_postgres,
    load_runtime_config,
    load_secret_file,
    reject_plaintext_secret_environment,
    validate_manifest_binding,
)
from .server import ServerBindingError, run_server, validate_server_binding

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
ServerRunner = Callable[..., None]


class ConstantTimeBearerSiteAuthorizer:
    """Exact local bearer and site binding without secret-bearing repr output."""

    __slots__ = ("_site_id", "_token")

    def __init__(self, *, bearer_token: SecretValue, site_id: str) -> None:
        if not site_id:
            raise ValueError("site_id is required")
        self._site_id = site_id
        self._token = bearer_token.reveal().encode("utf-8")

    def __repr__(self) -> str:
        return (
            f"ConstantTimeBearerSiteAuthorizer(site_id={self._site_id!r}, bearer_token=<redacted>)"
        )

    def authorize(
        self,
        *,
        authorization: str | None,
        requested_site_id: str | None,
    ) -> str:
        prefix = "Bearer "
        supplied = (
            b""
            if authorization is None or not authorization.startswith(prefix)
            else authorization[len(prefix) :].encode("utf-8")
        )
        token_matches = hmac.compare_digest(supplied, self._token)
        site_matches = requested_site_id is not None and hmac.compare_digest(
            requested_site_id, self._site_id
        )
        if not token_matches or not site_matches:
            raise PermissionError("local Agent API identity rejected")
        return self._site_id


def build_app(
    *,
    read_service: AgentReadService | None = None,
    authorizer: AgentRequestAuthorizer | None = None,
    health_provider: Callable[[str], MaterializationHealth] | None = None,
) -> FastAPI:
    return create_agent_runtime_app(
        read_service=read_service,
        authorizer=authorizer,
        health_provider=health_provider,
    )


def build_postgres_app(
    *,
    connection: object,
    site_id: str,
    bearer_token: SecretValue,
) -> FastAPI:
    repository = PostgresAgentTaskRepository(connection)  # type: ignore[arg-type]
    return build_app(
        read_service=PostgresAgentReadService(connection),  # type: ignore[arg-type]
        authorizer=ConstantTimeBearerSiteAuthorizer(
            bearer_token=bearer_token,
            site_id=site_id,
        ),
        health_provider=repository.materialization_health,
    )


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    server_runner: ServerRunner | None = None,
    internal_network: bool = False,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="agent-api",
            environ=environment,
        )
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        component_settings(config, "agent_api")
        unix_socket_value = environment.get("GBOS_LISTEN_UNIX_SOCKET")
        network_mode = (
            "unix_socket"
            if unix_socket_value is not None
            else "internal_network"
            if internal_network
            else "loopback"
        )
        unix_socket = validate_server_binding(
            host=config.listen.host,
            port=config.listen.agent_api_port,
            unix_socket=unix_socket_value,
            network_mode=network_mode,
        )
        bearer_token = load_secret_file(config.auth.agent_api_bearer_file)
        connection = connect_postgres(config.postgres, connector=connector)
        configured_app = build_postgres_app(
            connection=connection,
            site_id=config.site_id,
            bearer_token=bearer_token,
        )
        active_runner = server_runner or _run_server
        active_runner(
            configured_app,
            host=config.listen.host,
            port=config.listen.agent_api_port,
            unix_socket=unix_socket,
            network_mode=network_mode,
        )
        return 0
    except LocalEntrypointDisabled, RuntimeSupportError, ServerBindingError, ValueError:
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _run_server(
    application: FastAPI,
    *,
    host: str,
    port: int,
    unix_socket: Path | None,
    network_mode: str,
) -> None:
    run_server(
        application,
        host=host,
        port=port,
        unix_socket=unix_socket,
        network_mode=network_mode,
    )


app = build_app()

if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConstantTimeBearerSiteAuthorizer",
    "app",
    "build_app",
    "build_postgres_app",
    "main",
]
