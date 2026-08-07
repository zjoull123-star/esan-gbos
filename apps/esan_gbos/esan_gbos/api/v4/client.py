from __future__ import annotations

import http.client
import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_DEFAULT_MAX_REQUEST_BYTES = 262_144
_SAFE_ERROR_CODES = frozenset(
    {
        "idempotency_conflict",
        "request_in_progress",
        "revision_conflict",
        "invalid_transition",
        "scope_mismatch",
        "not_found",
        "invalid_query",
    }
)


class LocalServiceError(RuntimeError):
    """A local service is unavailable or returned an untrusted response."""

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


def read_bounded_json(response: Any, *, max_response_bytes: int) -> dict[str, Any]:
    raw = response.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise LocalServiceError("local service response exceeded its size budget")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalServiceError("local service response was not valid JSON") from error
    if not isinstance(value, dict):
        raise LocalServiceError("local service response must be a JSON object")
    return value


class JsonTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        raise NotImplementedError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibJsonTransport(JsonTransport):
    """Loopback HTTP transport with proxies and redirects disabled."""

    def __init__(self, *, max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES) -> None:
        self._max_response_bytes = max_response_bytes

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        data = _serialize_payload(payload)
        request = Request(url, data=data, headers=headers, method=method)
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                return int(response.status), read_bounded_json(
                    response,
                    max_response_bytes=self._max_response_bytes,
                )
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise LocalServiceError("local service redirect was rejected") from error
            return int(error.code), read_bounded_json(
                error,
                max_response_bytes=self._max_response_bytes,
            )
        except (OSError, URLError) as error:
            raise LocalServiceError("local service is unavailable") from error


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self._socket_path)
        self.sock = connection


class UnixSocketJsonTransport(JsonTransport):
    """Minimal HTTP/1.1 JSON transport over an explicitly configured Unix socket."""

    def __init__(
        self,
        socket_path: str,
        *,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._socket_path = socket_path
        self._max_response_bytes = max_response_bytes

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection = _UnixHTTPConnection(self._socket_path, timeout=timeout_seconds)
        try:
            connection.request(
                method,
                path,
                body=_serialize_payload(payload),
                headers=headers,
            )
            response = connection.getresponse()
            return int(response.status), read_bounded_json(
                response,
                max_response_bytes=self._max_response_bytes,
            )
        except OSError as error:
            raise LocalServiceError("local Unix service is unavailable") from error
        finally:
            connection.close()


def _serialize_payload(payload: dict[str, Any] | None) -> bytes | None:
    if payload is None:
        return None
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LocalServiceError("local service payload was not valid JSON") from error
    if len(raw) > _DEFAULT_MAX_REQUEST_BYTES:
        raise LocalServiceError("local service payload exceeded its size budget")
    return raw


def _header_value(value: str, field: str) -> str:
    if not value or "\r" in value or "\n" in value:
        raise LocalServiceError(f"a valid local {field} is required")
    return value


class LocalServiceClient:
    def __init__(
        self,
        *,
        service_name: str,
        base_url: str,
        token: str,
        auth_ref: str,
        transport: JsonTransport | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        parsed = urlsplit(base_url)
        unix_socket_path: str | None = None
        if parsed.scheme == "unix":
            if not parsed.path.startswith("/") or parsed.netloc or parsed.query or parsed.fragment:
                raise LocalServiceError("local Unix service URL is invalid")
            unix_socket_path = parsed.path
            normalized_base = "http://localhost"
        else:
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise LocalServiceError(
                    f"{service_name} URL must be an uncredentialed loopback HTTP URL"
                )
            try:
                port = parsed.port
            except ValueError as error:
                raise LocalServiceError(f"{service_name} URL has an invalid port") from error
            if port is None:
                raise LocalServiceError(f"{service_name} loopback URL must include a port")
            normalized_base = base_url.rstrip("/")
        if not 0 < timeout_seconds <= 10:
            raise LocalServiceError("local service timeout must be within 0 and 10 seconds")
        self._service_name = _header_value(service_name, "service name")
        self._base_url = normalized_base
        self._token = _header_value(token, "token")
        self._auth_ref = _header_value(auth_ref, "authentication reference")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or (
            UnixSocketJsonTransport(unix_socket_path)
            if unix_socket_path is not None
            else UrllibJsonTransport()
        )

    def request(
        self,
        *,
        method: str,
        path: str,
        site_id: str,
        purpose: str,
        request_id: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise LocalServiceError("local service method is not allowed")
        if (
            not path.startswith("/internal/")
            or "://" in path
            or "\r" in path
            or "\n" in path
            or "/../" in path
        ):
            raise LocalServiceError("local service path is invalid")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-GBOS-Local-Auth-Ref": self._auth_ref,
            "X-Site-ID": _header_value(site_id, "site"),
            "X-Processing-Purpose": _header_value(purpose, "processing purpose"),
            "X-Request-ID": _header_value(request_id, "request ID"),
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _header_value(
                idempotency_key,
                "idempotency key",
            )
        if payload is not None:
            headers["Content-Type"] = "application/json"
        status, response = self._transport.request(
            method=normalized_method,
            url=self._base_url + path,
            headers=headers,
            payload=payload,
            timeout_seconds=self._timeout_seconds,
        )
        if status != 200:
            error_value = response.get("error")
            error_code = (
                error_value.get("code")
                if isinstance(error_value, dict) and error_value.get("code") in _SAFE_ERROR_CODES
                else None
            )
            raise LocalServiceError(
                f"local {self._service_name} service rejected the request",
                status=status,
                error_code=error_code,
            )
        response_site = response.get("site_id")
        data = response.get("data")
        if response_site is None and isinstance(data, dict):
            response_site = data.get("site_id")
        if response_site is not None and response_site != site_id:
            raise LocalServiceError("local service returned mismatched site scope")
        return response
