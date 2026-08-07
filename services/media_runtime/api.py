from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import queue
import re
from collections.abc import AsyncIterable, Iterator
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .repository import MediaJobRepository, MediaJobSubmission
from .upload import (
    SourceKind,
    UploadBinding,
    UploadReceipt,
    UploadRejected,
    UploadRequest,
    UploadService,
)

_BOUND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")
_END = object()


@dataclass(frozen=True, slots=True)
class MediaUploadAPIConfig:
    max_upload_bytes: int
    max_filename_bytes: int
    allowed_media_types: frozenset[str]
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_upload_bytes <= 2**40:
            raise ValueError("max_upload_bytes_invalid")
        if not 1 <= self.max_filename_bytes <= 1_024:
            raise ValueError("max_filename_bytes_invalid")
        if not self.allowed_media_types or any(
            "/" not in media_type or len(media_type) > 255
            for media_type in self.allowed_media_types
        ):
            raise ValueError("allowed_media_types_invalid")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts_invalid")


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: BaseException


class _ChunkBridge:
    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | _StreamFailure | object] = queue.Queue(maxsize=4)
        self._consumer_done = Event()

    def chunks(self) -> Iterator[bytes]:
        try:
            while not self._consumer_done.is_set():
                try:
                    item = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is _END:
                    return
                if isinstance(item, _StreamFailure):
                    raise item.error
                if not isinstance(item, bytes):
                    raise RuntimeError("invalid_stream_bridge_item")
                yield item
        finally:
            self._consumer_done.set()

    def put(self, item: bytes | _StreamFailure | object) -> None:
        while not self._consumer_done.is_set():
            try:
                self._queue.put(item, timeout=0.05)
                return
            except queue.Full:
                continue

    def consumer_finished(self) -> None:
        self._consumer_done.set()


def create_media_upload_app(
    *,
    upload_service: UploadService | None = None,
    repository: MediaJobRepository | None = None,
    config: MediaUploadAPIConfig | None = None,
) -> FastAPI:
    application = FastAPI(
        title="ESAN GBOS Local Media Upload API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def enforce_local_no_store(request: Request, call_next: Any) -> Response:
        if not _is_loopback(request):
            response = Response(
                content='{"detail":"loopback client required"}',
                status_code=403,
                media_type="application/json",
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/health")
    def health() -> dict[str, object]:
        ready = upload_service is not None and repository is not None and config is not None
        return {
            "status": "ok" if ready else "disabled",
            "ready": ready,
            "local_only": True,
            "streaming_upload": ready,
            "formal_command_capability": False,
            "runtime_download": False,
        }

    @application.post("/internal/v1/media/uploads", status_code=202)
    async def upload(request: Request) -> dict[str, object]:
        if upload_service is None or repository is None or config is None:
            raise HTTPException(status_code=503, detail="media upload runtime is disabled")
        parsed = _parse_headers(request, config)
        binding = UploadBinding(
            site_id=parsed.site_id,
            purpose=parsed.purpose,
            source_kind=parsed.source_kind,
            request_id=parsed.request_id,
            declared_size=parsed.declared_size,
        )
        filename_digest = hashlib.sha256(parsed.filename.encode("utf-8")).hexdigest()
        upload_request = UploadRequest(
            binding=binding,
            credential=parsed.credential,
            media_type=parsed.media_type,
            filename_metadata_ref=f"localmeta://{filename_digest}",
        )
        try:
            receipt = await _receive_stream(upload_service, upload_request, request.stream())
        except UploadRejected as exc:
            raise _upload_http_error(exc) from None
        try:
            job = repository.enqueue(
                MediaJobSubmission(
                    receipt=receipt,
                    duration_ms=parsed.duration_ms,
                    channels=parsed.channels,
                    sample_rate=parsed.sample_rate,
                    language_hint=parsed.language_hint,
                    max_attempts=config.max_attempts,
                ),
                now=receipt.received_at,
            )
        except ValueError as exc:
            if str(exc) == "idempotency_conflict":
                raise HTTPException(status_code=409, detail="request replay conflict") from None
            raise HTTPException(status_code=422, detail="media work metadata rejected") from None
        except Exception:
            raise HTTPException(
                status_code=503, detail="media job repository unavailable"
            ) from None
        return {
            "data": {
                "receipt": job.receipt.to_contract(),
                "job": job.to_summary(),
            },
            "meta": {
                "request_id": receipt.request_id,
                "schema_version": "1.0",
            },
        }

    return application


@dataclass(frozen=True, slots=True)
class _ParsedUploadHeaders:
    credential: str = field(repr=False)
    site_id: str
    request_id: str
    purpose: str
    source_kind: SourceKind
    filename: str = field(repr=False)
    media_type: str
    declared_size: int
    duration_ms: int
    channels: int
    sample_rate: int
    language_hint: str | None


def _parse_headers(request: Request, config: MediaUploadAPIConfig) -> _ParsedUploadHeaders:
    credential = _required_header(request, "authorization", max_length=2_048)
    site_id = _bounded_id(_required_header(request, "x-site-id"), "site_id")
    request_id = _bounded_id(_required_header(request, "x-request-id"), "request_id")
    purpose = _bounded_id(_required_header(request, "x-purpose"), "purpose")
    source_raw = _required_header(request, "x-source-kind", max_length=32)
    try:
        source_kind = SourceKind(source_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="source kind rejected") from None
    filename = _required_header(
        request,
        "x-filename",
        max_length=config.max_filename_bytes,
    )
    if (
        len(filename.encode("utf-8")) > config.max_filename_bytes
        or filename in {".", ".."}
        or _SAFE_FILENAME.fullmatch(filename) is None
    ):
        raise HTTPException(status_code=400, detail="filename rejected")
    media_type = _required_header(request, "content-type", max_length=255).lower()
    if media_type not in config.allowed_media_types:
        raise HTTPException(status_code=415, detail="media type rejected")
    declared_size = _positive_int_header(request, "content-length", maximum=config.max_upload_bytes)
    duration_ms = _positive_int_header(request, "x-media-duration-ms", maximum=7_200_000)
    channels = _positive_int_header(request, "x-media-channels", maximum=8)
    sample_rate = _positive_int_header(request, "x-media-sample-rate", maximum=192_000)
    if sample_rate < 8_000:
        raise HTTPException(status_code=400, detail="media sample rate rejected")
    language_hint = request.headers.get("x-language-hint")
    if language_hint is not None and not 2 <= len(language_hint) <= 16:
        raise HTTPException(status_code=400, detail="language hint rejected")
    return _ParsedUploadHeaders(
        credential=credential,
        site_id=site_id,
        request_id=request_id,
        purpose=purpose,
        source_kind=source_kind,
        filename=filename,
        media_type=media_type,
        declared_size=declared_size,
        duration_ms=duration_ms,
        channels=channels,
        sample_rate=sample_rate,
        language_hint=language_hint,
    )


async def _receive_stream(
    service: UploadService,
    upload_request: UploadRequest,
    stream: AsyncIterable[bytes],
) -> UploadReceipt:
    bridge = _ChunkBridge()

    def consume() -> UploadReceipt:
        try:
            return service.receive(upload_request, bridge.chunks())
        finally:
            bridge.consumer_finished()

    consumer = asyncio.create_task(asyncio.to_thread(consume))
    try:
        try:
            async for chunk in stream:
                if chunk:
                    await asyncio.to_thread(bridge.put, bytes(chunk))
                if consumer.done():
                    break
        except Exception as exc:
            await asyncio.to_thread(bridge.put, _StreamFailure(exc))
        else:
            await asyncio.to_thread(bridge.put, _END)
        return await consumer
    finally:
        bridge.consumer_finished()


def _required_header(request: Request, name: str, *, max_length: int = 256) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1 or not values[0] or len(values[0]) > max_length:
        raise HTTPException(status_code=400, detail=f"{name} rejected")
    return values[0]


def _positive_int_header(request: Request, name: str, *, maximum: int) -> int:
    raw = _required_header(request, name, max_length=20)
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} rejected") from None
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"{name} rejected")
    if value > maximum:
        status = 413 if name == "content-length" else 400
        raise HTTPException(status_code=status, detail=f"{name} rejected")
    return value


def _bounded_id(value: str, name: str) -> str:
    if _BOUND_ID.fullmatch(value) is None:
        raise HTTPException(status_code=400, detail=f"{name} rejected")
    return value


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _upload_http_error(exc: UploadRejected) -> HTTPException:
    if exc.code == "authentication_failed":
        return HTTPException(status_code=401, detail="upload authentication rejected")
    if exc.code in {"size_limit_exceeded", "size_exceeded"}:
        return HTTPException(status_code=413, detail="upload size rejected")
    return HTTPException(status_code=400, detail="upload rejected")


# Safe import-time default: no verifier, object sink, repository, or listener.
app = create_media_upload_app()


__all__ = ["MediaUploadAPIConfig", "app", "create_media_upload_app"]
