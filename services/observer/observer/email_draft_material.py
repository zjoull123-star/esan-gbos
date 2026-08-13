"""Provider-neutral draft and final MIME materialization in Observer CAS."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Protocol

from .email_draft_material_repository import EmailDraftMaterialRepository
from .email_participant_authority import (
    EmailParticipantAuthorityBinding,
    canonical_binding_digest,
)
from .models import TenantScope, stable_ulid

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPAQUE_EMAIL = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
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
        "gateway_receipt_ref",
        "publication_ref",
        "message_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_delivery_ref",
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
        "participant_roles_digest",
        "issued_at",
        "expires_at",
    }
)
_ROLE_NAMES = frozenset(
    {"mailbox_owner", "original_sender", "original_to", "original_cc", "assigned_owner"}
)
_MAX_DRAFT_BYTES = 131_072
_MAX_FINAL_MIME_BYTES = 262_144
_PURPOSE = "email_draft_material"


class CasStore(Protocol):
    def put(self, scope: TenantScope, content: bytes, *, media_type: str) -> Any: ...

    def read(self, scope: TenantScope, object_ref: str) -> bytes: ...


ParticipantResolver = Callable[
    [TenantScope, "DraftAuthorizationReceipt", Mapping[str, object]],
    Mapping[str, object],
]


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
    gateway_receipt_ref: str
    publication_ref: str
    message_ref: str
    mailbox_ref: str
    mailbox_config_revision: int
    observer_delivery_ref: str
    payload_digest: str
    participant_binding_digest: str
    evidence_binding_digest: str
    participant_roles_digest: str
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
        role_digest = value.get("participant_roles_digest")
        if not isinstance(role_digest, str) or _DIGEST.fullmatch(role_digest) is None:
            raise ValueError("invalid participant roles digest")
        binding = EmailParticipantAuthorityBinding.from_wire(
            {
                field: value.get(field)
                for field in (
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
                )
            }
        )
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
            gateway_receipt_ref=binding.gateway_receipt_ref,
            publication_ref=binding.publication_ref,
            message_ref=binding.message_ref,
            mailbox_ref=binding.mailbox_ref,
            mailbox_config_revision=binding.mailbox_config_revision,
            observer_delivery_ref=binding.observer_delivery_ref,
            payload_digest=binding.payload_digest,
            participant_binding_digest=binding.participant_binding_digest,
            evidence_binding_digest=binding.evidence_binding_digest,
            participant_roles_digest=role_digest,
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
            f"publication_ref={self.publication_ref!r}, message_ref={self.message_ref!r}, "
            f"mailbox_ref={self.mailbox_ref!r}, "
            f"mailbox_config_revision={self.mailbox_config_revision}, "
            "gateway_receipt_ref=<redacted>, observer_delivery_ref=<redacted>, "
            "payload_digest=<redacted>, participant_binding_digest=<redacted>, "
            "evidence_binding_digest=<redacted>, participant_roles_digest=<redacted>, "
            f"issued_at={self.issued_at!r}, expires_at={self.expires_at!r})"
        )


class EmailDraftMaterialService:
    def __init__(
        self,
        *,
        store: CasStore,
        repository: EmailDraftMaterialRepository,
        participant_resolver: ParticipantResolver,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(participant_resolver) or not callable(clock):
            raise TypeError("draft material dependencies must be callable")
        self._store = store
        self._repository = repository
        self._participant_resolver = participant_resolver
        self._clock = clock

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
        request_digest = _json_digest(
            {
                "authorization": _authorization_semantics(authorization),
                "content_digest": digest,
            }
        )
        replay = self._repository.replay(
            scope,
            purpose=_PURPOSE,
            operation="save",
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if replay is not None:
            receipt = _save_receipt(replay)
            replayed = self._resolve_draft_material(
                scope,
                authorization=authorization,
                evidence_ref=str(receipt["evidence_ref"]),
                expected_digest=digest,
            )
            if not hmac.compare_digest(replayed, encoded):
                raise ValueError("draft material replay integrity drift")
            return receipt
        stored = self._store.put(scope, encoded, media_type="text/plain; charset=utf-8")
        evidence_ref = "EVR-" + stable_ulid(
            "email-draft-evidence",
            scope.site_id,
            authorization.inbox_item_ref,
            authorization.draft_ref,
            str(authorization.draft_revision),
            digest,
            str(stored.object_ref),
        )
        created_at = self._now()
        result: dict[str, object] = {
            "evidence_ref": evidence_ref,
            "digest": digest,
            "revision": authorization.draft_revision,
        }
        return _save_receipt(
            self._repository.commit_save(
                scope,
                purpose=_PURPOSE,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                receipt=result,
                binding={
                    "inbox_item_ref": authorization.inbox_item_ref,
                    "draft_ref": authorization.draft_ref,
                    "draft_revision": authorization.draft_revision,
                    "evidence_ref": evidence_ref,
                    "object_ref": str(stored.object_ref),
                    "digest": digest,
                    "media_type": "text/plain; charset=utf-8",
                    "byte_size": len(encoded),
                    "created_at": created_at,
                },
            )
        )

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
        role_binding = canonical_binding_digest(roles)
        if not hmac.compare_digest(role_binding, authorization.participant_roles_digest):
            raise PermissionError("participant role binding mismatch")
        material = self._resolve_draft_material(
            scope,
            authorization=authorization,
            evidence_ref=draft_evidence_ref,
            expected_digest=draft_digest,
        )
        replay_digest = _json_digest(
            {
                "authorization": _authorization_semantics(authorization),
                "draft_evidence_ref": draft_evidence_ref,
                "draft_digest": draft_digest,
                "draft_revision": draft_revision,
                "participant_roles": roles,
            }
        )
        resolved = self._participant_resolver(scope, authorization, roles)
        sender, recipients, cc, subject, participants = _resolved_participants(resolved, roles)
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
        if not final_bytes or len(final_bytes) > _MAX_FINAL_MIME_BYTES:
            raise ValueError("final MIME is outside the size budget")
        final_digest = "sha256:" + hashlib.sha256(final_bytes).hexdigest()
        replay = self._repository.replay(
            scope,
            purpose=_PURPOSE,
            operation="finalize",
            idempotency_key=idempotency_key,
            request_digest=replay_digest,
        )
        if replay is not None:
            receipt = _finalize_receipt(replay)
            replayed_bytes = self.resolve_final_mime(
                scope,
                evidence_ref=str(receipt["evidence_ref"]),
            )
            if not hmac.compare_digest(replayed_bytes, final_bytes):
                raise ValueError("final MIME replay integrity drift")
            return receipt

        # CAS and PostgreSQL cannot share one transaction. CAS is written first; if the
        # durable commit fails, the content-addressed object is an unreachable orphan
        # because neither its object_ref nor an EvidenceRef binding is returned.
        stored = self._store.put(scope, final_bytes, media_type="message/rfc822")
        evidence_ref = "EVR-" + stable_ulid(
            "email-final-mime-evidence",
            scope.site_id,
            authorization.draft_ref,
            str(authorization.draft_revision),
            final_digest,
            str(stored.object_ref),
        )
        result: dict[str, object] = {
            "evidence_ref": evidence_ref,
            "digest": final_digest,
            "role_binding": role_binding,
            "participants": participants,
        }
        created_at = self._now()
        return _finalize_receipt(
            self._repository.commit_finalize(
                scope,
                purpose=_PURPOSE,
                idempotency_key=idempotency_key,
                request_digest=replay_digest,
                receipt=result,
                binding={
                    "inbox_item_ref": authorization.inbox_item_ref,
                    "draft_ref": authorization.draft_ref,
                    "draft_revision": authorization.draft_revision,
                    "evidence_ref": evidence_ref,
                    "object_ref": str(stored.object_ref),
                    "digest": final_digest,
                    "media_type": "message/rfc822",
                    "byte_size": len(final_bytes),
                    "authorization_receipt_ref": authorization.receipt_ref,
                    "gateway_receipt_ref": authorization.gateway_receipt_ref,
                    "publication_ref": authorization.publication_ref,
                    "message_ref": authorization.message_ref,
                    "mailbox_ref": authorization.mailbox_ref,
                    "mailbox_config_revision": authorization.mailbox_config_revision,
                    "observer_delivery_ref": authorization.observer_delivery_ref,
                    "payload_digest": authorization.payload_digest,
                    "participant_binding_digest": authorization.participant_binding_digest,
                    "evidence_binding_digest": authorization.evidence_binding_digest,
                    "participant_roles_digest": authorization.participant_roles_digest,
                    "role_binding_digest": role_binding,
                    "source_draft_evidence_ref": draft_evidence_ref,
                    "source_draft_digest": draft_digest,
                    "created_at": created_at,
                },
            )
        )

    def resolve_final_mime(self, scope: TenantScope, *, evidence_ref: str) -> bytes:
        """Resolve one final EVR through its durable binding and verify CAS metadata."""

        _evidence_ref(evidence_ref)
        binding = self._repository.resolve_final(
            scope,
            purpose=_PURPOSE,
            evidence_ref=evidence_ref,
        )
        if binding is None:
            raise LookupError("final MIME evidence is unavailable")
        return self._read_binding(
            scope,
            binding,
            expected_evidence_ref=evidence_ref,
            expected_media_type="message/rfc822",
            maximum=_MAX_FINAL_MIME_BYTES,
        )

    def _authorize(self, scope: TenantScope, authorization: DraftAuthorizationReceipt) -> None:
        normalized = self._now()
        if authorization.site_id != scope.site_id:
            raise PermissionError("draft authorization site mismatch")
        if not authorization.issued_at <= normalized <= authorization.expires_at:
            raise PermissionError("draft authorization is stale")

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("draft material clock must be timezone-aware")
        return now.astimezone(UTC)

    def _resolve_draft_material(
        self,
        scope: TenantScope,
        *,
        authorization: DraftAuthorizationReceipt,
        evidence_ref: str,
        expected_digest: str,
    ) -> bytes:
        _evidence_ref(evidence_ref)
        binding = self._repository.resolve_draft(
            scope,
            purpose=_PURPOSE,
            evidence_ref=evidence_ref,
        )
        if binding is None:
            raise LookupError("draft evidence is unavailable")
        expected = {
            "inbox_item_ref": authorization.inbox_item_ref,
            "draft_ref": authorization.draft_ref,
            "draft_revision": authorization.draft_revision,
            "evidence_ref": evidence_ref,
            "digest": expected_digest,
        }
        if any(binding.get(field) != value for field, value in expected.items()):
            raise ValueError("draft evidence binding drift")
        return self._read_binding(
            scope,
            binding,
            expected_evidence_ref=evidence_ref,
            expected_media_type="text/plain; charset=utf-8",
            maximum=_MAX_DRAFT_BYTES,
        )

    def _read_binding(
        self,
        scope: TenantScope,
        binding: Mapping[str, object],
        *,
        expected_evidence_ref: str,
        expected_media_type: str,
        maximum: int,
    ) -> bytes:
        required = {"evidence_ref", "object_ref", "digest", "media_type", "byte_size"}
        if not required <= set(binding) or binding.get("evidence_ref") != expected_evidence_ref:
            raise ValueError("evidence binding is invalid")
        object_ref = _text(binding.get("object_ref"), "object_ref", maximum=512)
        digest = binding.get("digest")
        byte_size = binding.get("byte_size")
        if (
            not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or binding.get("media_type") != expected_media_type
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or not 1 <= byte_size <= maximum
        ):
            raise ValueError("evidence binding is invalid")
        try:
            content = self._store.read(scope, object_ref)
        except FileNotFoundError, ValueError:
            raise ValueError("evidence CAS integrity drift") from None
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if len(content) != byte_size or not hmac.compare_digest(actual, digest):
            raise ValueError("evidence CAS integrity drift")
        return content


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
) -> tuple[str, list[str], list[str], str, list[dict[str, str]]]:
    if not isinstance(value, Mapping) or not set(value).issubset(
        {
            "from",
            "to",
            "cc",
            "subject",
            "roles",
            "parsed_address_roles_digest",
            "participant_projection",
        }
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
    participants = _protected_participants(
        value.get("participant_projection"),
        recipient_count=len(recipients),
        cc_count=len(cc),
    )
    return sender, recipients, cc, subject, participants


def _protected_participants(
    value: object,
    *,
    recipient_count: int,
    cc_count: int,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 2 <= len(value) <= 256:
        raise PermissionError("participant projection is invalid")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "address_role",
            "opaque_address_ref",
        }:
            raise PermissionError("participant projection is invalid")
        role = item.get("address_role")
        opaque = item.get("opaque_address_ref")
        if role not in {"sender", "to", "cc"} or not isinstance(opaque, str):
            raise PermissionError("participant projection is invalid")
        if _OPAQUE_EMAIL.fullmatch(opaque) is None:
            raise PermissionError("participant projection is invalid")
        result.append({"address_role": role, "opaque_address_ref": opaque})
    if (
        sum(item["address_role"] == "sender" for item in result) != 1
        or sum(item["address_role"] == "to" for item in result) != recipient_count
        or sum(item["address_role"] == "cc" for item in result) != cc_count
        or len({(item["address_role"], item["opaque_address_ref"]) for item in result})
        != len(result)
    ):
        raise PermissionError("participant projection is invalid")
    return result


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


def _evidence_ref(value: object) -> str:
    text = _text(value, "evidence_ref", maximum=30)
    if re.fullmatch(r"EVR-[0-9A-HJKMNP-TV-Z]{26}", text) is None:
        raise ValueError("invalid evidence_ref")
    return text


def _authorization_semantics(value: DraftAuthorizationReceipt) -> dict[str, object]:
    return {
        "receipt_ref": value.receipt_ref,
        "site_id": value.site_id,
        "purpose": value.purpose,
        "inbox_item_ref": value.inbox_item_ref,
        "draft_ref": value.draft_ref,
        "draft_revision": value.draft_revision,
        "actor_ref": value.actor_ref,
        "team_ref": value.team_ref,
        "request_digest": value.request_digest,
        "gateway_receipt_ref": value.gateway_receipt_ref,
        "publication_ref": value.publication_ref,
        "message_ref": value.message_ref,
        "mailbox_ref": value.mailbox_ref,
        "mailbox_config_revision": value.mailbox_config_revision,
        "observer_delivery_ref": value.observer_delivery_ref,
        "payload_digest": value.payload_digest,
        "participant_binding_digest": value.participant_binding_digest,
        "evidence_binding_digest": value.evidence_binding_digest,
        "participant_roles_digest": value.participant_roles_digest,
        "issued_at": value.issued_at.isoformat(),
        "expires_at": value.expires_at.isoformat(),
    }


def _save_receipt(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != {"evidence_ref", "digest", "revision"}:
        raise ValueError("stored draft material receipt is invalid")
    evidence_ref = _evidence_ref(value.get("evidence_ref"))
    digest = value.get("digest")
    revision = value.get("revision")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise ValueError("stored draft material receipt is invalid")
    return {"evidence_ref": evidence_ref, "digest": digest, "revision": revision}


def _finalize_receipt(value: Mapping[str, object]) -> dict[str, object]:
    if set(value) != {"evidence_ref", "digest", "role_binding", "participants"}:
        raise ValueError("stored final MIME receipt is invalid")
    evidence_ref = _evidence_ref(value.get("evidence_ref"))
    digest = value.get("digest")
    role_binding = value.get("role_binding")
    participants = value.get("participants")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or not isinstance(role_binding, str)
        or _DIGEST.fullmatch(role_binding) is None
        or not isinstance(participants, list)
    ):
        raise ValueError("stored final MIME receipt is invalid")
    protected = _protected_participants(
        participants,
        recipient_count=sum(
            isinstance(item, Mapping) and item.get("address_role") == "to" for item in participants
        ),
        cc_count=sum(
            isinstance(item, Mapping) and item.get("address_role") == "cc" for item in participants
        ),
    )
    return {
        "evidence_ref": evidence_ref,
        "digest": digest,
        "role_binding": role_binding,
        "participants": protected,
    }


def _json_digest(value: object) -> str:
    import json

    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["DraftAuthorizationReceipt", "EmailDraftMaterialService"]
