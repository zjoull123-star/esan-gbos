from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import ValidationError

_MAILBOX_MODES = frozenset({"primary", "workflow", "migration", "selective_archive"})
_PROVIDERS = frozenset({"fake", "imap_smtp", "wecom_app_mail"})
_MAILBOX_STATES = frozenset({"draft", "active", "paused", "revoked", "error"})
_INBOX_STATES = frozenset({"identity_pending", "unassigned"})
_IDENTITY_STATES = frozenset({"unknown", "confirmed", "revoked"})
_HEALTH_STATES = frozenset({"healthy", "degraded", "paused", "revoked", "unknown"})
_FRESHNESS_STATES = frozenset({"fresh", "stale", "unknown"})
_MAX_CURSOR_BYTES = 512


def _text(value: str, name: str, *, maximum: int = 240) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"invalid {name}")
    return value


def _optional_text(value: str | None, name: str, *, maximum: int = 240) -> str | None:
    return None if value is None else _text(value, name, maximum=maximum)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"invalid {name}")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Phase1Mailbox:
    mailbox_ref: str
    display_label: str
    provider_kind: str
    business_mode: str
    business_purpose: str
    default_team_ref: str
    account_owner_user_ref: str
    inbound_enabled: bool
    outbound_enabled: bool
    status: str
    config_revision: int
    site_id: str = "alpha.example"
    observer_connector_instance_ref: str | None = None

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.site_id, "site id", 140),
            (self.mailbox_ref, "mailbox ref", 140),
            (self.display_label, "display label", 240),
            (self.business_purpose, "business purpose", 80),
            (self.default_team_ref, "default team ref", 140),
            (self.account_owner_user_ref, "account owner user ref", 140),
        ):
            _text(value, name, maximum=maximum)
        if self.observer_connector_instance_ref is not None:
            _text(
                self.observer_connector_instance_ref,
                "observer connector instance ref",
                maximum=256,
            )
        if self.provider_kind not in _PROVIDERS or self.business_mode not in _MAILBOX_MODES:
            raise ValidationError("invalid mailbox projection")
        if self.status not in _MAILBOX_STATES:
            raise ValidationError("invalid mailbox status")
        if not isinstance(self.inbound_enabled, bool) or self.outbound_enabled is not False:
            raise ValidationError("invalid mailbox switch")
        if (
            not isinstance(self.config_revision, int)
            or isinstance(self.config_revision, bool)
            or self.config_revision < 1
        ):
            raise ValidationError("invalid mailbox revision")

    def to_wire(self) -> dict[str, object]:
        return {
            "mailbox_ref": self.mailbox_ref,
            "display_label": self.display_label,
            "provider_kind": self.provider_kind,
            "business_mode": self.business_mode,
            "business_purpose": self.business_purpose,
            "default_team_ref": self.default_team_ref,
            "account_owner_user_ref": self.account_owner_user_ref,
            "inbound_enabled": self.inbound_enabled,
            "outbound_enabled": False,
            "status": self.status,
            "config_revision": self.config_revision,
        }


@dataclass(frozen=True, slots=True)
class Phase1InboxItem:
    inbox_item_ref: str
    mailbox_label: str
    mailbox_role: str
    received_at: datetime
    state: str
    safe_summary: str
    team_ref: str
    assignee_user_ref: str | None
    identity_state: str
    revision: int
    site_id: str = "alpha.example"

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.site_id, "site id", 140),
            (self.inbox_item_ref, "inbox item ref", 140),
            (self.mailbox_label, "mailbox label", 240),
            (self.safe_summary, "safe summary", 500),
            (self.team_ref, "team ref", 140),
        ):
            _text(value, name, maximum=maximum)
        _optional_text(self.assignee_user_ref, "assignee user ref", maximum=140)
        _aware(self.received_at, "received at")
        if self.mailbox_role not in _MAILBOX_MODES:
            raise ValidationError("invalid mailbox role")
        if self.state not in _INBOX_STATES or self.identity_state not in _IDENTITY_STATES:
            raise ValidationError("invalid inbox projection")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValidationError("invalid inbox revision")

    def to_wire(self) -> dict[str, object]:
        return {
            "inbox_item_ref": self.inbox_item_ref,
            "mailbox_label": self.mailbox_label,
            "mailbox_role": self.mailbox_role,
            "received_at": _iso(self.received_at),
            "state": self.state,
            "safe_summary": self.safe_summary,
            "team_ref": self.team_ref,
            "assignee_user_ref": self.assignee_user_ref,
            "identity_state": self.identity_state,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    mailbox_ref: str
    mailbox_label: str
    status: str
    freshness: str
    backlog: int
    last_success_at: datetime | None
    safe_error_code: str | None

    def __post_init__(self) -> None:
        _text(self.mailbox_ref, "mailbox ref", maximum=140)
        _text(self.mailbox_label, "mailbox label")
        _optional_text(self.safe_error_code, "safe error code", maximum=80)
        if self.status not in _HEALTH_STATES or self.freshness not in _FRESHNESS_STATES:
            raise ValidationError("invalid connector health")
        if not isinstance(self.backlog, int) or isinstance(self.backlog, bool) or self.backlog < 0:
            raise ValidationError("invalid connector backlog")
        if self.last_success_at is not None:
            _aware(self.last_success_at, "last success at")

    def to_wire(self) -> dict[str, object]:
        return {
            "mailbox_ref": self.mailbox_ref,
            "mailbox_label": self.mailbox_label,
            "status": self.status,
            "freshness": self.freshness,
            "backlog": self.backlog,
            "last_success_at": (
                None if self.last_success_at is None else _iso(self.last_success_at)
            ),
            "safe_error_code": self.safe_error_code,
        }


def encode_cursor(kind: str, *values: str) -> str:
    raw = json.dumps([kind, *values], separators=(",", ":"), ensure_ascii=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(encoded) > _MAX_CURSOR_BYTES:
        raise ValidationError("invalid cursor")
    return encoded


def decode_cursor(cursor: str, kind: str, value_count: int) -> tuple[str, ...]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_BYTES:
        raise ValidationError("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        value = json.loads(raw)
    except ValueError, UnicodeDecodeError, json.JSONDecodeError:
        raise ValidationError("invalid cursor") from None
    if (
        not isinstance(value, list)
        or len(value) != value_count + 1
        or value[0] != kind
        or not all(isinstance(item, str) and item for item in value[1:])
    ):
        raise ValidationError("invalid cursor")
    return tuple(value[1:])


__all__ = [
    "ConnectorHealth",
    "Page",
    "Phase1InboxItem",
    "Phase1Mailbox",
    "decode_cursor",
    "encode_cursor",
]
