"""Closed, provider-neutral facts published for one normalized email delivery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .identity_tokens import TransientIdentitySubject
from .models import (
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
    _require_aware,
    stable_ulid,
)

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_PUBLICATION_ID = re.compile(r"^PUB-[0-9A-HJKMNP-TV-Z]{26}$")
_MAILBOX_ID = re.compile(r"^MBX-[0-9A-HJKMNP-TV-Z]{26}$")
_CONNECTOR_REF = re.compile(r"^OCI-[0-9A-HJKMNP-TV-Z]{26}$")
_DELIVERY_REF = re.compile(r"^DLV-[0-9A-HJKMNP-TV-Z]{26}$")
_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_IDEMPOTENCY_KEY = re.compile(r"^idem:v1:[a-f0-9]{64}$")
_ADDRESS_ROLES = frozenset({"from", "to", "cc", "bcc"})
_OPAQUE_IDENTITY = re.compile(
    r"^(?:extid:v1:email:[A-Za-z0-9_-]{43}|"
    r"unresolved:delivery:[0-9A-HJKMNP-TV-Z]{26})$"
)


def _require_text(value: str, name: str, *, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")


def _require_digest(value: str | None, name: str) -> None:
    if value is not None and _DIGEST.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")


def header_digest(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("header value must be text")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class EmailParticipantSubject:
    """Transient address with its RFC header role; repr never reveals the address."""

    address_role: str
    subject: TransientIdentitySubject

    def __post_init__(self) -> None:
        if self.address_role not in _ADDRESS_ROLES:
            raise ValueError("invalid email address role")
        if (
            not isinstance(self.subject, TransientIdentitySubject)
            or self.subject.provider != "email"
        ):
            raise TypeError("email participant requires an email identity subject")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(address_role={self.address_role!r}, "
            f"subject=<redacted chars={len(self.subject.subject)}>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailHeaderFacts:
    subject_digest: str
    message_id_digest: str | None
    in_reply_to_digest: str | None
    references_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_digest(self.subject_digest, "subject_digest")
        _require_digest(self.message_id_digest, "message_id_digest")
        _require_digest(self.in_reply_to_digest, "in_reply_to_digest")
        if (
            not isinstance(self.references_digests, tuple)
            or len(self.references_digests) > 100
            or len(self.references_digests) != len(set(self.references_digests))
        ):
            raise ValueError("invalid references digests")
        for value in self.references_digests:
            _require_digest(value, "references_digest")
        if (
            self.message_id_digest is None
            and self.in_reply_to_digest is None
            and not self.references_digests
        ):
            raise ValueError("email header facts require a thread digest")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(subject_digest={self.subject_digest!r}, "
            f"message_id_digest={self.message_id_digest!r}, "
            f"in_reply_to_digest={self.in_reply_to_digest!r}, "
            f"references_count={len(self.references_digests)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailPublicationParticipant:
    address_role: str
    identity_ref: str

    def __post_init__(self) -> None:
        if self.address_role not in _ADDRESS_ROLES:
            raise ValueError("invalid publication address role")
        if _OPAQUE_IDENTITY.fullmatch(self.identity_ref) is None:
            raise ValueError("invalid publication identity ref")

    def to_payload(self) -> dict[str, str]:
        return {"address_role": self.address_role, "identity_ref": self.identity_ref}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(address_role={self.address_role!r}, identity_ref=<protected>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailMessagePublication:
    publication_id: str
    site_id: str
    mailbox_id: str
    mailbox_config_revision: int
    observer_connector_instance_ref: str
    observer_delivery_ref: str
    received_at: datetime
    evidence_refs: tuple[str, ...]
    participants: tuple[EmailPublicationParticipant, ...]
    subject_digest: str
    message_id_digest: str | None
    in_reply_to_digest: str | None
    references_digests: tuple[str, ...]
    publication_revision: int
    idempotency_key: str

    def __post_init__(self) -> None:
        for value, pattern, name in (
            (self.publication_id, _PUBLICATION_ID, "publication_id"),
            (self.site_id, _SITE_ID, "site_id"),
            (self.mailbox_id, _MAILBOX_ID, "mailbox_id"),
            (
                self.observer_connector_instance_ref,
                _CONNECTOR_REF,
                "observer_connector_instance_ref",
            ),
            (self.observer_delivery_ref, _DELIVERY_REF, "observer_delivery_ref"),
            (self.idempotency_key, _IDEMPOTENCY_KEY, "idempotency_key"),
        ):
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError(f"invalid {name}")
        if (
            isinstance(self.mailbox_config_revision, bool)
            or not isinstance(self.mailbox_config_revision, int)
            or not 1 <= self.mailbox_config_revision <= 2_147_483_647
        ):
            raise ValueError("invalid mailbox_config_revision")
        if (
            isinstance(self.publication_revision, bool)
            or not isinstance(self.publication_revision, int)
            or not 1 <= self.publication_revision <= 2_147_483_647
        ):
            raise ValueError("invalid publication_revision")
        _require_aware(self.received_at, "received_at")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or len(self.evidence_refs) > 256
            or len(self.evidence_refs) != len(set(self.evidence_refs))
            or any(_EVIDENCE_REF.fullmatch(value) is None for value in self.evidence_refs)
        ):
            raise ValueError("publication requires bounded evidence refs")
        if (
            not isinstance(self.participants, tuple)
            or not self.participants
            or len(self.participants) > 256
        ):
            raise ValueError("publication requires bounded participants")
        if not all(isinstance(value, EmailPublicationParticipant) for value in self.participants):
            raise TypeError("invalid publication participant")
        participant_items = tuple(
            (value.address_role, value.identity_ref) for value in self.participants
        )
        if len(participant_items) != len(set(participant_items)):
            raise ValueError("publication participants must be unique")
        _require_digest(self.subject_digest, "subject_digest")
        _require_digest(self.message_id_digest, "message_id_digest")
        _require_digest(self.in_reply_to_digest, "in_reply_to_digest")
        if (
            not isinstance(self.references_digests, tuple)
            or len(self.references_digests) > 100
            or len(self.references_digests) != len(set(self.references_digests))
        ):
            raise ValueError("invalid references digests")
        for value in self.references_digests:
            _require_digest(value, "references_digest")
        if (
            self.message_id_digest is None
            and self.in_reply_to_digest is None
            and not self.references_digests
        ):
            raise ValueError("publication requires a thread header digest")

    def to_wire(self) -> dict[str, Any]:
        header_digests: dict[str, object] = {}
        if self.message_id_digest is not None:
            header_digests["message_id"] = self.message_id_digest
        if self.in_reply_to_digest is not None:
            header_digests["in_reply_to"] = self.in_reply_to_digest
        if self.references_digests:
            header_digests["references"] = list(self.references_digests)
        return {
            "publication_id": self.publication_id,
            "site_id": self.site_id,
            "mailbox_id": self.mailbox_id,
            "mailbox_config_revision": self.mailbox_config_revision,
            "observer_connector_instance_ref": self.observer_connector_instance_ref,
            "observer_delivery_ref": self.observer_delivery_ref,
            "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
            "evidence_refs": list(self.evidence_refs),
            "participants": [value.to_payload() for value in self.participants],
            "subject_digest": self.subject_digest,
            "header_digests": header_digests,
            "publication_revision": self.publication_revision,
            "idempotency_key": self.idempotency_key,
        }

    def to_payload(self) -> dict[str, Any]:
        """Compatibility alias; persisted/public wire shape is schema-exact."""

        return self.to_wire()

    @property
    def payload_sha256(self) -> str:
        encoded = json.dumps(
            self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        delivery_digest = hashlib.sha256(self.observer_delivery_ref.encode()).hexdigest()
        return (
            f"{type(self).__name__}(publication_id={self.publication_id!r}, "
            f"site_id={self.site_id!r}, mailbox_id={self.mailbox_id!r}, "
            f"mailbox_config_revision={self.mailbox_config_revision}, "
            f"observer_delivery_ref_sha256={delivery_digest!r}, "
            f"participant_count={len(self.participants)}, "
            f"evidence_count={len(self.evidence_refs)}, "
            f"payload_sha256={self.payload_sha256!r})"
        )


def build_email_publication(
    *,
    scope: TenantScope,
    key: ConnectorKey,
    item: ConnectorItem,
    normalized: NormalizedObservationInput,
    mailbox_id: str,
    mailbox_config_revision: int,
    observer_delivery_ref: str,
    received_at: datetime,
    publication_revision: int,
) -> EmailMessagePublication:
    """Convert transient decoder facts and opaque normalized identities to a closed payload."""

    if key.connector != "email" or normalized.channel != "email":
        raise ValueError("email publication requires normalized email input")
    header_facts = item.payload.get("email_header_facts")
    role_subjects = item.payload.get("email_participant_subjects")
    if not isinstance(header_facts, EmailHeaderFacts) or not isinstance(role_subjects, tuple):
        raise ValueError("email publication facts are unavailable")
    if not all(isinstance(value, EmailParticipantSubject) for value in role_subjects):
        raise ValueError("invalid email participant facts")
    if len(role_subjects) != len(normalized.participants):
        raise ValueError("email participant projection mismatch")
    participant_candidates = tuple(
        EmailPublicationParticipant(
            address_role=transient.address_role,
            identity_ref=opaque.identity_ref,
        )
        for transient, opaque in zip(role_subjects, normalized.participants, strict=True)
    )
    participants = tuple(dict.fromkeys(participant_candidates))
    evidence_refs = tuple(
        artifact.reference for artifact in normalized.evidence if artifact.reference is not None
    )
    identity_material = "\x1f".join(
        (
            scope.site_id,
            mailbox_id,
            str(mailbox_config_revision),
            key.instance_id,
            observer_delivery_ref,
        )
    )
    identity_digest = hashlib.sha256(identity_material.encode()).hexdigest()
    return EmailMessagePublication(
        publication_id="PUB-" + stable_ulid("email-publication", identity_digest),
        site_id=scope.site_id,
        mailbox_id=mailbox_id,
        mailbox_config_revision=mailbox_config_revision,
        observer_connector_instance_ref=(
            "OCI-" + stable_ulid("email-connector-instance", scope.site_id, key.instance_id)
        ),
        observer_delivery_ref=(
            "DLV-"
            + stable_ulid("email-delivery", scope.site_id, key.instance_id, observer_delivery_ref)
        ),
        received_at=received_at,
        evidence_refs=tuple(
            "EVR-" + stable_ulid("email-evidence-ref", scope.site_id, reference)
            for reference in evidence_refs
        ),
        participants=participants,
        subject_digest=header_facts.subject_digest,
        message_id_digest=header_facts.message_id_digest,
        in_reply_to_digest=header_facts.in_reply_to_digest,
        references_digests=header_facts.references_digests,
        publication_revision=publication_revision,
        idempotency_key="idem:v1:" + identity_digest,
    )


__all__ = [
    "EmailHeaderFacts",
    "EmailMessagePublication",
    "EmailParticipantSubject",
    "EmailPublicationParticipant",
    "build_email_publication",
    "header_digest",
]
