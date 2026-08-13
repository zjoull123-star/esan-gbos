"""Provider-neutral draft and final MIME materialization in Observer CAS."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Protocol

from .models import TenantScope

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_RECEIPT_FIELDS = frozenset(
    {
        "receipt_ref",
        "site_id",
        "purpose",
        "inbox_item_ref",
        "draft_ref",
        "draft_revision",
        "actor_ref",
        "team_ref",
        "request_digest",
        "issued_at",
        "expires_at",
    }
)
_ROLE_NAMES = frozenset(
    {"mailbox_owner", "original_sender", "original_to", "original_cc", "assigned_owner"}
)
_MAX_DRAFT_BYTES = 131_072


class CasStore(Protocol):
    def put(self, scope: TenantScope, content: bytes, *, media_type: str) -> Any: ...

    def read(self, scope: TenantScope, object_ref: str) -> bytes: ...


ParticipantResolver = Callable[[TenantScope, str, Mapping[str, object]], Mapping[str, object]]


@dataclass(frozen=True, slots=True, repr=False)
class DraftAuthorizationReceipt:
    receipt_ref: str
    site_id: str
    purpose: str
    inbox_item_ref: str
    draft_ref: str
    draft_revision: int
    actor_ref: str
    team_ref: str
    request_digest: str
    issued_at: datetime
    expires_at: datetime

    @classmethod
    def from_wire(cls, value: object) -> DraftAuthorizationReceipt:
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
            raise ValueError("invalid draft authorization receipt")
        revision = value.get("draft_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("invalid draft authorization revision")
        digest = value.get("request_digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("invalid draft authorization digest")
        issued_at = _time(value.get("issued_at"), "issued_at")
        expires_at = _time(value.get("expires_at"), "expires_at")
        if expires_at <= issued_at or (expires_at - issued_at).total_seconds() > 300:
            raise ValueError("invalid draft authorization lifetime")
        fields = {
            name: _text(value.get(name), name)
            for name in (
                "receipt_ref",
                "site_id",
                "purpose",
                "inbox_item_ref",
                "draft_ref",
                "actor_ref",
                "team_ref",
            )
        }
        if fields["purpose"] != "email_draft_material":
            raise PermissionError("draft authorization purpose mismatch")
        return cls(
            **fields,
            draft_revision=revision,
            request_digest=digest,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def __repr__(self) -> str:
        return (
            "DraftAuthorizationReceipt("
            f"receipt_ref={self.receipt_ref!r}, site_id={self.site_id!r}, "
            f"purpose={self.purpose!r}, inbox_item_ref={self.inbox_item_ref!r}, "
            f"draft_ref={self.draft_ref!r}, draft_revision={self.draft_revision}, "
            "actor_ref=<redacted>, team_ref=<redacted>, request_digest=<redacted>, "
            f"issued_at={self.issued_at!r}, expires_at={self.expires_at!r})"
        )


class EmailDraftMaterialService:
    def __init__(
        self,
        *,
        store: CasStore,
        participant_resolver: ParticipantResolver,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(participant_resolver) or not callable(clock):
            raise TypeError("draft material dependencies must be callable")
        self._store = store
        self._participant_resolver = participant_resolver
        self._clock = clock
        self._replays: dict[str, tuple[str, dict[str, object]]] = {}

    def __repr__(self) -> str:
        return "EmailDraftMaterialService(store=<redacted>, resolver=<redacted>)"

    @staticmethod
    def digest_text(content: str) -> str:
        if not isinstance(content, str):
            raise TypeError("draft content must be text")
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()

    def save(
        self,
        scope: TenantScope,
        *,
        authorization: DraftAuthorizationReceipt,
        content: str,
        content_digest: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        self._authorize(scope, authorization)
        _text(idempotency_key, "idempotency_key")
        if not isinstance(content, str):
            raise ValueError("draft content must be UTF-8 text")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > _MAX_DRAFT_BYTES:
            raise ValueError("draft content is outside the size budget")
        digest = self.digest_text(content)
        if (
            _DIGEST.fullmatch(content_digest) is None
            or digest != content_digest
            or digest != authorization.request_digest
        ):
            raise ValueError("draft content digest drift")
        replay = self._replay(idempotency_key, digest)
        if replay is not None:
            return replay
        stored = self._store.put(scope, encoded, media_type="text/plain; charset=utf-8")
        result: dict[str, object] = {
            "evidence_ref": str(stored.object_ref),
            "digest": digest,
            "revision": authorization.draft_revision,
        }
        self._replays[idempotency_key] = (digest, result)
        return dict(result)

    def finalize(
        self,
        scope: TenantScope,
        *,
        authorization: DraftAuthorizationReceipt,
        draft_evidence_ref: str,
        draft_digest: str,
        draft_revision: int,
        participant_roles: Mapping[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        self._authorize(scope, authorization)
        _text(draft_evidence_ref, "draft_evidence_ref", maximum=512)
        _text(idempotency_key, "idempotency_key")
        if draft_revision != authorization.draft_revision or draft_digest != (
            authorization.request_digest
        ):
            raise ValueError("draft revision or digest drift")
        roles = _participant_roles(participant_roles)
        material = self._store.read(scope, draft_evidence_ref)
        actual_digest = "sha256:" + hashlib.sha256(material).hexdigest()
        if actual_digest != draft_digest or len(material) > _MAX_DRAFT_BYTES:
            raise ValueError("draft evidence integrity drift")
        replay_digest = _json_digest(
            {
                "draft_evidence_ref": draft_evidence_ref,
                "draft_digest": draft_digest,
                "draft_revision": draft_revision,
                "participant_roles": roles,
            }
        )
        replay = self._replay(idempotency_key, replay_digest)
        if replay is not None:
            return replay
        resolved = self._participant_resolver(scope, authorization.inbox_item_ref, roles)
        sender, recipients, cc, subject = _resolved_participants(resolved, roles)
        try:
            body = material.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("draft evidence is not UTF-8") from None
        message = EmailMessage(policy=SMTP)
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Subject"] = subject
        message.set_content(body, charset="utf-8")
        final_bytes = message.as_bytes(policy=SMTP)
        stored = self._store.put(scope, final_bytes, media_type="message/rfc822")
        final_digest = "sha256:" + hashlib.sha256(final_bytes).hexdigest()
        role_binding = _json_digest(roles)
        result: dict[str, object] = {
            "evidence_ref": str(stored.object_ref),
            "digest": final_digest,
            "role_binding": role_binding,
        }
        self._replays[idempotency_key] = (replay_digest, result)
        return dict(result)

    def _authorize(self, scope: TenantScope, authorization: DraftAuthorizationReceipt) -> None:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("draft material clock must be timezone-aware")
        normalized = now.astimezone(UTC)
        if authorization.site_id != scope.site_id:
            raise PermissionError("draft authorization site mismatch")
        if not authorization.issued_at <= normalized <= authorization.expires_at:
            raise PermissionError("draft authorization is stale")

    def _replay(self, key: str, digest: str) -> dict[str, object] | None:
        replay = self._replays.get(key)
        if replay is None:
            return None
        if replay[0] != digest:
            raise ValueError("draft material replay drift")
        return dict(replay[1])


def _participant_roles(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"sender", "recipients"}:
        raise ValueError("participant roles must use the closed opaque shape")
    sender = value.get("sender")
    recipients = value.get("recipients")
    if sender not in _ROLE_NAMES or (
        not isinstance(recipients, list)
        or not recipients
        or len(recipients) > 20
        or len(recipients) != len(set(recipients))
        or any(item not in _ROLE_NAMES for item in recipients)
    ):
        raise ValueError("invalid opaque participant roles")
    return {"sender": sender, "recipients": list(recipients)}


def _resolved_participants(
    value: Mapping[str, object], roles: Mapping[str, object]
) -> tuple[str, list[str], list[str], str]:
    if not isinstance(value, Mapping) or not set(value).issubset(
        {"from", "to", "cc", "subject", "roles"}
    ):
        raise PermissionError("participant authority is invalid")
    if value.get("roles") != roles:
        raise PermissionError("participant role binding mismatch")
    sender = _address(value.get("from"), "from")
    recipients = _addresses(value.get("to"), "to")
    cc = [] if value.get("cc") is None else _addresses(value.get("cc"), "cc")
    subject = "Re: governed inbox message"
    if value.get("subject") is not None:
        subject = _text(value.get("subject"), "subject", maximum=240)
    return sender, recipients, cc, subject


def _address(value: object, field: str) -> str:
    text = _text(value, field, maximum=320)
    if "@" not in text or any(character in text for character in "\r\n"):
        raise PermissionError("participant authority address is invalid")
    return text


def _addresses(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise PermissionError("participant authority addresses are invalid")
    return [_address(item, field) for item in value]


def _text(value: object, field: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {field}")
    return value


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"invalid {field}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid {field}") from None
    if result.tzinfo is None:
        raise ValueError(f"invalid {field}")
    return result.astimezone(UTC)


def _json_digest(value: object) -> str:
    import json

    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["DraftAuthorizationReceipt", "EmailDraftMaterialService"]
