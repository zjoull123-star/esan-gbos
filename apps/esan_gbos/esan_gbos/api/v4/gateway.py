from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, request_id, success
from esan_gbos.api.v4.client import LocalServiceClient, LocalServiceError


def v4_success(data: Any, **meta: Any) -> dict[str, Any]:
    return success(data, schema_version="4.0", **meta)


def active_site() -> str:
    site = str(getattr(frappe.local, "site", "")).strip()
    if not site:
        raise BFFError("internal_error", "Active site is unavailable", status=503)
    return site


def configured_client(service: str) -> LocalServiceClient:
    key = service.lower()
    base_url = str(frappe.conf.get(f"gbos_{key}_url") or "").strip()
    token = str(frappe.conf.get(f"gbos_{key}_token") or "").strip()
    auth_ref = str(frappe.conf.get(f"gbos_{key}_auth_ref") or "").strip()
    if not base_url or not token or not auth_ref:
        raise BFFError(
            "internal_error",
            f"Local {service} service is not configured",
            status=503,
        )
    timeout_value = frappe.conf.get(f"gbos_{key}_timeout_seconds") or 3.0
    try:
        timeout = float(timeout_value)
        return LocalServiceClient(
            service_name=service,
            base_url=base_url,
            token=token,
            auth_ref=auth_ref,
            timeout_seconds=timeout,
        )
    except (TypeError, ValueError, LocalServiceError) as error:
        raise BFFError(
            "internal_error",
            f"Local {service} service configuration is invalid",
            status=503,
        ) from error


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
