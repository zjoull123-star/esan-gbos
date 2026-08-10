from __future__ import annotations

import builtins
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v4.gateway import call_local, v4_success
from esan_gbos.domain.permissions import PermissionScopeError, communication_scope
from esan_gbos.domain.v4_dto import (
    V4DTOValidationError,
    map_communication_detail,
    map_communication_summary,
)

COMMUNICATION_ROLES = frozenset({"Sales Manager", "Sales User", "CEO", "GBOS Admin"})


def _integer(value: int | str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_query", f"{field} must be an integer") from error
    if isinstance(value, bool):
        raise BFFError("invalid_query", f"{field} must be an integer")
    return parsed


def _scope() -> dict[str, Any]:
    roles = set(frappe.get_roles())
    teams: set[str] = set()
    if not roles & {"CEO", "GBOS Admin"}:
        rows = frappe.get_all(
            "GBOS Team Member",
            filters={"user": frappe.session.user, "enabled": 1},
            fields=["parent"],
            order_by="parent asc",
        )
        teams = {str(row["parent"]) for row in rows if row.get("parent")}
    try:
        return communication_scope(
            roles=roles,
            actor_ref=frappe.session.user,
            team_refs=teams,
        )
    except PermissionScopeError as error:
        raise BFFError(
            "permission_denied",
            "No resolvable team scope",
            status=403,
        ) from error


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def list(
    channel: str | None = None,
    classification: str | None = None,
    review_status: str | None = None,
    cursor: str | None = None,
    page_size: int | str = 25,
) -> dict[str, Any]:
    require_roles(COMMUNICATION_ROLES)
    size = _integer(page_size, "page_size")
    if not 1 <= size <= 50:
        raise BFFError("invalid_query", "page_size is outside the allowed range")
    payload: dict[str, Any] = {
        **_scope(),
        "page_size": size,
    }
    for key, value in (
        ("channel", channel),
        ("classification", classification),
        ("review_status", review_status),
        ("cursor", cursor),
    ):
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    data = call_local(
        "Observer",
        method="POST",
        path="/internal/v1/bff/communications/list",
        purpose="communication_projection",
        payload=payload,
    )
    rows = data.get("communications")
    next_cursor = data.get("next_cursor")
    if not isinstance(rows, builtins.list) or not (
        next_cursor is None or isinstance(next_cursor, str)
    ):
        raise BFFError("internal_error", "Observer communication list is invalid", status=503)
    try:
        mapped = [map_communication_summary(item) for item in rows]
    except (TypeError, V4DTOValidationError) as error:
        raise BFFError(
            "internal_error",
            "Observer communication list is invalid",
            status=503,
        ) from error
    return v4_success(
        {"communications": mapped, "next_cursor": next_cursor},
        next_cursor=next_cursor,
        page_size=size,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def get(observation_id: str) -> dict[str, Any]:
    require_roles(COMMUNICATION_ROLES)
    value = _scoped_observer_detail(observation_id)
    try:
        communication = map_communication_detail(value)
    except (TypeError, V4DTOValidationError) as error:
        raise BFFError(
            "internal_error",
            "Observer communication detail is invalid",
            status=503,
        ) from error
    return v4_success({"communication": communication})


def _scoped_observer_detail(observation_id: str) -> dict[str, Any]:
    """Fetch one closed detail after proving the current user's Observer scope."""
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise BFFError("invalid_query", "observation_id is required")
    data = call_local(
        "Observer",
        method="POST",
        path="/internal/v1/bff/communications/get",
        purpose="communication_projection",
        payload={
            **_scope(),
            "observation_id": observation_id.strip(),
        },
    )
    value = data.get("communication")
    if not isinstance(value, dict):
        raise BFFError("not_found", "Communication was not found", status=404)
    return value
