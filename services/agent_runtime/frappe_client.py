"""Authenticated, local-only Frappe client for controlled AI Draft materialization."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .materialization import FrappeDraftReceipt
from .models import IdempotencyConflict, ValidationError, canonical_payload_digest, thaw_json
from .proposals import MaterializationIntent

_APPLY_PATH = "/api/method/esan_gbos.api.internal.materialization.apply_draft"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_REQUEST_BYTES = 262_144
_MAX_RESPONSE_BYTES = 1_048_576
_FRAPPE_INTERNAL_PORT = 8000
_LOCAL_SOCKET_DIRECTORY = PurePosixPath("/run/gbos/sockets")
_SAFE_SOCKET_FILENAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,126}[A-Za-z0-9])?\.sock$")
_SAFE_INTERNAL_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class FrappeClientError(RuntimeError):
    """Safe transport/protocol failure without downstream response content."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class FrappeJsonTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxFrappeTransport:
    """Bounded httpx transport with environment proxies and redirects disabled."""

    __slots__ = ("_socket_path",)

    def __init__(self, socket_path: str | None = None) -> None:
        self._socket_path = socket_path

    def request(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        encoded = _serialize_request(payload)
        transport = (
            httpx.HTTPTransport(uds=self._socket_path, retries=0)
            if self._socket_path is not None
            else httpx.HTTPTransport(retries=0)
        )
        try:
            with (
                httpx.Client(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(timeout_seconds),
                ) as client,
                client.stream(
                    "POST",
                    url,
                    headers=headers,
                    content=encoded,
                ) as response,
            ):
                if 300 <= response.status_code < 400:
                    raise FrappeClientError("Frappe redirect was rejected")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise FrappeClientError("Frappe response exceeded its size budget")
                return int(response.status_code), _decode_response(bytes(raw))
        except FrappeClientError:
            raise
        except httpx.HTTPError, OSError:
            raise FrappeClientError("Frappe is unavailable") from None


class _FrappeHttpBoundary:
    __slots__ = (
        "_api_key",
        "_api_secret",
        "_auth_ref",
        "_base_url",
        "_site_id",
        "_timeout_seconds",
        "_transport",
    )

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        auth_ref: str,
        site_id: str,
        timeout_seconds: float,
        transport: FrappeJsonTransport | None,
        allowed_internal_hosts: frozenset[str] = frozenset(),
    ) -> None:
        normalized_base, socket_path = _local_endpoint(
            base_url,
            allowed_internal_hosts=allowed_internal_hosts,
        )
        if not 0 < timeout_seconds <= 10:
            raise FrappeClientError("Frappe timeout must be within 0 and 10 seconds")
        self._base_url = normalized_base
        self._api_key = _header(api_key, "API key")
        self._api_secret = _header(api_secret, "API secret")
        self._auth_ref = _header(auth_ref, "authentication reference")
        self._site_id = _header(site_id, "site")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or HttpxFrappeTransport(socket_path)

    def __repr__(self) -> str:
        return (
            "_FrappeHttpBoundary("
            f"base_url={self._base_url!r}, site_id={self._site_id!r}, "
            f"auth_ref={self._auth_ref!r}, credentials=<redacted>)"
        )

    @property
    def site_id(self) -> str:
        return self._site_id

    @property
    def auth_ref(self) -> str:
        return self._auth_ref

    def post(
        self,
        *,
        path: str,
        purpose: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        purpose_value = _header(purpose, "processing purpose")
        request_value = _header(request_id, "request ID")
        headers = {
            "Authorization": f"token {self._api_key}:{self._api_secret}",
            "Content-Type": "application/json",
            "Host": self._site_id,
            "X-GBOS-Frappe-Auth-Ref": self._auth_ref,
            "X-Processing-Purpose": purpose_value,
            "X-Request-ID": request_value,
            "X-Site-ID": self._site_id,
        }
        status, response = self._transport.request(
            url=self._base_url + path,
            headers=headers,
            payload={"payload": payload},
            timeout_seconds=self._timeout_seconds,
        )
        body = _unwrap_message(response)
        if status != 200:
            error = body.get("error")
            error_code = error.get("code") if isinstance(error, dict) else None
            if status == 409 and error_code == "idempotency_conflict":
                raise IdempotencyConflict("Frappe materialization request body conflict")
            raise FrappeClientError(
                "Frappe rejected the governed request",
                status=status,
                error_code=error_code if isinstance(error_code, str) else None,
            )
        return body


class HttpFrappeDraftClient:
    """FrappeDraftClient implementation restricted to a local authenticated endpoint."""

    __slots__ = ("_boundary", "_processing_purpose")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        auth_ref: str,
        site_id: str,
        processing_purpose: str,
        timeout_seconds: float = 3.0,
        transport: FrappeJsonTransport | None = None,
        allowed_internal_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self._boundary = _FrappeHttpBoundary(
            base_url=base_url,
            api_key=api_key,
            api_secret=api_secret,
            auth_ref=auth_ref,
            site_id=site_id,
            timeout_seconds=timeout_seconds,
            transport=transport,
            allowed_internal_hosts=allowed_internal_hosts,
        )
        self._processing_purpose = _header(
            processing_purpose,
            "processing purpose",
        )

    def __repr__(self) -> str:
        return (
            "HttpFrappeDraftClient("
            f"site_id={self._boundary.site_id!r}, "
            f"processing_purpose={self._processing_purpose!r}, credentials=<redacted>)"
        )

    def apply(
        self,
        intent: MaterializationIntent,
        *,
        request_id: str,
        request_digest: str,
    ) -> FrappeDraftReceipt:
        values = thaw_json(intent.values)
        if not isinstance(values, dict):
            raise ValidationError("materialization intent values must be an object")
        intent_document = {
            "operation": intent.operation,
            "doctype": intent.doctype,
            "values": values,
        }
        if (
            _DIGEST.fullmatch(request_digest) is None
            or canonical_payload_digest(intent_document) != request_digest
        ):
            raise ValidationError("materialization request digest does not match intent")
        response = self._boundary.post(
            path=_APPLY_PATH,
            purpose=self._processing_purpose,
            request_id=request_id,
            payload={
                "site_id": self._boundary.site_id,
                "processing_purpose": self._processing_purpose,
                "request_id": request_id,
                "auth_ref": self._boundary.auth_ref,
                "request_digest": request_digest,
                "intent": intent_document,
            },
        )
        receipt = _receipt(response)
        if (
            response.get("site_id") != self._boundary.site_id
            or receipt.doctype != intent.doctype
            or receipt.request_id != request_id
            or receipt.request_digest != request_digest
        ):
            raise FrappeClientError("Frappe returned a mismatched materialization receipt")
        return receipt


def _receipt(value: Mapping[str, Any]) -> FrappeDraftReceipt:
    try:
        doctype = value["doctype"]
        name = value["name"]
        revision = value["revision"]
        request_id = value["request_id"]
        request_digest = value["request_digest"]
        if not all(isinstance(item, str) for item in (doctype, name, request_id, request_digest)):
            raise TypeError
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise TypeError
        return FrappeDraftReceipt(
            doctype=doctype,
            name=name,
            revision=revision,
            request_id=request_id,
            request_digest=request_digest,
        )
    except KeyError, TypeError, ValidationError:
        raise FrappeClientError("Frappe returned an invalid materialization receipt") from None


def _local_endpoint(
    value: str,
    *,
    allowed_internal_hosts: frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    allowed_hosts = _validated_internal_hosts(allowed_internal_hosts)
    parsed = urlsplit(value)
    if parsed.scheme == "unix":
        socket_path = PurePosixPath(parsed.path)
        if (
            not parsed.path.startswith("/")
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or socket_path.parent != _LOCAL_SOCKET_DIRECTORY
            or _SAFE_SOCKET_FILENAME.fullmatch(socket_path.name) is None
        ):
            raise FrappeClientError("Frappe Unix socket URL is invalid")
        return "http://frappe.internal", parsed.path
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
    ):
        raise FrappeClientError(
            "Frappe URL must be an uncredentialed local or allowed internal HTTP URL"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise FrappeClientError("Frappe URL has an invalid host or port") from error
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None:
        if not address.is_loopback or port is None:
            raise FrappeClientError("Frappe URL must use a literal loopback address and port")
        return value.rstrip("/"), None
    hostname = parsed.hostname
    if (
        hostname not in allowed_hosts
        or port != _FRAPPE_INTERNAL_PORT
        or parsed.netloc != f"{hostname}:{_FRAPPE_INTERNAL_PORT}"
    ):
        raise FrappeClientError("Frappe URL host and port are not explicitly allowed")
    return value.rstrip("/"), None


def _validated_internal_hosts(value: frozenset[str]) -> frozenset[str]:
    if not isinstance(value, frozenset):
        raise FrappeClientError("Frappe internal host allowlist must be a frozenset")
    for host in value:
        if (
            not isinstance(host, str)
            or _SAFE_INTERNAL_HOST.fullmatch(host) is None
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise FrappeClientError("Frappe internal host allowlist is invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        raise FrappeClientError("Frappe internal host allowlist cannot contain IP addresses")
    return value


def _header(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise FrappeClientError(f"a valid Frappe {field} is required")
    return value


def _serialize_request(value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    except (TypeError, ValueError) as error:
        raise FrappeClientError("Frappe request was not valid JSON") from error
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise FrappeClientError("Frappe request exceeded its size budget")
    return encoded


def _decode_response(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrappeClientError("Frappe response was not valid JSON") from error
    if not isinstance(value, dict):
        raise FrappeClientError("Frappe response must be a JSON object")
    return value


def _unwrap_message(response: dict[str, Any]) -> dict[str, Any]:
    message = response.get("message")
    if message is None:
        return response
    if not isinstance(message, dict):
        raise FrappeClientError("Frappe response message must be an object")
    return message


__all__ = [
    "FrappeClientError",
    "FrappeJsonTransport",
    "HttpFrappeDraftClient",
    "HttpxFrappeTransport",
]
