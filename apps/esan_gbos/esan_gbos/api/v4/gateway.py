from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, request_id, success
from esan_gbos.api.v4.client import LocalServiceClient, LocalServiceError

_TOKEN_DIRECTORY = Path("/run/secrets")
_SAFE_TOKEN_FILENAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_MAX_TOKEN_FILE_BYTES = 4096
_INTERNAL_SERVICE_URLS = {
    "agent": frozenset({"http://agent-api:8002"}),
    "observer": frozenset({"http://observer-api:8003"}),
}


def v4_success(data: Any, **meta: Any) -> dict[str, Any]:
    return success(data, schema_version="4.0", **meta)


def active_site() -> str:
    site = str(getattr(frappe.local, "site", "")).strip()
    if not site:
        raise BFFError("internal_error", "Active site is unavailable", status=503)
    return site


def configured_client(service: str) -> LocalServiceClient:
    key = service.lower()
    allowed_internal_urls = _INTERNAL_SERVICE_URLS.get(key)
    if allowed_internal_urls is None:
        raise BFFError(
            "internal_error",
            f"Local {service} service is not configured",
            status=503,
        )
    base_url = str(frappe.conf.get(f"gbos_{key}_url") or "").strip()
    token_file = str(frappe.conf.get(f"gbos_{key}_token_file") or "").strip()
    inline_token = str(frappe.conf.get(f"gbos_{key}_token") or "").strip()
    auth_ref = str(frappe.conf.get(f"gbos_{key}_auth_ref") or "").strip()
    if not base_url or not auth_ref or (not token_file and not inline_token):
        raise BFFError(
            "internal_error",
            f"Local {service} service is not configured",
            status=503,
        )
    timeout_value = frappe.conf.get(f"gbos_{key}_timeout_seconds") or 3.0
    try:
        token = _configured_token(
            token_file=token_file,
            inline_token=inline_token,
        )
        timeout = float(timeout_value)
        return LocalServiceClient(
            service_name=service,
            base_url=base_url,
            token=token,
            auth_ref=auth_ref,
            timeout_seconds=timeout,
            allowed_internal_urls=allowed_internal_urls,
        )
    except (TypeError, ValueError, LocalServiceError) as error:
        raise BFFError(
            "internal_error",
            f"Local {service} service configuration is invalid",
            status=503,
        ) from error


def _configured_token(*, token_file: str, inline_token: str) -> str:
    if token_file and inline_token:
        raise LocalServiceError("local service token sources conflict")
    if token_file:
        return _read_token_file(token_file)
    if not _developer_mode_enabled():
        raise LocalServiceError("inline local service tokens require developer mode")
    return inline_token


def _developer_mode_enabled() -> bool:
    value = frappe.conf.get("developer_mode")
    return value is True or value == 1 or value == "1"


def _read_token_file(value: str) -> str:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.parent != _TOKEN_DIRECTORY
        or _SAFE_TOKEN_FILENAME.fullmatch(path.name) is None
    ):
        raise LocalServiceError("local service token file path is invalid")
    try:
        before = path.lstat()
        if not _safe_token_file_details(before):
            raise LocalServiceError("local service token file is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            after = os.fstat(descriptor)
            if not _safe_token_file_details(after) or (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise LocalServiceError("local service token file is unsafe")
            raw = bytearray()
            while len(raw) <= _MAX_TOKEN_FILE_BYTES:
                chunk = os.read(
                    descriptor,
                    _MAX_TOKEN_FILE_BYTES + 1 - len(raw),
                )
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            os.close(descriptor)
    except LocalServiceError:
        raise
    except OSError:
        raise LocalServiceError("local service token file is unavailable") from None
    if not 0 < len(raw) <= _MAX_TOKEN_FILE_BYTES:
        raise LocalServiceError("local service token file is empty or unbounded")
    try:
        token = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise LocalServiceError("local service token file is not valid UTF-8") from None
    if token.endswith("\n"):
        token = token[:-1]
    if not token or "\x00" in token or "\r" in token or "\n" in token:
        raise LocalServiceError("local service token file contains invalid characters")
    return token


def _safe_token_file_details(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and stat.S_IMODE(details.st_mode) in {0o400, 0o600}
        and 0 < details.st_size <= _MAX_TOKEN_FILE_BYTES
    )


def call_local(
    service: str,
    *,
    method: str,
    path: str,
    purpose: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    try:
        response = configured_client(service).request(
            method=method,
            path=path,
            site_id=active_site(),
            purpose=purpose,
            request_id=request_id(),
            payload=payload,
            idempotency_key=idempotency_key,
        )
    except LocalServiceError as error:
        if error.error_code in {
            "idempotency_conflict",
            "request_in_progress",
            "revision_conflict",
            "invalid_transition",
            "scope_mismatch",
        }:
            raise BFFError(
                error.error_code,
                f"Local {service} rejected the governed request",
                status=409,
            ) from error
        if error.error_code == "not_found":
            raise BFFError(
                "not_found", f"Local {service} record was not found", status=404
            ) from error
        if error.error_code == "invalid_query":
            raise BFFError("invalid_query", f"Local {service} rejected the query") from error
        if error.status == 409:
            raise BFFError(
                "revision_conflict",
                f"Local {service} rejected a stale revision",
                status=409,
            ) from error
        raise BFFError(
            "internal_error",
            f"Local {service} service is unavailable",
            status=503,
        ) from error
    data = response.get("data")
    if not isinstance(data, dict):
        raise BFFError(
            "internal_error",
            f"Local {service} returned an invalid response",
            status=503,
        )
    return data
