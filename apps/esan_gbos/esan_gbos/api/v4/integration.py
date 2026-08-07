from __future__ import annotations

from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v4.gateway import call_local, v4_success
from esan_gbos.domain.v4_dto import (
    V4DTOValidationError,
    map_connector_status,
    validate_connector_command,
)

INTEGRATION_ROLES = frozenset({"Integration Admin", "GBOS Admin"})


def _integer(value: int | str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_dto", f"{field} must be an integer") from error
    if isinstance(value, bool):
        raise BFFError("invalid_dto", f"{field} must be an integer")
    return parsed


def _command_payload(
    instance_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        return validate_connector_command(
            {
                "instance_id": instance_id,
                "expected_revision": _integer(expected_revision, "expected_revision"),
                "idempotency_key": idempotency_key,
            }
        )
    except V4DTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error


def _command(
    action: str,
    *,
    instance_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(INTEGRATION_ROLES)
    payload = _command_payload(instance_id, expected_revision, idempotency_key)

    def execute() -> dict[str, Any]:
        downstream_payload = dict(payload)
        if action == "replay":
            downstream_payload.update(
                {
                    "delivery_scope": "eligible_failed_deliveries",
                    "limit": 100,
                    "requires": [
                        "within_connector_replay_window",
                        "not_retention_expired",
                        "same_site_and_instance",
                    ],
                }
            )
        data = call_local(
            "Observer",
            method="POST",
            path=f"/internal/v1/bff/connectors/{action}",
            purpose="connector_control",
            payload=downstream_payload,
            idempotency_key=payload["idempotency_key"],
        )
        value = data.get("connector", data)
        if not isinstance(value, dict):
            raise BFFError("internal_error", "Observer connector response is invalid", status=503)
        try:
            return map_connector_status(value)
        except V4DTOValidationError as error:
            raise BFFError(
                "internal_error",
                "Observer connector response is invalid",
                status=503,
            ) from error

    result, replayed, original_request_id = run_idempotent(
        f"integration.{action}",
        payload["idempotency_key"],
        payload,
        execute,
        api_version="v4",
    )
    return v4_success(
        result,
        replayed=replayed,
        original_request_id=original_request_id,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list_status(channel: str | None = None) -> dict[str, Any]:
    require_roles(INTEGRATION_ROLES)
    payload = {"channel": channel.strip()} if isinstance(channel, str) and channel.strip() else {}
    data = call_local(
        "Observer",
        method="POST",
        path="/internal/v1/bff/connectors/list",
        purpose="connector_status",
        payload=payload,
    )
    connectors = data.get("connectors")
    if not isinstance(connectors, list):
        raise BFFError("internal_error", "Observer connector list is invalid", status=503)
    try:
        mapped = [map_connector_status(item) for item in connectors]
    except (TypeError, V4DTOValidationError) as error:
        raise BFFError(
            "internal_error",
            "Observer connector list is invalid",
            status=503,
        ) from error
    return v4_success({"connectors": mapped})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def pause(
    instance_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _command(
        "pause",
        instance_id=instance_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def resume(
    instance_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _command(
        "resume",
        instance_id=instance_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("POST")
def replay(
    instance_id: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _command(
        "replay",
        instance_id=instance_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
