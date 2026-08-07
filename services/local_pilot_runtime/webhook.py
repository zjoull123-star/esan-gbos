"""Import-safe, receive-only WhatsApp webhook composition."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from services.observer.observer.connectors.whatsapp_cloud import (
    WhatsAppCloudDurableReceiver,
    WhatsAppCloudRequestError,
    verify_webhook_challenge,
)
from services.observer.observer.runtime import (
    KillSwitchEngaged,
    LocalPilotRuntimeGuard,
)

_WEBHOOK_PATH = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_/-]{0,254}$")
_DEFAULT_PATH = "/webhooks/whatsapp"


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


def main() -> int:
    """Refuse standalone startup until dependencies are explicitly composed."""

    return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "WhatsAppWebhookConfig",
    "app",
    "create_whatsapp_webhook_app",
    "main",
]
