from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, request_id, success
from esan_gbos.api.v4.client import LocalServiceClient, LocalServiceError

_GATEWAY_URL = "http://email-gateway-api:8004"
_BEARER_FILE = Path("/run/secrets/email_gateway_bff_bearer")
_MAX_BEARER_BYTES = 4096
_AUTH_REF = "email-gateway-bff-v1"
_EMAIL_ROLES = frozenset(
    {"CEO", "Sales Manager", "Sales User", "Reviewer", "Integration Admin", "GBOS Admin"}
)


def v5_success(data: Any, **meta: Any) -> dict[str, Any]:
    return success(data, schema_version="5.0", **meta)


def active_site() -> str:
    site = str(getattr(frappe.local, "site", "")).strip()
    if not site:
        raise BFFError("internal_error", "Active site is unavailable", status=503)
    return site


def scope_payload(*, business_read: bool = False) -> dict[str, Any]:
    roles = sorted(set(frappe.get_roles()) & _EMAIL_ROLES)
    rows = frappe.get_all(
        "GBOS Team Member",
        filters={"user": frappe.session.user, "enabled": 1},
        fields=["parent"],
        order_by="parent asc",
    )
    if {"CEO", "GBOS Admin"} & set(roles):
        teams = ["*"]
    else:
        teams = sorted({str(row["parent"]) for row in rows if row.get("parent")})
    if business_read and not teams:
        raise BFFError("permission_denied", "A governed team scope is required", status=403)
    return {
        "actor_ref": str(frappe.session.user),
        "actor_roles": roles,
        "allowed_team_refs": teams,
    }


def configured_gateway_client() -> LocalServiceClient:
    configured_url = str(frappe.conf.get("gbos_email_gateway_url") or "").strip()
    configured_file = str(frappe.conf.get("gbos_email_gateway_token_file") or "").strip()
    inline = str(frappe.conf.get("gbos_email_gateway_token") or "").strip()
    auth_ref = str(frappe.conf.get("gbos_email_gateway_auth_ref") or "").strip()
    if (
        configured_url != _GATEWAY_URL
        or configured_file != str(_BEARER_FILE)
        or inline
        or auth_ref != _AUTH_REF
    ):
        raise BFFError(
            "internal_error",
            "Email Gateway service configuration is invalid",
            status=503,
        )
    try:
        bearer = _read_bearer(_BEARER_FILE)
        return LocalServiceClient(
            service_name="Email Gateway",
            base_url=_GATEWAY_URL,
            token=bearer,
            auth_ref=auth_ref,
            timeout_seconds=3.0,
            allowed_internal_urls=frozenset({_GATEWAY_URL}),
        )
    except LocalServiceError as error:
        raise BFFError(
            "internal_error",
            "Email Gateway service configuration is invalid",
            status=503,
        ) from error


def _read_bearer(path: Path) -> str:
    try:
        before = path.lstat()
        if not _safe_bearer_details(before):
            raise LocalServiceError("mounted bearer file is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            after = os.fstat(descriptor)
            if not _safe_bearer_details(after) or (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise LocalServiceError("mounted bearer file is unsafe")
            raw = os.read(descriptor, _MAX_BEARER_BYTES + 1)
        finally:
            os.close(descriptor)
    except LocalServiceError:
        raise
    except OSError:
        raise LocalServiceError("mounted bearer file is unavailable") from None
    if not 0 < len(raw) <= _MAX_BEARER_BYTES:
        raise LocalServiceError("mounted bearer file is empty or unbounded")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LocalServiceError("mounted bearer file is invalid") from None
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise LocalServiceError("mounted bearer file is invalid")
    return value


def _safe_bearer_details(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and stat.S_IMODE(details.st_mode) in {0o400, 0o600}
        and 0 < details.st_size <= _MAX_BEARER_BYTES
    )


def call_gateway(
    *,
    method: str,
    path: str,
    purpose: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    try:
        response = configured_gateway_client().request(
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
                "Email Gateway rejected the governed request",
                status=409,
            ) from error
        if error.error_code == "not_found":
            raise BFFError("not_found", "Email Gateway record was not found", status=404) from error
        if error.error_code == "invalid_query":
            raise BFFError("invalid_query", "Email Gateway rejected the query") from error
        raise BFFError("internal_error", "Email Gateway is unavailable", status=503) from error
    data = response.get("data")
    if not isinstance(data, dict):
        raise BFFError("internal_error", "Email Gateway returned an invalid response", status=503)
    return data
