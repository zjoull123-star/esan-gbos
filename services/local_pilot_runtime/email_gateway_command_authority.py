"""Bounded, proxy-free Frappe client for fenced email command authority."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, TypeGuard
from urllib import error as urlerror
from urllib import request as urlrequest

from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import CommandPublication

from .runtime_support import RuntimeSupportError

FRAPPE_EMAIL_COMMAND_AUTHORITY_URL = (
    "http://frappe-backend:8000/api/method/"
    "esan_gbos.api.internal.email_gateway_authority.resolve_email_send_command"
)
_MAX_RESPONSE_BYTES = 65_536


class _HttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, size: int) -> bytes: ...

    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, *args: object) -> object: ...


class _HttpOpener(Protocol):
    def open(self, request: object, *, timeout: float) -> _HttpResponse: ...


class _RejectRedirects(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class FrappeEmailCommandAuthorityClient:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        auth_ref: str,
        timeout_seconds: float = 5.0,
        opener: _HttpOpener | None = None,
    ) -> None:
        if not valid_frappe_email_command_authority_client_config(
            api_key=api_key,
            api_secret=api_secret,
            auth_ref=auth_ref,
            timeout_seconds=timeout_seconds,
        ):
            raise RuntimeSupportError("Frappe email command authority credentials rejected")
        self._authorization = f"token {api_key}:{api_secret}"
        self._auth_ref = auth_ref
        self._timeout = timeout_seconds
        self._opener = opener or urlrequest.build_opener(
            urlrequest.ProxyHandler({}),
            _RejectRedirects(),
        )

    def __repr__(self) -> str:
        return "FrappeEmailCommandAuthorityClient(credentials=<redacted>)"

    def resolve(
        self,
        scope: TenantScope,
        publication: CommandPublication,
        command: Mapping[str, Any],
    ) -> Mapping[str, object]:
        request_id = command.get("request_id")
        command_ref = command.get("command_id")
        if (
            not isinstance(request_id, str)
            or not isinstance(command_ref, str)
            or command.get("site_id") != scope.site_id
            or command.get("processing_purpose") != scope.processing_purpose
        ):
            raise RuntimeSupportError("Frappe email command authority request rejected")
        payload = {
            "site_id": scope.site_id,
            "processing_purpose": "email_gateway_authority",
            "request_id": request_id,
            "auth_ref": self._auth_ref,
            "publication_ref": publication.publication_ref,
            "attempt": publication.attempt,
            "generation": publication.generation,
            "fence_token": publication.fence_token,
            "command_ref": command_ref,
            "payload_digest": publication.payload_digest,
        }
        request = urlrequest.Request(
            FRAPPE_EMAIL_COMMAND_AUTHORITY_URL,
            data=json.dumps({"payload": payload}, separators=(",", ":"), sort_keys=True).encode(),
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Content-Type": "application/json",
                "Host": scope.site_id,
                "X-GBOS-Frappe-Auth-Ref": self._auth_ref,
                "X-Site-ID": scope.site_id,
                "X-Processing-Purpose": "email_gateway_authority",
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200:
                    raise RuntimeSupportError("Frappe email command authority unavailable")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    raise RuntimeSupportError("Frappe email command authority response rejected")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise RuntimeSupportError("Frappe email command authority response rejected")
            value = json.loads(body)
        except RuntimeSupportError:
            raise
        except (urlerror.URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeSupportError("Frappe email command authority unavailable") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"message"}
            or not isinstance(value["message"], dict)
            or set(value["message"]) != {"email_send_authority"}
            or not isinstance(value["message"]["email_send_authority"], dict)
        ):
            raise RuntimeSupportError("Frappe email command authority response rejected")
        return value["message"]["email_send_authority"]


def _credential(value: object) -> TypeGuard[str]:
    return bool(
        isinstance(value, str)
        and 15 <= len(value) <= 128
        and value == value.strip()
        and all(char not in value for char in "\x00\r\n")
    )


def valid_frappe_email_command_authority_client_config(
    *,
    api_key: object,
    api_secret: object,
    auth_ref: object,
    timeout_seconds: object,
) -> bool:
    return bool(
        _credential(api_key)
        and _credential(api_secret)
        and ":" not in api_key
        and auth_ref == "email-gateway-authority-v1"
        and isinstance(timeout_seconds, int | float)
        and not isinstance(timeout_seconds, bool)
        and 0 < timeout_seconds <= 10
    )


__all__ = [
    "FRAPPE_EMAIL_COMMAND_AUTHORITY_URL",
    "FrappeEmailCommandAuthorityClient",
    "valid_frappe_email_command_authority_client_config",
]
