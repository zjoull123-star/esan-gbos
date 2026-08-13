"""Short-lived, content-free authorization receipts for BFF follow-up calls."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import canonical_digest, stable_ref

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_PREFIXED_REF = re.compile(r"^(EGR|PUB|INB|MSG|MBX|DLV)-[0-9A-HJKMNP-TV-Z]{26}$")
PARTICIPANT_AUTHORITY_BINDING_FIELDS = frozenset(
    {
        "gateway_receipt_ref",
        "publication_ref",
        "inbox_item_ref",
        "message_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_delivery_ref",
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
    }
)
_TTL = timedelta(minutes=5)
_COMMAND_AUTH_REF = "email-command-ingest-v1"
_COMMAND_AUDIENCE = "email-command-executor"
_COMMAND_SCOPE = "email-send-execute"


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandIngestAuthorization:
    """Exact scoped bearer boundary for the command executor endpoint."""

    bearer_token: str
    auth_ref: str = _COMMAND_AUTH_REF

    def __post_init__(self) -> None:
        if (
            not self.bearer_token
            or self.bearer_token != self.bearer_token.strip()
            or len(self.bearer_token) > 4096
            or self.auth_ref != _COMMAND_AUTH_REF
        ):
            raise ValueError("invalid command ingest credentials")

    def authorize(
        self,
        *,
        authorization: str | None,
        auth_ref: str | None,
        audience: str | None,
        granted_scope: str | None,
    ) -> bool:
        return bool(
            authorization is not None
            and hmac.compare_digest(authorization, f"Bearer {self.bearer_token}")
            and auth_ref is not None
            and hmac.compare_digest(auth_ref, self.auth_ref)
            and audience == _COMMAND_AUDIENCE
            and granted_scope == _COMMAND_SCOPE
        )


class GatewayAuthorizationIssuer:
    """Issues bounded receipts; transport authentication remains a separate concern."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock

    def __repr__(self) -> str:
        return "GatewayAuthorizationIssuer(clock=<redacted>)"

    def issue_draft(
        self,
        *,
        site_id: str,
        actor_ref: str,
        team_ref: str,
        inbox_item_ref: str,
        draft_ref: str,
        draft_revision: int,
        request_digest: str,
        participant_authority_binding: Mapping[str, object],
        participant_roles_digest: str,
    ) -> dict[str, object]:
        issued_at = _now(self._clock)
        for value, field in (
            (site_id, "site_id"),
            (actor_ref, "actor_ref"),
            (team_ref, "team_ref"),
            (inbox_item_ref, "inbox_item_ref"),
            (draft_ref, "draft_ref"),
        ):
            _bounded(value, field)
        if (
            isinstance(draft_revision, bool)
            or not isinstance(draft_revision, int)
            or draft_revision < 1
            or _DIGEST.fullmatch(request_digest) is None
            or _DIGEST.fullmatch(participant_roles_digest) is None
        ):
            raise ValueError("invalid draft authorization binding")
        binding = validate_participant_authority_binding(
            participant_authority_binding,
            inbox_item_ref=inbox_item_ref,
        )
        return {
            "receipt_ref": stable_ref(
                "DAR",
                site_id,
                inbox_item_ref,
                draft_ref,
                str(draft_revision),
                request_digest,
                canonical_digest(binding),
                participant_roles_digest,
                issued_at.isoformat(),
            ),
            "site_id": site_id,
            "purpose": "email_draft_material",
            "inbox_item_ref": inbox_item_ref,
            "draft_ref": draft_ref,
            "draft_revision": draft_revision,
            "actor_ref": actor_ref,
            "team_ref": team_ref,
            "request_digest": request_digest,
            **binding,
            "participant_roles_digest": participant_roles_digest,
            "issued_at": _wire_time(issued_at),
            "expires_at": _wire_time(issued_at + _TTL),
        }

    def issue_evidence(
        self,
        *,
        site_id: str,
        actor_ref: str,
        team_ref: str,
        inbox_item_ref: str,
        evidence_ref: str,
    ) -> dict[str, object]:
        issued_at = _now(self._clock)
        for value, field in (
            (site_id, "site_id"),
            (actor_ref, "actor_ref"),
            (team_ref, "team_ref"),
            (inbox_item_ref, "inbox_item_ref"),
            (evidence_ref, "evidence_ref"),
        ):
            _bounded(value, field)
        return {
            "receipt_ref": stable_ref(
                "EAR", site_id, inbox_item_ref, evidence_ref, actor_ref, issued_at.isoformat()
            ),
            "site_id": site_id,
            "purpose": "email_evidence_reveal",
            "inbox_item_ref": inbox_item_ref,
            "evidence_ref": evidence_ref,
            "actor_ref": actor_ref,
            "team_ref": team_ref,
            "issued_at": _wire_time(issued_at),
            "expires_at": _wire_time(issued_at + _TTL),
        }


def validate_participant_authority_binding(
    value: object,
    *,
    inbox_item_ref: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != PARTICIPANT_AUTHORITY_BINDING_FIELDS:
        raise ValueError("invalid participant authority binding")
    result = dict(value)
    for field, prefix in (
        ("gateway_receipt_ref", "EGR"),
        ("publication_ref", "PUB"),
        ("inbox_item_ref", "INB"),
        ("message_ref", "MSG"),
        ("mailbox_ref", "MBX"),
        ("observer_delivery_ref", "DLV"),
    ):
        candidate = result.get(field)
        matched = _PREFIXED_REF.fullmatch(candidate) if isinstance(candidate, str) else None
        if matched is None or matched.group(1) != prefix:
            raise ValueError("invalid participant authority binding")
    revision = result.get("mailbox_config_revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= 2_147_483_647
    ):
        raise ValueError("invalid participant authority binding")
    for field in (
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
    ):
        digest = result.get(field)
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("invalid participant authority binding")
    if result["inbox_item_ref"] != inbox_item_ref:
        raise ValueError("participant authority inbox drift")
    return result


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("authorization clock must be timezone-aware")
    return value.astimezone(UTC)


def _wire_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bounded(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {field}")
    return value


__all__ = [
    "PARTICIPANT_AUTHORITY_BINDING_FIELDS",
    "CommandIngestAuthorization",
    "GatewayAuthorizationIssuer",
    "validate_participant_authority_binding",
]
