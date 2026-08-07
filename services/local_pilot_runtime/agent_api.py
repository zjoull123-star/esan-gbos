"""Import-safe local Agent read API entrypoint."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from services.agent_runtime.api import (
    AgentReadService,
    AgentRequestAuthorizer,
    create_agent_runtime_app,
)
from services.agent_runtime.materialization import MaterializationHealth


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


app = build_app()

__all__ = ["app", "build_app"]
