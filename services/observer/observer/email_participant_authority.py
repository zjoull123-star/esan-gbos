"""Observer-owned Inbox bindings and transient RFC 822 participant resolution."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from .email_publication import EmailHeaderFacts, EmailParticipantSubject
from .identity_tokens import IdentityTokenResolver
from .models import TenantScope, _require_aware
from .storage import Connection

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_PREFIXED_REF = re.compile(r"^(EGR|PUB|INB|MSG|MBX|DLV)-[0-9A-HJKMNP-TV-Z]{26}$")
_EMAIL_IDENTITY_REF = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_RECEIPT_FIELDS = frozenset(
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
_MAX_MESSAGE_BYTES = 10_000_000


class EmailParticipantAuthorityConflict(PermissionError):
    """Safe fail-closed authority error that never renders protected material."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("invalid participant authority error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


def canonical_binding_digest(value: object) -> str:
    """Digest closed JSON with stable mapping order and significant list order."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except TypeError, ValueError, UnicodeEncodeError:
        raise EmailParticipantAuthorityConflict("binding_json_invalid") from None
    if not encoded or len(encoded) > 1_048_576:
        raise EmailParticipantAuthorityConflict("binding_json_invalid")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class EmailParticipantAuthorityBinding:
    gateway_receipt_ref: str
    publication_ref: str
    inbox_item_ref: str
    message_ref: str
    mailbox_ref: str
    mailbox_config_revision: int
    observer_delivery_ref: str
    payload_digest: str
    participant_binding_digest: str
    evidence_binding_digest: str

    @classmethod
    def from_wire(cls, value: object) -> EmailParticipantAuthorityBinding:
        if not isinstance(value, Mapping) or set(value) != _RECEIPT_FIELDS:
            raise EmailParticipantAuthorityConflict("receipt_shape_invalid")
        references = {
            field: _reference(value.get(field), prefix)
            for field, prefix in (
                ("gateway_receipt_ref", "EGR"),
                ("publication_ref", "PUB"),
                ("inbox_item_ref", "INB"),
                ("message_ref", "MSG"),
                ("mailbox_ref", "MBX"),
                ("observer_delivery_ref", "DLV"),
            )
        }
        revision = value.get("mailbox_config_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 1 <= revision <= 2_147_483_647
        ):
            raise EmailParticipantAuthorityConflict("receipt_revision_invalid")
        digests = {
            field: _digest(value.get(field), field)
            for field in (
                "payload_digest",
                "participant_binding_digest",
                "evidence_binding_digest",
            )
        }
        return cls(**references, mailbox_config_revision=revision, **digests)

    def to_wire(self) -> dict[str, object]:
        return {
            "gateway_receipt_ref": self.gateway_receipt_ref,
            "publication_ref": self.publication_ref,
            "inbox_item_ref": self.inbox_item_ref,
            "message_ref": self.message_ref,
            "mailbox_ref": self.mailbox_ref,
            "mailbox_config_revision": self.mailbox_config_revision,
            "observer_delivery_ref": self.observer_delivery_ref,
            "payload_digest": self.payload_digest,
            "participant_binding_digest": self.participant_binding_digest,
            "evidence_binding_digest": self.evidence_binding_digest,
        }

    def __repr__(self) -> str:
        return (
            "EmailParticipantAuthorityBinding("
            f"gateway_receipt_ref={self.gateway_receipt_ref!r}, "
            f"publication_ref={self.publication_ref!r}, "
            f"inbox_item_ref={self.inbox_item_ref!r}, message_ref={self.message_ref!r}, "
            f"mailbox_ref={self.mailbox_ref!r}, "
            f"mailbox_config_revision={self.mailbox_config_revision}, "
            "observer_delivery_ref=<redacted>, payload_digest=<redacted>, "
            "participant_binding_digest=<redacted>, evidence_binding_digest=<redacted>)"
        )


def validate_gateway_receipt_binding(
    receipt: object,
    *,
    publication_payload: Mapping[str, object],
    payload_digest: str,
) -> EmailParticipantAuthorityBinding:
    """Close a Gateway receipt against the Observer's own immutable outbox payload."""

    binding = EmailParticipantAuthorityBinding.from_wire(receipt)
    if not isinstance(publication_payload, Mapping):
        raise EmailParticipantAuthorityConflict("publication_payload_invalid")
    calculated_payload_digest = canonical_binding_digest(dict(publication_payload))
    expected_payload_digest = _digest(payload_digest, "payload_digest")
    participants = publication_payload.get("participants")
    evidence_refs = publication_payload.get("evidence_refs")
    if not isinstance(participants, list) or not participants:
        raise EmailParticipantAuthorityConflict("publication_participants_invalid")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise EmailParticipantAuthorityConflict("publication_evidence_invalid")
    expected = (
        (binding.publication_ref, publication_payload.get("publication_id")),
        (binding.mailbox_ref, publication_payload.get("mailbox_id")),
        (
            binding.mailbox_config_revision,
            publication_payload.get("mailbox_config_revision"),
        ),
        (binding.observer_delivery_ref, publication_payload.get("observer_delivery_ref")),
        (binding.payload_digest, calculated_payload_digest),
        (binding.payload_digest, expected_payload_digest),
        (binding.participant_binding_digest, canonical_binding_digest(participants)),
        (binding.evidence_binding_digest, canonical_binding_digest(evidence_refs)),
    )
    if any(not _equal(left, right) for left, right in expected):
        raise EmailParticipantAuthorityConflict("receipt_binding_drift")
    return binding


@dataclass(frozen=True, slots=True, repr=False)
class EmailParticipantAuthorityRecord:
    binding: EmailParticipantAuthorityBinding
    publication_payload: Mapping[str, object]
    delivery_id: str
    object_ref: str
    exact_body_sha256: str
    byte_size: int
    media_type: str
    received_at: datetime
    mailbox_address_identity_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, EmailParticipantAuthorityBinding):
            raise TypeError("invalid participant authority binding")
        if not isinstance(self.publication_payload, Mapping):
            raise TypeError("invalid participant authority publication payload")
        _safe_text(self.delivery_id, "delivery_id", maximum=512)
        _safe_text(self.object_ref, "object_ref", maximum=512)
        if not re.fullmatch(r"[a-f0-9]{64}", self.exact_body_sha256):
            raise ValueError("invalid exact body digest")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 1 <= self.byte_size <= _MAX_MESSAGE_BYTES
            or self.media_type.lower() != "message/rfc822"
        ):
            raise ValueError("invalid participant authority delivery metadata")
        _require_aware(self.received_at, "received_at")
        if self.mailbox_address_identity_ref is not None and (
            not isinstance(self.mailbox_address_identity_ref, str)
            or _EMAIL_IDENTITY_REF.fullmatch(self.mailbox_address_identity_ref) is None
        ):
            raise ValueError("invalid mailbox address identity ref")

    def __repr__(self) -> str:
        return (
            "EmailParticipantAuthorityRecord("
            f"binding={self.binding!r}, delivery_id=<redacted>, object_ref=<redacted>, "
            f"byte_size={self.byte_size}, media_type={self.media_type!r}, "
            f"received_at={self.received_at!r}, publication_payload=<redacted>, "
            "mailbox_address_identity_ref=<redacted>)"
        )


class EmailParticipantAuthorityRepository(Protocol):
    def load(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> EmailParticipantAuthorityRecord: ...


class ParticipantCasStore(Protocol):
    def read(self, scope: TenantScope, object_ref: str) -> bytes: ...


class InMemoryEmailParticipantAuthorityRepository:
    def __init__(self, records: tuple[EmailParticipantAuthorityRecord, ...] = ()) -> None:
        self._records: dict[tuple[str, str], EmailParticipantAuthorityRecord] = {}
        for record in records:
            key = (str(record.publication_payload.get("site_id")), record.binding.inbox_item_ref)
            if key in self._records:
                raise EmailParticipantAuthorityConflict("authority_inbox_conflict")
            self._records[key] = record

    def load(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> EmailParticipantAuthorityRecord:
        record = self._records.get((scope.site_id, inbox_item_ref))
        if record is None:
            raise EmailParticipantAuthorityConflict("participant_authority_unavailable")
        return record

    def __repr__(self) -> str:
        return f"{type(self).__name__}(record_count={len(self._records)})"


class PostgresEmailParticipantAuthorityRepository:
    """Observer-app read-only view from Inbox binding through delivery to CAS metadata."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> EmailParticipantAuthorityRecord:
        _reference(inbox_item_ref, "INB")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
            cursor.execute(
                """
                SELECT binding.gateway_receipt_ref, binding.publication_ref,
                       binding.inbox_item_ref, binding.message_ref,
                       binding.mailbox_ref, binding.mailbox_config_revision,
                       binding.observer_delivery_ref, binding.payload_digest,
                       binding.participant_binding_digest,
                       binding.evidence_binding_digest, outbox.payload,
                       delivery.delivery_id, delivery.object_ref,
                       delivery.exact_body_sha256, delivery.byte_size,
                       delivery.media_type, delivery.received_at,
                       config.mailbox_address_identity_ref
                FROM observer.email_gateway_inbox_bindings AS binding
                JOIN observer.email_message_publication_outbox AS outbox
                  ON outbox.site_id = binding.site_id
                 AND outbox.publication_id = binding.publication_ref
                 AND outbox.relay_status = 'delivered'
                JOIN observer.inbound_deliveries AS delivery
                  ON delivery.site_id = outbox.site_id
                 AND delivery.connector = outbox.connector
                 AND delivery.connector_instance_id = outbox.connector_instance_id
                 AND delivery.delivery_id = outbox.observer_delivery_ref
                JOIN observer.email_connector_config_projections AS config
                  ON config.site_id = outbox.site_id
                 AND config.mailbox_id = outbox.mailbox_id
                 AND config.mailbox_config_revision = outbox.mailbox_config_revision
                WHERE binding.site_id = %s AND binding.inbox_item_ref = %s
                """,
                (scope.site_id, inbox_item_ref),
            )
            row = cursor.fetchone()
        if row is None:
            raise EmailParticipantAuthorityConflict("participant_authority_unavailable")
        payload = row[10]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                raise EmailParticipantAuthorityConflict("publication_payload_invalid") from None
        if not isinstance(payload, Mapping):
            raise EmailParticipantAuthorityConflict("publication_payload_invalid")
        binding = EmailParticipantAuthorityBinding.from_wire(
            dict(zip(_RECEIPT_FIELDS_IN_ORDER, row[:10], strict=True))
        )
        return EmailParticipantAuthorityRecord(
            binding=binding,
            publication_payload=cast(Mapping[str, object], payload),
            delivery_id=str(row[11]),
            object_ref=str(row[12]),
            exact_body_sha256=str(row[13]),
            byte_size=int(str(row[14])),
            media_type=str(row[15]),
            received_at=cast(datetime, row[16]),
            mailbox_address_identity_ref=(None if row[17] is None else str(row[17])),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connection=<redacted>)"


class EmailParticipantAuthorityResolver:
    """Resolve raw addresses only from one bound Observer delivery and CAS object."""

    def __init__(
        self,
        *,
        repository: EmailParticipantAuthorityRepository,
        store: ParticipantCasStore,
        identity_resolver: IdentityTokenResolver | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._identity_resolver = identity_resolver

    def __call__(
        self,
        scope: TenantScope,
        authorization: object,
        roles: Mapping[str, object],
    ) -> Mapping[str, object]:
        requested_binding = _authorization_binding(authorization)
        durable = self._repository.load(
            scope,
            inbox_item_ref=requested_binding.inbox_item_ref,
        )
        if durable.binding != requested_binding:
            raise EmailParticipantAuthorityConflict("draft_authority_binding_drift")
        validate_gateway_receipt_binding(
            durable.binding.to_wire(),
            publication_payload=durable.publication_payload,
            payload_digest=durable.binding.payload_digest,
        )
        raw = self._store.read(scope, durable.object_ref)
        if len(raw) != durable.byte_size or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), durable.exact_body_sha256
        ):
            raise EmailParticipantAuthorityConflict("delivery_cas_integrity_drift")
        try:
            from .connectors.email_delivery import EmailRawDeliveryDecoder

            item = EmailRawDeliveryDecoder(max_message_bytes=_MAX_MESSAGE_BYTES).decode_delivery(
                raw,
                delivery_id=durable.delivery_id,
                received_at=durable.received_at,
                source_ref=durable.object_ref,
            )[0]
        except Exception:
            raise EmailParticipantAuthorityConflict("delivery_eml_invalid") from None
        subjects = item.payload.get("email_participant_subjects")
        headers = item.payload.get("email_header_facts")
        if (
            not isinstance(subjects, tuple)
            or not all(isinstance(value, EmailParticipantSubject) for value in subjects)
            or not isinstance(headers, EmailHeaderFacts)
        ):
            raise EmailParticipantAuthorityConflict("delivery_eml_invalid")
        published_participants = durable.publication_payload.get("participants")
        if not isinstance(published_participants, list):
            raise EmailParticipantAuthorityConflict("publication_participants_invalid")
        published_roles: list[str] = []
        for participant in published_participants:
            if not isinstance(participant, Mapping) or set(participant) != {
                "address_role",
                "identity_ref",
            }:
                raise EmailParticipantAuthorityConflict("publication_participants_invalid")
            role = participant.get("address_role")
            if not isinstance(role, str):
                raise EmailParticipantAuthorityConflict("publication_participants_invalid")
            published_roles.append(role)
        parsed_roles = [value.address_role for value in subjects]
        if published_roles != parsed_roles or not _header_facts_match(
            headers, durable.publication_payload
        ):
            raise EmailParticipantAuthorityConflict("delivery_publication_drift")
        if self._identity_resolver is None:
            raise EmailParticipantAuthorityConflict("participant_identity_authority_unavailable")
        try:
            parsed_identities = [
                self._identity_resolver.resolve(
                    scope.site_id,
                    scope.processing_purpose,
                    "email",
                    value.subject.subject,
                )
                for value in subjects
            ]
        except Exception:
            raise EmailParticipantAuthorityConflict(
                "participant_identity_authority_unavailable"
            ) from None
        published_identities = [
            cast(str, participant.get("identity_ref")) for participant in published_participants
        ]
        if len(parsed_identities) != len(published_identities) or any(
            not hmac.compare_digest(parsed, published)
            for parsed, published in zip(parsed_identities, published_identities, strict=True)
        ):
            raise EmailParticipantAuthorityConflict("delivery_identity_binding_drift")
        addresses: dict[str, list[str]] = {role: [] for role in ("from", "to", "cc", "bcc")}
        for value in subjects:
            addresses[value.address_role].append(value.subject.subject)
        sender = _resolve_role(
            str(roles.get("sender")),
            addresses,
            subjects=subjects,
            parsed_identities=parsed_identities,
            mailbox_address_identity_ref=durable.mailbox_address_identity_ref,
        )
        if len(sender) != 1:
            raise EmailParticipantAuthorityConflict("sender_role_ambiguous")
        recipients_value = roles.get("recipients")
        if not isinstance(recipients_value, list) or not recipients_value:
            raise EmailParticipantAuthorityConflict("recipient_roles_invalid")
        to: list[str] = []
        cc: list[str] = []
        for role_value in recipients_value:
            selected = _resolve_role(
                str(role_value),
                addresses,
                subjects=subjects,
                parsed_identities=parsed_identities,
                mailbox_address_identity_ref=durable.mailbox_address_identity_ref,
            )
            (cc if role_value == "original_cc" else to).extend(selected)
        if not to:
            raise EmailParticipantAuthorityConflict("recipient_to_unavailable")
        return {
            "from": sender[0],
            "to": _deduplicate(to),
            "cc": _deduplicate(cc),
            "roles": dict(roles),
            "parsed_address_roles_digest": canonical_binding_digest(parsed_roles),
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(repository=<redacted>, store=<redacted>)"


_RECEIPT_FIELDS_IN_ORDER = (
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


def _authorization_binding(value: object) -> EmailParticipantAuthorityBinding:
    try:
        wire = {field: getattr(value, field) for field in _RECEIPT_FIELDS_IN_ORDER}
    except AttributeError:
        if not isinstance(value, EmailParticipantAuthorityBinding):
            raise EmailParticipantAuthorityConflict("draft_authority_binding_invalid") from None
        return value
    return EmailParticipantAuthorityBinding.from_wire(wire)


def _header_facts_match(facts: EmailHeaderFacts, payload: Mapping[str, object]) -> bool:
    headers = payload.get("header_digests")
    return bool(
        isinstance(headers, Mapping)
        and payload.get("subject_digest") == facts.subject_digest
        and headers.get("message_id") == facts.message_id_digest
        and headers.get("in_reply_to") == facts.in_reply_to_digest
        and tuple(cast(list[str], headers.get("references", []))) == facts.references_digests
    )


def _resolve_role(
    role: str,
    addresses: Mapping[str, list[str]],
    *,
    subjects: tuple[EmailParticipantSubject, ...],
    parsed_identities: list[str],
    mailbox_address_identity_ref: str | None,
) -> list[str]:
    if role == "assigned_owner":
        raise PermissionError("assigned owner authority is unavailable")
    if role == "mailbox_owner":
        if mailbox_address_identity_ref is None:
            raise EmailParticipantAuthorityConflict("mailbox_owner_authority_unavailable")
        candidates = [
            subject.subject.subject
            for subject, identity_ref in zip(subjects, parsed_identities, strict=True)
            if hmac.compare_digest(identity_ref, mailbox_address_identity_ref)
        ]
        if len(candidates) != 1:
            raise EmailParticipantAuthorityConflict("mailbox_owner_ambiguous")
        return candidates
    source = {
        "original_sender": "from",
        "original_to": "to",
        "original_cc": "cc",
    }.get(role)
    if source is None:
        raise EmailParticipantAuthorityConflict("participant_role_invalid")
    resolved = _deduplicate(addresses.get(source, []))
    if not resolved:
        raise EmailParticipantAuthorityConflict("participant_role_unavailable")
    return resolved


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _reference(value: object, prefix: str) -> str:
    if not isinstance(value, str):
        raise EmailParticipantAuthorityConflict("receipt_reference_invalid")
    matched = _PREFIXED_REF.fullmatch(value)
    if matched is None or matched.group(1) != prefix:
        raise EmailParticipantAuthorityConflict("receipt_reference_invalid")
    return value


def _digest(value: object, _field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EmailParticipantAuthorityConflict("receipt_digest_invalid")
    return value


def _safe_text(value: object, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"invalid {field}")
    return value


def _equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return hmac.compare_digest(left, right)
    return left == right


__all__ = [
    "EmailParticipantAuthorityBinding",
    "EmailParticipantAuthorityConflict",
    "EmailParticipantAuthorityRecord",
    "EmailParticipantAuthorityResolver",
    "InMemoryEmailParticipantAuthorityRepository",
    "PostgresEmailParticipantAuthorityRepository",
    "canonical_binding_digest",
    "validate_gateway_receipt_binding",
]
