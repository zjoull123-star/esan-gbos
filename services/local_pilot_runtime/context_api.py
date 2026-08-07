"""Import-safe Context-to-Agent API with explicit PostgreSQL composition."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi import FastAPI

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)
from services.context.context_service.agent_runtime_api import (
    create_agent_context_runtime_app,
)
from services.context.context_service.decision_postgres import (
    PostgresDecisionStorage,
)

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

DEFAULT_MANIFEST = Path("/config/local-pilot-manifest.json")
DEFAULT_RUNTIME_CONFIG = Path("/config/local-pilot-runtime.json")
ServerRunner = Callable[[FastAPI, str, int], None]


def build_app(
    *,
    connection: object | None = None,
    bearer_token: SecretValue | None = None,
    auth_ref: str | None = None,
) -> FastAPI:
    if connection is None or bearer_token is None or auth_ref is None:
        return create_agent_context_runtime_app()
    return create_agent_context_runtime_app(
        storage=PostgresDecisionStorage(connection),  # type: ignore[arg-type]
        local_token=bearer_token.reveal(),
        local_auth_ref=auth_ref,
    )


def main(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_config_path: Path = DEFAULT_RUNTIME_CONFIG,
    environ: Mapping[str, str] | None = None,
    connector: Callable[..., object] | None = None,
    server_runner: ServerRunner | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    connection: object | None = None
    try:
        reject_plaintext_secret_environment(environment)
        manifest = load_local_manifest(manifest_path)
        require_component_enabled(
            manifest,
            component="context-api",
            environ=environment,
        )
        config = load_runtime_config(runtime_config_path)
        validate_manifest_binding(manifest, config)
        component_settings(config, "context_api")
        bearer_token = load_secret_file(config.auth.context_api_bearer_file)
        connection = connect_postgres(config.postgres, connector=connector)
        configured_app = build_app(
            connection=connection,
            bearer_token=bearer_token,
            auth_ref=config.auth.context_auth_ref,
        )
        active_runner = server_runner or _run_server
        active_runner(
            configured_app,
            config.listen.host,
            config.listen.context_api_port,
        )
        return 0
    except LocalEntrypointDisabled, RuntimeSupportError, ValueError:
        return 78
    finally:
        if connection is not None:
            close_connection(connection)


def _run_server(application: FastAPI, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(application, host=host, port=port, access_log=False)


app = build_app()

if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["app", "build_app", "main"]
