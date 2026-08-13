from __future__ import annotations

import builtins
import re
from typing import Any

import frappe

from esan_gbos.api.v1.audit import run_idempotent
from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles
from esan_gbos.api.v5.gateway import call_gateway, call_observer, scope_payload, v5_success
from esan_gbos.domain.v5_email_dto import (
    V5EmailDTOValidationError,
    map_connector_health,
    map_mailbox,
    validate_mailbox_status,
    validate_mailbox_upsert,
)

EMAIL_ADMIN_ROLES = frozenset({"Integration Admin", "GBOS Admin"})
_OPAQUE_MAILBOX_ADDRESS = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_TEAM_REF = re.compile(r"^TEM-[0-9A-HJKMNP-TV-Z]{26}$")
_GATEWAY_MAILBOX_FIELDS = frozenset(
    {
        "mailbox_ref",
        "display_label",
        "provider_kind",
        "business_mode",
        "business_purpose",
        "default_team_ref",
        "account_owner_user_ref",
        "inbound_enabled",
        "outbound_enabled",
        "status",
        "config_revision",
    }
)


def _integer(value: int | str, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise BFFError("invalid_query", f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise BFFError("invalid_query", f"{field} must be an integer") from error
    if result < 0 or (maximum is not None and result > maximum):
        raise BFFError("invalid_query", f"{field} is outside the allowed range")
    return result


def _optional(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_transient_text(value: object, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BFFError("invalid_dto", f"{field} is invalid")
    return value


def _map_mailbox_response(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("mailbox")
    if not isinstance(value, dict):
        raise BFFError("internal_error", "Email Gateway mailbox response is invalid", status=503)
    try:
        return map_mailbox(_public_mailbox(value))
    except V5EmailDTOValidationError as error:
        raise BFFError(
            "internal_error", "Email Gateway mailbox response is invalid", status=503
        ) from error


def _public_mailbox(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _GATEWAY_MAILBOX_FIELDS:
        raise V5EmailDTOValidationError("unexpected internal mailbox fields")
    return {
        **{
            key: item
            for key, item in value.items()
            if key not in {"default_team_ref", "account_owner_user_ref"}
        },
        "default_team_label": _label("GBOS Team", value["default_team_ref"], "team_name"),
        "account_owner_label": _label("User", value["account_owner_user_ref"], "full_name"),
    }


def _label(doctype: str, reference: object, field: str) -> str | None:
    if not isinstance(reference, str) or not reference:
        return None
    try:
        value = frappe.get_value(doctype, reference, field)
    except Exception:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_mailbox_authority(team_ref: str, owner_ref: str) -> None:
    try:
        team_ok = frappe.db.exists(
            "GBOS Team",
            {
                "name": team_ref,
                "business_status": "Active",
                "review_status": "Approved",
            },
        )
        owner_ok = frappe.db.exists(
            "User",
            {"name": owner_ref, "enabled": 1, "user_type": "System User"},
        )
        member_ok = frappe.db.exists(
            "GBOS Team Member",
            {"parent": team_ref, "user": owner_ref, "enabled": 1},
        )
    except Exception as error:
        raise BFFError(
            "permission_denied",
            "Mailbox authority is unavailable",
            status=403,
        ) from error
    if not team_ok or not owner_ok or not member_ok:
        raise BFFError("permission_denied", "Mailbox authority is invalid", status=403)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def list_mailboxes(cursor: str | None = None, page_size: int | str = 25) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    payload = {
        **scope_payload(),
        "page_size": _integer(page_size, "page_size", maximum=50),
    }
    if value := _optional(cursor):
        payload["cursor"] = value
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-admin/mailboxes/list",
        purpose="email_mailbox_read",
        payload=payload,
    )
    rows = data.get("mailboxes")
    next_cursor = data.get("next_cursor")
    if not isinstance(rows, builtins.list) or not (
        next_cursor is None or isinstance(next_cursor, str)
    ):
        raise BFFError("internal_error", "Email Gateway mailbox list is invalid", status=503)
    try:
        mailboxes = [map_mailbox(_public_mailbox(row)) for row in rows]
    except (TypeError, V5EmailDTOValidationError) as error:
        raise BFFError(
            "internal_error", "Email Gateway mailbox list is invalid", status=503
        ) from error
    return v5_success(
        {"mailboxes": mailboxes, "next_cursor": next_cursor},
        next_cursor=next_cursor,
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def get_mailbox(mailbox_ref: str) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    reference = _optional(mailbox_ref)
    if reference is None:
        raise BFFError("invalid_query", "mailbox_ref is required")
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-admin/mailboxes/get",
        purpose="email_mailbox_read",
        payload={**scope_payload(), "mailbox_ref": reference},
    )
    return v5_success({"mailbox": _map_mailbox_response(data)})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def upsert_mailbox(
    canonical_mailbox_address: str,
    display_label: str,
    provider_kind: str,
    business_mode: str,
    business_purpose: str,
    provider_account_ref: str,
    observer_connector_instance_ref: str,
    default_team_ref: str,
    account_owner_user_ref: str,
    priority: int | str,
    credential_ref: str,
    inbound_enabled: bool | int | str,
    outbound_enabled: bool | int | str,
    expected_revision: int | str,
    idempotency_key: str,
    mailbox_ref: str | None = None,
) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    canonical_address = _safe_transient_text(
        canonical_mailbox_address,
        "canonical_mailbox_address",
        maximum=254,
    )
    team_ref = _safe_transient_text(default_team_ref, "default_team_ref", maximum=140)
    if _TEAM_REF.fullmatch(team_ref) is None:
        raise BFFError("invalid_dto", "default_team_ref is invalid")
    owner_ref = _safe_transient_text(
        account_owner_user_ref,
        "account_owner_user_ref",
        maximum=140,
    )
    safe_idempotency_key = _safe_transient_text(
        idempotency_key,
        "idempotency_key",
        maximum=256,
    )
    _require_mailbox_authority(team_ref, owner_ref)
    identity = call_observer(
        path="/internal/v1/bff/email-mailbox-identity/derive",
        purpose="email_mailbox_identity",
        payload={
            "canonical_mailbox_address": canonical_address,
            "idempotency_key": safe_idempotency_key,
        },
        idempotency_key=safe_idempotency_key,
    )
    if set(identity) != {"opaque_address_ref", "normalization_version"}:
        raise BFFError(
            "internal_error",
            "Observer returned an invalid mailbox identity response",
            status=503,
        )
    opaque_address_ref = identity.get("opaque_address_ref")
    if (
        not isinstance(opaque_address_ref, str)
        or _OPAQUE_MAILBOX_ADDRESS.fullmatch(opaque_address_ref) is None
        or identity.get("normalization_version") != "email-v1"
    ):
        raise BFFError(
            "internal_error",
            "Observer returned an invalid mailbox identity response",
            status=503,
        )
    command: dict[str, Any] = {
        "mailbox_address_identity_ref": opaque_address_ref,
        "display_label": display_label,
        "provider_kind": provider_kind,
        "business_mode": business_mode,
        "business_purpose": business_purpose,
        "provider_account_ref": provider_account_ref,
        "observer_connector_instance_ref": observer_connector_instance_ref,
        "default_team_ref": team_ref,
        "account_owner_user_ref": owner_ref,
        "priority": _integer(priority, "priority", maximum=1000),
        "credential_ref": credential_ref,
        "inbound_enabled": _form_boolean(inbound_enabled, "inbound_enabled"),
        "outbound_enabled": _form_boolean(outbound_enabled, "outbound_enabled"),
        "expected_revision": _integer(expected_revision, "expected_revision"),
        "idempotency_key": safe_idempotency_key,
    }
    if reference := _optional(mailbox_ref):
        command["mailbox_ref"] = reference
    try:
        command = validate_mailbox_upsert(command)
    except V5EmailDTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error
    return _mailbox_command("upsert", command)


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def set_mailbox_status(
    mailbox_ref: str,
    action: str,
    expected_revision: int | str,
    idempotency_key: str,
) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    try:
        command = validate_mailbox_status(
            {
                "mailbox_ref": mailbox_ref,
                "action": action,
                "expected_revision": _integer(expected_revision, "expected_revision"),
                "idempotency_key": idempotency_key,
            }
        )
    except V5EmailDTOValidationError as error:
        raise BFFError("invalid_dto", str(error)) from error
    return _mailbox_command("status", command)


def _mailbox_command(action: str, command: dict[str, Any]) -> dict[str, Any]:
    payload = {**scope_payload(), **command}

    def execute() -> dict[str, Any]:
        data = call_gateway(
            method="POST",
            path=f"/internal/v1/bff/email-admin/mailboxes/{action}",
            purpose="email_mailbox_admin",
            payload=payload,
            idempotency_key=command["idempotency_key"],
        )
        return _map_mailbox_response(data)

    result, replayed, original_request_id = run_idempotent(
        f"email_admin.{action}",
        command["idempotency_key"],
        payload,
        execute,
        api_version="v5",
    )
    return v5_success(
        {"mailbox": result},
        replayed=replayed,
        original_request_id=original_request_id,
    )


def _form_boolean(value: bool | int | str, field: str) -> bool:
    if value in (True, 1, "1", "true"):
        return True
    if value in (False, 0, "0", "false"):
        return False
    raise BFFError("invalid_dto", f"{field} must be a boolean")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def connector_health() -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-admin/connector-health/get",
        purpose="email_connector_health_read",
        payload=scope_payload(),
    )
    rows = data.get("connector_health")
    if not isinstance(rows, builtins.list):
        raise BFFError("internal_error", "Connector health response is invalid", status=503)
    try:
        health = [map_connector_health(row) for row in rows]
    except (TypeError, V5EmailDTOValidationError) as error:
        raise BFFError(
            "internal_error", "Connector health response is invalid", status=503
        ) from error
    return v5_success({"connector_health": health})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("GET")
def list_rules(page_size: int | str = 25) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    data = call_gateway(
        method="POST",
        path="/internal/v1/bff/email-admin/rules/list",
        purpose="email_mailbox_read",
        payload={
            **scope_payload(),
            "page_size": _integer(page_size, "page_size", maximum=50),
        },
    )
    rows = data.get("rules")
    if not isinstance(rows, builtins.list):
        raise BFFError("internal_error", "Routing rule response is invalid", status=503)
    return v5_success({"rules": [_public_rule(row) for row in rows]})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # type: ignore[untyped-decorator]
@bff_endpoint("POST")
def upsert_rule(
    team_ref: str,
    mailbox_ref: str,
    owner_user_ref: str,
    priority: int | str,
    enabled: bool | int | str,
    expected_revision: int | str,
    idempotency_key: str,
    rule_ref: str | None = None,
) -> dict[str, Any]:
    require_roles(EMAIL_ADMIN_ROLES)
    _require_mailbox_authority(team_ref, owner_user_ref)
    command: dict[str, Any] = {
        "team_ref": team_ref,
        "mailbox_ref": mailbox_ref,
        "owner_user_ref": owner_user_ref,
        "priority": _integer(priority, "priority", maximum=1000),
        "enabled": _form_boolean(enabled, "enabled"),
        "expected_revision": _integer(expected_revision, "expected_revision"),
        "idempotency_key": idempotency_key,
    }
    if reference := _optional(rule_ref):
        command["rule_ref"] = reference
    payload = {**scope_payload(), **command}

    def execute() -> dict[str, Any]:
        data = call_gateway(
            method="POST",
            path="/internal/v1/bff/email-admin/rules/upsert",
            purpose="email_mailbox_admin",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return _public_rule(data.get("rule"))

    result, replayed, original_request_id = run_idempotent(
        "email_admin.upsert_rule",
        idempotency_key,
        payload,
        execute,
        api_version="v5",
    )
    return v5_success(
        {"rule": result},
        replayed=replayed,
        original_request_id=original_request_id,
    )


def _public_rule(value: object) -> dict[str, Any]:
    fields = {
        "rule_ref",
        "team_ref",
        "mailbox_ref",
        "owner_user_ref",
        "priority",
        "revision",
        "enabled",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise BFFError("internal_error", "Routing rule response is invalid", status=503)
    return {
        "rule_ref": value["rule_ref"],
        "mailbox_ref": value["mailbox_ref"],
        "team_label": _label("GBOS Team", value["team_ref"], "team_name"),
        "owner_label": _label("User", value["owner_user_ref"], "full_name"),
        "priority": value["priority"],
        "revision": value["revision"],
        "enabled": value["enabled"],
    }
