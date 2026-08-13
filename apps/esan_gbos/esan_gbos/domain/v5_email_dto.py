from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


class V5EmailDTOValidationError(ValueError):
    """An Email Gateway value did not match the closed v5 projection."""


_PROVIDER_KINDS = frozenset({"fake", "imap_smtp", "wecom_app_mail"})
_BUSINESS_MODES = frozenset({"primary", "selective_archive", "migration"})
_MAILBOX_STATES = frozenset({"draft", "active", "paused", "revoked", "error"})
_MAILBOX_ACTIONS = frozenset({"enable", "pause", "revoke"})
_INBOX_STATES = frozenset(
    {
        "identity_pending",
        "unassigned",
        "assigned",
        "draft",
        "waiting_internal",
        "waiting_customer",
        "converted",
        "closed",
        "quarantined",
        "send_queued",
        "send_uncertain",
    }
)
_IDENTITY_STATES = frozenset({"unknown", "confirmed", "revoked"})
_HEALTH_STATES = frozenset({"healthy", "degraded", "paused", "revoked", "unknown"})
_FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown"})
_BUSINESS_PURPOSES = frozenset(
    {
        "business_operations",
        "observation_processing",
        "entity_resolution",
        "customer_service",
        "sales_follow_up",
        "procurement_coordination",
        "product_sample_management",
        "risk_review",
        "metric_reporting",
        "audit_compliance",
    }
)
_TEAM_REF = re.compile(r"^TEM-[0-9A-HJKMNP-TV-Z]{26}$")
_CONNECTOR_REF = re.compile(r"^OCI-[0-9A-HJKMNP-TV-Z]{26}$")
_CREDENTIAL_REF = re.compile(r"^secretref:v1/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MAILBOX_ADDRESS_IDENTITY_REF = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_MAILBOX_REF = re.compile(r"^MBX-[0-9A-HJKMNP-TV-Z]{26}$")
_SLA_POLICY_REF = re.compile(r"^SLA-[0-9A-HJKMNP-TV-Z]{26}$")
_RFC3339 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,9})?(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise V5EmailDTOValidationError(f"{field} must be an object")
    return value


def _closed(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    keys = set(value)
    optional = optional or set()
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise V5EmailDTOValidationError(f"missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise V5EmailDTOValidationError(f"unexpected fields: {', '.join(sorted(unexpected))}")


def _text(value: object, field: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise V5EmailDTOValidationError(f"{field} must be bounded text")
    return value.strip()


def _optional_text(value: object, field: str, *, maximum: int = 240) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=maximum)


def _integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise V5EmailDTOValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise V5EmailDTOValidationError(f"{field} must be a boolean")
    return value


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    text = _text(value, field, maximum=64)
    if text not in choices:
        raise V5EmailDTOValidationError(f"{field} is not allowed")
    return text


def _matching_text(
    value: object,
    field: str,
    pattern: re.Pattern[str],
    *,
    maximum: int = 256,
) -> str:
    text = _text(value, field, maximum=maximum)
    if pattern.fullmatch(text) is None:
        raise V5EmailDTOValidationError(f"{field} is invalid")
    return text


def _strict_text(value: object, field: str, *, minimum: int = 1, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise V5EmailDTOValidationError(f"{field} must be bounded text")
    return value


def _rfc3339_utc(value: object, field: str) -> str:
    text = _strict_text(value, field, minimum=20, maximum=35)
    match = _RFC3339.fullmatch(text)
    if match is None or match.group("zone") == "-00:00":
        raise V5EmailDTOValidationError(f"{field} must be timezone-aware RFC3339")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if parsed.utcoffset() is None:
            raise ValueError("timezone missing")
        normalized = parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise V5EmailDTOValidationError(f"{field} must be timezone-aware RFC3339") from error
    return normalized.strftime("%Y-%m-%dT%H:%M:%S") + (match.group("fraction") or "") + "Z"


def map_sla_policy(value: object) -> dict[str, Any]:
    item = _object(value, "SLA policy")
    required = {
        "policy_ref",
        "revision",
        "first_response_duration_seconds",
        "effective_at",
    }
    _closed(item, required)
    return {
        "policy_ref": _matching_text(item["policy_ref"], "policy_ref", _SLA_POLICY_REF, maximum=30),
        "revision": _integer(item["revision"], "revision", minimum=1),
        "first_response_duration_seconds": _integer(
            item["first_response_duration_seconds"],
            "first_response_duration_seconds",
            minimum=60,
            maximum=604800,
        ),
        "effective_at": _rfc3339_utc(item["effective_at"], "effective_at"),
    }


def map_sla_policy_result(value: object) -> dict[str, Any]:
    item = _object(value, "SLA policy result")
    policy_fields = {
        "policy_ref",
        "revision",
        "first_response_duration_seconds",
        "effective_at",
    }
    _closed(item, policy_fields | {"mailbox_ref"})
    return {
        "mailbox_ref": _matching_text(item["mailbox_ref"], "mailbox_ref", _MAILBOX_REF, maximum=30),
        **map_sla_policy({field: item[field] for field in policy_fields}),
    }


def validate_sla_policy_upsert(value: object) -> dict[str, Any]:
    item = _object(value, "SLA policy command")
    required = {
        "mailbox_ref",
        "first_response_duration_seconds",
        "effective_at",
        "expected_revision",
        "idempotency_key",
    }
    _closed(item, required)
    return {
        "mailbox_ref": _matching_text(item["mailbox_ref"], "mailbox_ref", _MAILBOX_REF, maximum=30),
        "first_response_duration_seconds": _integer(
            item["first_response_duration_seconds"],
            "first_response_duration_seconds",
            minimum=60,
            maximum=604800,
        ),
        "effective_at": _rfc3339_utc(item["effective_at"], "effective_at"),
        "expected_revision": _integer(item["expected_revision"], "expected_revision"),
        "idempotency_key": _strict_text(
            item["idempotency_key"], "idempotency_key", minimum=8, maximum=256
        ),
    }


def map_mailbox(value: object) -> dict[str, Any]:
    item = _object(value, "mailbox")
    required = {
        "mailbox_ref",
        "display_label",
        "provider_kind",
        "business_mode",
        "business_purpose",
        "default_team_label",
        "account_owner_label",
        "inbound_enabled",
        "outbound_enabled",
        "status",
        "config_revision",
    }
    _closed(item, required)
    outbound = _boolean(item["outbound_enabled"], "outbound_enabled")
    if outbound:
        raise V5EmailDTOValidationError("outbound_enabled must remain false in Phase 1")
    return {
        "mailbox_ref": _text(item["mailbox_ref"], "mailbox_ref", maximum=140),
        "display_label": _text(item["display_label"], "display_label"),
        "provider_kind": _choice(item["provider_kind"], "provider_kind", _PROVIDER_KINDS),
        "business_mode": _choice(item["business_mode"], "business_mode", _BUSINESS_MODES),
        "business_purpose": _text(item["business_purpose"], "business_purpose", maximum=80),
        "default_team_label": _optional_text(item["default_team_label"], "default_team_label"),
        "account_owner_label": _optional_text(item["account_owner_label"], "account_owner_label"),
        "inbound_enabled": _boolean(item["inbound_enabled"], "inbound_enabled"),
        "outbound_enabled": False,
        "status": _choice(item["status"], "status", _MAILBOX_STATES),
        "config_revision": _integer(item["config_revision"], "config_revision"),
    }


def map_inbox_item(value: object) -> dict[str, Any]:
    item = _object(value, "inbox item")
    required = {
        "inbox_item_ref",
        "mailbox_label",
        "mailbox_role",
        "received_at",
        "state",
        "safe_summary",
        "team_label",
        "revision",
    }
    _closed(item, required)
    return {
        "inbox_item_ref": _text(item["inbox_item_ref"], "inbox_item_ref", maximum=140),
        "mailbox_label": _text(item["mailbox_label"], "mailbox_label"),
        "mailbox_role": _choice(item["mailbox_role"], "mailbox_role", _BUSINESS_MODES),
        "received_at": _text(item["received_at"], "received_at", maximum=64),
        "state": _choice(item["state"], "state", _INBOX_STATES),
        "safe_summary": _text(item["safe_summary"], "safe_summary", maximum=500),
        "team_label": _optional_text(item["team_label"], "team_label"),
        "revision": _integer(item["revision"], "revision"),
    }


def map_inbox_detail(value: object) -> dict[str, Any]:
    item = _object(value, "inbox detail")
    summary_fields = {
        "inbox_item_ref",
        "mailbox_label",
        "mailbox_role",
        "received_at",
        "state",
        "safe_summary",
        "team_label",
        "revision",
    }
    _closed(item, summary_fields | {"assignee_label", "identity_state"})
    summary = map_inbox_item({key: item[key] for key in summary_fields})
    return {
        **summary,
        "assignee_label": _optional_text(item["assignee_label"], "assignee_label"),
        "identity_state": _choice(item["identity_state"], "identity_state", _IDENTITY_STATES),
    }


def map_connector_health(value: object) -> dict[str, Any]:
    item = _object(value, "connector health")
    required = {
        "mailbox_ref",
        "mailbox_label",
        "status",
        "freshness",
        "backlog",
        "last_success_at",
        "safe_error_code",
    }
    _closed(item, required)
    return {
        "mailbox_ref": _text(item["mailbox_ref"], "mailbox_ref", maximum=140),
        "mailbox_label": _text(item["mailbox_label"], "mailbox_label"),
        "status": _choice(item["status"], "status", _HEALTH_STATES),
        "freshness": _choice(item["freshness"], "freshness", _FRESHNESS_STATES),
        "backlog": _integer(item["backlog"], "backlog"),
        "last_success_at": _optional_text(item["last_success_at"], "last_success_at", maximum=64),
        "safe_error_code": _optional_text(item["safe_error_code"], "safe_error_code", maximum=80),
    }


def validate_mailbox_upsert(value: object) -> dict[str, Any]:
    item = _object(value, "mailbox command")
    required = {
        "mailbox_address_identity_ref",
        "display_label",
        "provider_kind",
        "business_mode",
        "business_purpose",
        "provider_account_ref",
        "observer_connector_instance_ref",
        "default_team_ref",
        "account_owner_user_ref",
        "priority",
        "credential_ref",
        "inbound_enabled",
        "outbound_enabled",
        "expected_revision",
        "idempotency_key",
    }
    _closed(item, required, {"mailbox_ref"})
    outbound = _boolean(item["outbound_enabled"], "outbound_enabled")
    if outbound:
        raise V5EmailDTOValidationError("outbound_enabled must remain false in Phase 1")
    result: dict[str, Any] = {
        "mailbox_address_identity_ref": _matching_text(
            item["mailbox_address_identity_ref"],
            "mailbox_address_identity_ref",
            _MAILBOX_ADDRESS_IDENTITY_REF,
            maximum=58,
        ),
        "display_label": _text(item["display_label"], "display_label"),
        "provider_kind": _choice(item["provider_kind"], "provider_kind", _PROVIDER_KINDS),
        "business_mode": _choice(item["business_mode"], "business_mode", _BUSINESS_MODES),
        "business_purpose": _choice(
            item["business_purpose"], "business_purpose", _BUSINESS_PURPOSES
        ),
        "provider_account_ref": _text(
            item["provider_account_ref"], "provider_account_ref", maximum=256
        ),
        "observer_connector_instance_ref": _matching_text(
            item["observer_connector_instance_ref"],
            "observer_connector_instance_ref",
            _CONNECTOR_REF,
        ),
        "default_team_ref": _matching_text(
            item["default_team_ref"], "default_team_ref", _TEAM_REF, maximum=140
        ),
        "account_owner_user_ref": _text(
            item["account_owner_user_ref"], "account_owner_user_ref", maximum=140
        ),
        "priority": _integer(item["priority"], "priority", maximum=1000),
        "credential_ref": _matching_text(
            item["credential_ref"], "credential_ref", _CREDENTIAL_REF, maximum=128
        ),
        "inbound_enabled": _boolean(item["inbound_enabled"], "inbound_enabled"),
        "outbound_enabled": False,
        "expected_revision": _integer(item["expected_revision"], "expected_revision"),
        "idempotency_key": _text(item["idempotency_key"], "idempotency_key", maximum=256),
    }
    if "mailbox_ref" in item:
        result["mailbox_ref"] = _text(item["mailbox_ref"], "mailbox_ref", maximum=140)
    return result


def validate_mailbox_status(value: object) -> dict[str, Any]:
    item = _object(value, "mailbox status command")
    required = {"mailbox_ref", "action", "expected_revision", "idempotency_key"}
    _closed(item, required)
    return {
        "mailbox_ref": _text(item["mailbox_ref"], "mailbox_ref", maximum=140),
        "action": _choice(item["action"], "action", _MAILBOX_ACTIONS),
        "expected_revision": _integer(item["expected_revision"], "expected_revision"),
        "idempotency_key": _text(item["idempotency_key"], "idempotency_key", maximum=256),
    }
