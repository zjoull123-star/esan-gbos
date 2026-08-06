from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)

_MAX_RESPONSE_BYTES = 1_048_576
_SAVE_PATHS = {
    RecordKind.EVIDENCE: "/internal/v1/context/evidence-records",
    RecordKind.FACT_PROPOSAL: "/internal/v1/context/fact-proposals",
    RecordKind.ENTITY_RESOLUTION_PROPOSAL: ("/internal/v1/context/entity-resolution-proposals"),
}
_GET_PATHS = {
    RecordKind.EVIDENCE: "/v1/context/evidence-records/{record_id}",
    RecordKind.FACT_PROPOSAL: "/v1/context/fact-proposals/{record_id}",
    RecordKind.ENTITY_RESOLUTION_PROPOSAL: ("/v1/context/entity-resolution-proposals/{record_id}"),
}


class ContextClientError(RuntimeError):
    """The local Context service failed closed or returned untrusted metadata."""


class JsonTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class UrllibJsonTransport:
    """Loopback HTTP transport with proxies disabled and a bounded JSON response."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        request = Request(url, data=data, headers=headers, method=method)
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return int(response.status), _read_json_response(response)
        except HTTPError as exc:
            return int(exc.code), _read_json_response(exc)
        except (OSError, URLError) as exc:
            raise ContextClientError("local Context service is unavailable") from exc


def _read_json_response(response: Any) -> dict[str, Any]:
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ContextClientError("local Context response exceeded its size budget")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextClientError("local Context response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ContextClientError("local Context response must be a JSON object")
    return value


class HttpContextRepository:
    """ContextRepository adapter restricted to the approved local loopback service."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: JsonTransport | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ContextClientError(
                "Gate 3 Context URL must be an uncredentialed loopback HTTP URL"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ContextClientError("Gate 3 Context URL has an invalid port") from exc
        if port is None:
            raise ContextClientError("Gate 3 Context URL must include an explicit port")
        if not token or "\n" in token or "\r" in token:
            raise ContextClientError("a valid local Context token is required")
        if not 0 < timeout_seconds <= 30:
            raise ContextClientError("Context timeout must be within 0 and 30 seconds")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport or UrllibJsonTransport()
        self._timeout_seconds = timeout_seconds

    def save(
        self,
        scope: TenantScope,
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata:
        if scope.site_id != envelope.site_id:
            raise ValidationError("Context client site scope does not match envelope")
        if scope.processing_purpose != envelope.processing_purpose:
            raise ValidationError("Context client purpose does not match envelope")
        status, response = self._transport.request(
            method="POST",
            url=self._base_url + _SAVE_PATHS[kind],
            headers=self._headers(
                scope,
                request_id=f"context-{envelope.payload_digest[:26]}",
                idempotency_key=envelope.idempotency_key,
                has_payload=True,
            ),
            payload=dict(envelope.payload),
            timeout_seconds=self._timeout_seconds,
        )
        if status == 409:
            raise IdempotencyConflict("Context idempotency conflict")
        if status != 200:
            raise ContextClientError("local Context service rejected publication")
        return self._trusted_metadata(
            response,
            expected_kind=kind,
            expected_scope=scope,
            expected_idempotency_key=envelope.idempotency_key,
            expected_payload_digest=envelope.payload_digest,
        )

    def get(
        self,
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
    ) -> RecordMetadata | None:
        if not record_id:
            raise ValidationError("Context record_id is required")
        status, response = self._transport.request(
            method="GET",
            url=self._base_url + _GET_PATHS[kind].format(record_id=record_id),
            headers=self._headers(
                scope,
                request_id=f"context-read-{kind.value}",
                idempotency_key=f"context-read:{kind.value}:{record_id}",
                has_payload=False,
            ),
            payload=None,
            timeout_seconds=self._timeout_seconds,
        )
        if status == 404:
            return None
        if status != 200:
            raise ContextClientError("local Context service rejected metadata read")
        metadata = self._trusted_metadata(
            response,
            expected_kind=kind,
            expected_scope=scope,
        )
        if metadata.record_id != record_id:
            raise ContextClientError("local Context returned mismatched metadata")
        return metadata

    def _headers(
        self,
        scope: TenantScope,
        *,
        request_id: str,
        idempotency_key: str,
        has_payload: bool,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Site-ID": scope.site_id,
            "X-Processing-Purpose": scope.processing_purpose,
            "X-Request-ID": request_id,
            "Idempotency-Key": idempotency_key,
        }
        if has_payload:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _trusted_metadata(
        response: dict[str, Any],
        *,
        expected_kind: RecordKind,
        expected_scope: TenantScope,
        expected_idempotency_key: str | None = None,
        expected_payload_digest: str | None = None,
    ) -> RecordMetadata:
        data = response.get("data")
        if not isinstance(data, dict):
            raise ContextClientError("local Context response omitted metadata")
        try:
            recorded_at = datetime.fromisoformat(str(data["recorded_at"]))
            metadata = RecordMetadata(
                kind=RecordKind(str(data["kind"])),
                record_id=str(data["record_id"]),
                site_id=str(data["site_id"]),
                processing_purpose=str(data["processing_purpose"]),
                idempotency_key=str(data["idempotency_key"]),
                payload_digest=str(data["payload_digest"]),
                recorded_at=recorded_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextClientError("local Context returned invalid metadata") from exc
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ContextClientError("local Context returned invalid metadata")
        if (
            metadata.kind is not expected_kind
            or metadata.site_id != expected_scope.site_id
            or metadata.processing_purpose != expected_scope.processing_purpose
            or (
                expected_idempotency_key is not None
                and metadata.idempotency_key != expected_idempotency_key
            )
            or (
                expected_payload_digest is not None
                and metadata.payload_digest != expected_payload_digest
            )
        ):
            raise ContextClientError("local Context returned mismatched metadata")
        return metadata
