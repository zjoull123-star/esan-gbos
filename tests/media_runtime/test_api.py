from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI

from services.media_runtime.api import MediaUploadAPIConfig, create_media_upload_app
from services.media_runtime.repository import InMemoryMediaJobRepository
from services.media_runtime.upload import (
    StoredUploadRefs,
    UploadBinding,
    UploadService,
)


class AcceptingVerifier:
    def verify(self, credential: str, binding: UploadBinding) -> bool:
        return credential == "Bearer local-secret" and binding.site_id == "site-a"


class RecordingTemporaryUpload:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def finalize(self, *, sha256: str, byte_size: int, media_type: str) -> StoredUploadRefs:
        assert byte_size == sum(map(len, self.chunks))
        assert media_type == "audio/wav"
        assert len(sha256) == 64
        return StoredUploadRefs(
            object_ref="object://site-a/object-01",
            evidence_ref="evidence://site-a/evidence-01",
        )

    def abort(self) -> None:
        self.chunks.clear()


class RecordingSink:
    def __init__(self) -> None:
        self.upload = RecordingTemporaryUpload()

    def open(self, _binding: UploadBinding, *, idempotency_key: str) -> RecordingTemporaryUpload:
        assert idempotency_key.startswith("upload:")
        return self.upload


def _app(
    *,
    max_bytes: int = 32,
) -> tuple[FastAPI, RecordingSink, InMemoryMediaJobRepository]:
    sink = RecordingSink()
    repository = InMemoryMediaJobRepository()
    service = UploadService(
        verifier=AcceptingVerifier(),
        temporary_sink=sink,
        clock=lambda: datetime(2026, 8, 8, tzinfo=UTC),
        receipt_id_factory=lambda: "receipt-01",
        max_bytes=max_bytes,
    )
    app = create_media_upload_app(
        upload_service=service,
        repository=repository,
        config=MediaUploadAPIConfig(
            max_upload_bytes=max_bytes,
            max_filename_bytes=64,
            allowed_media_types=frozenset({"audio/wav"}),
        ),
    )
    return app, sink, repository


def _headers(**overrides: str) -> dict[str, str]:
    values = {
        "Authorization": "Bearer local-secret",
        "X-Site-ID": "site-a",
        "X-Request-ID": "request-01",
        "X-Purpose": "meeting_capture",
        "X-Source-Kind": "meeting",
        "X-Filename": "meeting.wav",
        "X-Media-Duration-Ms": "1000",
        "X-Media-Channels": "1",
        "X-Media-Sample-Rate": "16000",
        "Content-Type": "audio/wav",
        "Content-Length": "7",
    }
    values.update(overrides)
    return values


async def _request(
    app: Any,
    *,
    headers: dict[str, str],
    body: bytes = b"one-two",
    client: tuple[str, int] = ("127.0.0.1", 50000),
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as http:
        return await http.post("/internal/v1/media/uploads", headers=headers, content=body)


def test_loopback_authenticated_upload_streams_exact_bytes_and_enqueues_job() -> None:
    app, sink, repository = _app()

    response = asyncio.run(_request(app, headers=_headers()))

    assert response.status_code == 202
    assert sink.upload.chunks == [b"one-two"]
    payload = response.json()["data"]
    assert payload["receipt"]["byte_size"] == 7
    assert payload["job"]["status"] == "queued"
    assert repository.get("site-a", payload["job"]["job_id"]) is not None
    assert response.headers["cache-control"] == "no-store"


def test_non_loopback_request_is_rejected_before_upload_service() -> None:
    app, sink, _repository = _app()

    response = asyncio.run(
        _request(
            app,
            headers=_headers(),
            client=("198.51.100.10", 50000),
        )
    )

    assert response.status_code == 403
    assert sink.upload.chunks == []


def test_size_mime_filename_and_auth_limits_fail_closed() -> None:
    cases = (
        (_headers(**{"Content-Length": "33"}), 413),
        (_headers(**{"Content-Type": "video/mp4"}), 415),
        (_headers(**{"X-Filename": "../meeting.wav"}), 400),
        (_headers(**{"Authorization": "Bearer wrong"}), 401),
    )

    for headers, expected in cases:
        app, _sink, repository = _app()
        response = asyncio.run(_request(app, headers=headers))
        assert response.status_code == expected
        assert repository.get_by_request("site-a", "request-01") is None
