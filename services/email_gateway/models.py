from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Self

_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_OPAQUE_EMAIL_IDENTITY = re.compile(r"^extid:v1:[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_-]{43}$")
_OPAQUE_MAILBOX_ADDRESS_IDENTITY = re.compile(r"^extid:v1:email:[A-Za-z0-9_-]{43}$")
_OPAQUE_PUBLICATION_PARTICIPANT = re.compile(
    r"^(?:extid:v1:email:[A-Za-z0-9_-]{43}|unresolved:delivery:[0-9A-HJKMNP-TV-Z]{26})$"
)
_PREFIXED_ULID = re.compile(r"^(?P<prefix>[A-Z]{3})-[0-9A-HJKMNP-TV-Z]{26}$")
_IDEMPOTENCY = re.compile(r"^idem:v1:[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SECRET_REF = re.compile(r"^secretref:v1/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PROCESSING_PURPOSES = frozenset(
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
PROVIDERS = frozenset({"fake", "imap_smtp", "wecom_app_mail"})
ENTRY_ROLES = frozenset({"primary", "workflow", "migration", "selective_archive"})
MAILBOX_STATUSES = frozenset({"draft", "active", "paused", "revoked", "error"})
PARTICIPANT_ROLES = frozenset({"from", "to", "cc", "bcc"})
INBOX_STATES = frozenset(
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


class ValidationError(ValueError):
    """A closed Email Gateway contract rejected an invalid value."""


class ScopeViolation(ValidationError):
    """A site or processing-purpose boundary was crossed."""


class RevisionConflict(ValidationError):
    """An expected revision did not match current durable state."""


class IdempotencyConflict(ValidationError):
    """An idempotency key was replayed with a different payload."""


class AuthorizationError(ValidationError):
    """A human or service actor is not authorized for the operation."""


class OutboundNotAuthorized(AuthorizationError):
    """Approved outbound command execution is not installed."""


def _safe_text(value: str, name: str, *, maximum: int = 256, allow_at: bool = True) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL.search(value)
        or (not allow_at and "@" in value)
    ):
        raise ValidationError(f"invalid {name}")
    return value


def _optional_text(
    value: str | None, name: str, *, maximum: int = 256, allow_at: bool = True
) -> str | None:
    if value is not None:
        _safe_text(value, name, maximum=maximum, allow_at=allow_at)
    return value


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"invalid {name}")


def _digest(value: str, name: str = "digest") -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValidationError(f"invalid {name}")


def _prefixed_ulid(value: object, prefix: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"invalid {name}")
    matched = _PREFIXED_ULID.fullmatch(value)
    if matched is None or matched.group("prefix") != prefix:
        raise ValidationError(f"invalid {name}")
    return value


def _wire_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"invalid {name}")
    return value


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def stable_ref(prefix: str, *parts: str) -> str:
    if not re.fullmatch(r"[A-Z]{3}", prefix):
        raise ValidationError("invalid stable ref prefix")
    material = "\x1f".join((prefix, *parts)).encode()
    value = int.from_bytes(hashlib.sha256(material).digest()[:16], "big")
    encoded = "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))
    return f"{prefix}-{encoded}"


@dataclass(frozen=True, slots=True)
class TenantScope:
    site_id: str
    processing_purpose: str

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid site_id")
        if self.processing_purpose not in PROCESSING_PURPOSES:
            raise ValidationError("invalid processing_purpose")


def require_scope(
    scope: TenantScope,
    *,
    site_id: str,
    processing_purpose: str | None = None,
) -> None:
    if scope.site_id != site_id:
        raise ScopeViolation("site scope mismatch")
    if processing_purpose is not None and scope.processing_purpose != processing_purpose:
        raise ScopeViolation("processing purpose mismatch")


@dataclass(frozen=True, slots=True)
class GatewayActorScope:
    site_id: str
    actor_ref: str
    team_refs: tuple[str, ...]
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid actor site")
        _safe_text(self.actor_ref, "actor ref")
        if not isinstance(self.team_refs, tuple) or not isinstance(self.roles, tuple):
            raise ValidationError("actor scope collections must be tuples")
        if len(self.team_refs) != len(set(self.team_refs)) or len(self.roles) != len(
            set(self.roles)
        ):
            raise ValidationError("duplicate actor scope value")
        for value in self.team_refs:
            _safe_text(value, "team ref", allow_at=False)
        for value in self.roles:
            _safe_text(value, "role", maximum=80)


@dataclass(frozen=True, slots=True)
class Mailbox:
    mailbox_ref: str
    site_id: str
    address_display: str
    provider: str
    provider_account_ref: str
    observer_connector_instance_ref: str
    entry_role: str
    business_purpose: str
    default_team_ref: str
    account_owner_user_ref: str
    priority: int
    inbound_enabled: bool
    outbound_enabled: bool
    credential_ref: str
    status: str
    config_revision: int
    observer_config_projection_receipt: str | None
    mailbox_address_identity_ref: str | None = None

    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "mailbox_ref",
            "site_id",
            "address_display",
            "provider",
            "provider_account_ref",
            "observer_connector_instance_ref",
            "entry_role",
            "business_purpose",
            "default_team_ref",
            "account_owner_user_ref",
            "priority",
            "inbound_enabled",
            "outbound_enabled",
            "credential_ref",
            "status",
            "config_revision",
            "observer_config_projection_receipt",
            "mailbox_address_identity_ref",
        }
    )

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid site")
        _safe_text(self.mailbox_ref, "mailbox ref", allow_at=False)
        _safe_text(self.address_display, "mailbox address display", maximum=320)
        if self.provider not in PROVIDERS:
            raise ValidationError("invalid provider")
        _safe_text(self.provider_account_ref, "provider account ref", allow_at=False)
        _safe_text(
            self.observer_connector_instance_ref,
            "observer connector instance ref",
            allow_at=False,
        )
        if self.entry_role not in ENTRY_ROLES:
            raise ValidationError("invalid entry role")
        if self.business_purpose not in PROCESSING_PURPOSES:
            raise ValidationError("invalid business purpose")
        _safe_text(self.default_team_ref, "default team ref", allow_at=False)
        _safe_text(self.account_owner_user_ref, "account owner user ref")
        if (
            not isinstance(self.priority, int)
            or isinstance(self.priority, bool)
            or not 0 <= self.priority <= 1000
        ):
            raise ValidationError("invalid priority")
        if not isinstance(self.inbound_enabled, bool) or not isinstance(
            self.outbound_enabled, bool
        ):
            raise ValidationError("invalid mailbox switch")
        _safe_text(self.credential_ref, "credential ref", maximum=80, allow_at=False)
        if self.status not in MAILBOX_STATUSES:
            raise ValidationError("invalid status")
        if (
            not isinstance(self.config_revision, int)
            or isinstance(self.config_revision, bool)
            or self.config_revision < 1
        ):
            raise ValidationError("invalid config revision")
        _optional_text(
            self.observer_config_projection_receipt,
            "observer config projection receipt",
            allow_at=False,
        )
        if self.mailbox_address_identity_ref is not None and (
            not isinstance(self.mailbox_address_identity_ref, str)
            or _OPAQUE_MAILBOX_ADDRESS_IDENTITY.fullmatch(self.mailbox_address_identity_ref) is None
        ):
            raise ValidationError("invalid mailbox address identity ref")

    def to_wire(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_wire(cls, value: dict[str, object], *, expected_site_id: str | None = None) -> Self:
        if not isinstance(value, dict) or set(value) != cls.WIRE_FIELDS:
            raise ValidationError("unknown or missing mailbox field")
        mailbox = cls(**value)  # type: ignore[arg-type]
        if expected_site_id is not None and mailbox.site_id != expected_site_id:
            raise ValidationError("mailbox site mismatch")
        return mailbox

    def __repr__(self) -> str:
        return (
            "Mailbox("
            f"mailbox_ref={self.mailbox_ref!r}, site_id={self.site_id!r}, "
            f"provider={self.provider!r}, "
            f"entry_role={self.entry_role!r}, status={self.status!r}, "
            f"config_revision={self.config_revision}, address_display=<redacted>, "
            "account_owner_user_ref=<redacted>, credential_ref=<redacted>, "
            "mailbox_address_identity_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class MailboxChangeReceipt:
    mailbox: Mailbox
    config_publication_ref: str
    request_id: str
    idempotency_key: str
    payload_digest: str

    def __post_init__(self) -> None:
        _safe_text(self.config_publication_ref, "config publication ref", allow_at=False)
        _safe_text(self.request_id, "request id", allow_at=False)
        _safe_text(self.idempotency_key, "idempotency key", allow_at=False)
        _digest(self.payload_digest)


@dataclass(frozen=True, slots=True)
class MailboxConnectorProjection:
    """Closed Task 1 mailbox configuration projection for Observer."""

    site_id: str
    observer_connector_instance_ref: str
    provider_kind: str
    entry_role: str
    business_purpose: str
    team_ref: str
    credential_ref: str
    inbound_enabled: bool
    mailbox_ref: str
    mailbox_config_revision: int
    activation_not_before: datetime
    projection_revision: int
    mailbox_address_identity_ref: str | None = None

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid projection site")
        _prefixed_ulid(
            self.observer_connector_instance_ref,
            "OCI",
            "observer connector instance ref",
        )
        if self.provider_kind not in {"imap_smtp", "wecom_app_mail"}:
            raise ValidationError("invalid connector projection provider")
        if self.entry_role not in ENTRY_ROLES:
            raise ValidationError("invalid connector projection entry role")
        if self.business_purpose not in PROCESSING_PURPOSES:
            raise ValidationError("invalid connector projection business purpose")
        _prefixed_ulid(self.team_ref, "TEM", "team ref")
        if (
            not isinstance(self.credential_ref, str)
            or len(self.credential_ref) > 128
            or _SECRET_REF.fullmatch(self.credential_ref) is None
        ):
            raise ValidationError("invalid connector projection credential ref")
        if not isinstance(self.inbound_enabled, bool):
            raise ValidationError("invalid connector projection inbound switch")
        _prefixed_ulid(self.mailbox_ref, "MBX", "mailbox ref")
        if (
            not isinstance(self.mailbox_config_revision, int)
            or isinstance(self.mailbox_config_revision, bool)
            or not 1 <= self.mailbox_config_revision <= 2_147_483_647
            or self.projection_revision != self.mailbox_config_revision
        ):
            raise ValidationError("invalid connector projection revision")
        _aware(self.activation_not_before, "activation not before")
        if self.mailbox_address_identity_ref is not None and (
            not isinstance(self.mailbox_address_identity_ref, str)
            or _OPAQUE_MAILBOX_ADDRESS_IDENTITY.fullmatch(self.mailbox_address_identity_ref) is None
        ):
            raise ValidationError("invalid connector projection mailbox address identity ref")

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "site_id": self.site_id,
            "observer_connector_instance_ref": self.observer_connector_instance_ref,
            "provider_kind": self.provider_kind,
            "entry_role": self.entry_role,
            "business_purpose": self.business_purpose,
            "team_ref": self.team_ref,
            "credential_ref": self.credential_ref,
            "inbound_enabled": self.inbound_enabled,
            "activation_watermark": {
                "mailbox_id": self.mailbox_ref,
                "mailbox_config_revision": self.mailbox_config_revision,
                "not_before": self.activation_not_before.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            "projection_revision": self.projection_revision,
        }
        if self.mailbox_address_identity_ref is not None:
            payload["mailbox_address_identity_ref"] = self.mailbox_address_identity_ref
        return {**payload, "projection_digest": canonical_digest(payload)}

    def __repr__(self) -> str:
        return (
            "MailboxConnectorProjection("
            f"site_id={self.site_id!r}, mailbox_ref={self.mailbox_ref!r}, "
            f"provider_kind={self.provider_kind!r}, "
            f"projection_revision={self.projection_revision}, "
            f"activation_not_before={self.activation_not_before!r}, "
            "credential_ref=<redacted>, mailbox_address_identity_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class PublicationParticipant:
    role: str
    identity_ref: str

    def __post_init__(self) -> None:
        if self.role not in PARTICIPANT_ROLES:
            raise ValidationError("invalid participant role")
        if not _OPAQUE_PUBLICATION_PARTICIPANT.fullmatch(self.identity_ref):
            raise ValidationError("participant identity must be opaque")

    def __repr__(self) -> str:
        return f"PublicationParticipant(role={self.role!r}, identity_ref=<redacted>)"


@dataclass(frozen=True, slots=True)
class EmailMessagePublication:
    publication_ref: str
    site_id: str
    processing_purpose: str
    mailbox_ref: str
    mailbox_config_revision: int
    observer_connector_instance_ref: str
    observer_delivery_ref: str
    received_at: datetime
    participants: tuple[PublicationParticipant, ...]
    subject_projection: str | None
    subject_digest: str | None
    message_id_digest: str | None
    in_reply_to_digest: str | None
    references_digests: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    publication_revision: int
    idempotency_key: str
    payload_digest: str

    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "publication_id",
            "site_id",
            "mailbox_id",
            "mailbox_config_revision",
            "observer_connector_instance_ref",
            "observer_delivery_ref",
            "received_at",
            "participants",
            "subject_projection",
            "subject_digest",
            "header_digests",
            "evidence_refs",
            "publication_revision",
            "idempotency_key",
        }
    )
    REQUIRED_WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "publication_id",
            "site_id",
            "mailbox_id",
            "mailbox_config_revision",
            "observer_connector_instance_ref",
            "observer_delivery_ref",
            "received_at",
            "participants",
            "header_digests",
            "evidence_refs",
            "publication_revision",
            "idempotency_key",
        }
    )

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid publication site")
        if self.processing_purpose not in PROCESSING_PURPOSES:
            raise ValidationError("invalid publication purpose")
        for name, value in (
            ("publication ref", self.publication_ref),
            ("mailbox ref", self.mailbox_ref),
            ("observer connector instance ref", self.observer_connector_instance_ref),
            ("observer delivery ref", self.observer_delivery_ref),
            ("idempotency key", self.idempotency_key),
        ):
            _safe_text(value, name, allow_at=False)
        if self.mailbox_config_revision < 1 or self.publication_revision < 1:
            raise ValidationError("invalid publication revision")
        _aware(self.received_at, "received_at")
        if not isinstance(self.participants, tuple) or not self.participants:
            raise ValidationError("publication participants must be a non-empty tuple")
        if not all(isinstance(item, PublicationParticipant) for item in self.participants):
            raise ValidationError("invalid publication participant")
        identities = tuple((item.role, item.identity_ref) for item in self.participants)
        if len(identities) != len(set(identities)):
            raise ValidationError("duplicate publication participant")
        if sum(item.role == "from" for item in self.participants) != 1:
            raise ValidationError("publication requires exactly one from participant")
        _optional_text(self.subject_projection, "subject projection", maximum=240)
        if (self.subject_projection is None) == (self.subject_digest is None):
            raise ValidationError("publication requires exactly one subject representation")
        if self.subject_digest is not None:
            _digest(self.subject_digest, "subject digest")
        if self.message_id_digest is not None:
            _digest(self.message_id_digest, "message id digest")
        if self.in_reply_to_digest is not None:
            _digest(self.in_reply_to_digest, "in reply to digest")
        if not isinstance(self.references_digests, tuple) or len(self.references_digests) > 100:
            raise ValidationError("invalid references digests")
        for value in self.references_digests:
            _digest(value, "reference digest")
        if len(self.references_digests) != len(set(self.references_digests)):
            raise ValidationError("duplicate reference digest")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValidationError("publication requires evidence")
        for value in self.evidence_refs:
            _safe_text(value, "evidence ref", maximum=512, allow_at=False)
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValidationError("duplicate evidence ref")
        _digest(self.payload_digest, "payload digest")

    @classmethod
    def from_wire(
        cls,
        value: object,
        *,
        processing_purpose: str,
        payload_digest: str,
    ) -> Self:
        if not isinstance(value, dict):
            raise ValidationError("publication wire value must be an object")
        keys = set(value)
        if not cls.REQUIRED_WIRE_FIELDS.issubset(keys) or not keys.issubset(cls.WIRE_FIELDS):
            raise ValidationError("unknown or missing publication wire field")
        has_projection = "subject_projection" in value
        has_digest = "subject_digest" in value
        if has_projection == has_digest:
            raise ValidationError("publication wire requires exactly one subject representation")
        publication_ref = _prefixed_ulid(value.get("publication_id"), "PUB", "publication id")
        mailbox_ref = _prefixed_ulid(value.get("mailbox_id"), "MBX", "mailbox id")
        connector_ref = _prefixed_ulid(
            value.get("observer_connector_instance_ref"),
            "OCI",
            "observer connector instance ref",
        )
        delivery_ref = _prefixed_ulid(
            value.get("observer_delivery_ref"), "DLV", "observer delivery ref"
        )
        site_id = value.get("site_id")
        if not isinstance(site_id, str):
            raise ValidationError("invalid publication site")
        received_value = value.get("received_at")
        if not isinstance(received_value, str):
            raise ValidationError("invalid received_at")
        try:
            received_at = datetime.fromisoformat(received_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid received_at") from exc
        participants_value = value.get("participants")
        if not isinstance(participants_value, list):
            raise ValidationError("invalid publication participants")
        participants: list[PublicationParticipant] = []
        for item in participants_value:
            if not isinstance(item, dict) or set(item) != {"address_role", "identity_ref"}:
                raise ValidationError("unknown or missing participant field")
            role = item.get("address_role")
            identity_ref = item.get("identity_ref")
            if not isinstance(role, str) or not isinstance(identity_ref, str):
                raise ValidationError("invalid participant value")
            participants.append(PublicationParticipant(role=role, identity_ref=identity_ref))
        headers = value.get("header_digests")
        if (
            not isinstance(headers, dict)
            or not headers
            or not set(headers).issubset({"message_id", "in_reply_to", "references"})
        ):
            raise ValidationError("unknown or missing header digest field")
        message_id = headers.get("message_id")
        in_reply_to = headers.get("in_reply_to")
        references = headers.get("references", [])
        if message_id is not None and not isinstance(message_id, str):
            raise ValidationError("invalid message id digest")
        if in_reply_to is not None and not isinstance(in_reply_to, str):
            raise ValidationError("invalid in reply to digest")
        if not isinstance(references, list) or not all(
            isinstance(item, str) for item in references
        ):
            raise ValidationError("invalid references digests")
        evidence_value = value.get("evidence_refs")
        if not isinstance(evidence_value, list):
            raise ValidationError("invalid evidence refs")
        evidence_refs = tuple(
            _prefixed_ulid(item, "EVR", "evidence ref") for item in evidence_value
        )
        idempotency_key = value.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise ValidationError("invalid idempotency key")
        mailbox_revision = value.get("mailbox_config_revision")
        publication_revision = value.get("publication_revision")
        if not isinstance(mailbox_revision, int) or isinstance(mailbox_revision, bool):
            raise ValidationError("invalid mailbox config revision")
        if not isinstance(publication_revision, int) or isinstance(publication_revision, bool):
            raise ValidationError("invalid publication revision")
        projection = value.get("subject_projection")
        subject_digest = value.get("subject_digest")
        if projection is not None and not isinstance(projection, str):
            raise ValidationError("invalid subject projection")
        if subject_digest is not None and not isinstance(subject_digest, str):
            raise ValidationError("invalid subject digest")
        return cls(
            publication_ref=publication_ref,
            site_id=site_id,
            processing_purpose=processing_purpose,
            mailbox_ref=mailbox_ref,
            mailbox_config_revision=mailbox_revision,
            observer_connector_instance_ref=connector_ref,
            observer_delivery_ref=delivery_ref,
            received_at=received_at,
            participants=tuple(participants),
            subject_projection=projection,
            subject_digest=subject_digest,
            message_id_digest=message_id,
            in_reply_to_digest=in_reply_to,
            references_digests=tuple(references),
            evidence_refs=evidence_refs,
            publication_revision=publication_revision,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    def to_wire(self) -> dict[str, object]:
        header_digests: dict[str, object] = {}
        if self.message_id_digest is not None:
            header_digests["message_id"] = self.message_id_digest
        if self.in_reply_to_digest is not None:
            header_digests["in_reply_to"] = self.in_reply_to_digest
        if self.references_digests:
            header_digests["references"] = list(self.references_digests)
        wire: dict[str, object] = {
            "publication_id": self.publication_ref,
            "site_id": self.site_id,
            "mailbox_id": self.mailbox_ref,
            "mailbox_config_revision": self.mailbox_config_revision,
            "observer_connector_instance_ref": self.observer_connector_instance_ref,
            "observer_delivery_ref": self.observer_delivery_ref,
            "received_at": self.received_at.isoformat().replace("+00:00", "Z"),
            "participants": [
                {"address_role": item.role, "identity_ref": item.identity_ref}
                for item in self.participants
            ],
            "header_digests": header_digests,
            "evidence_refs": list(self.evidence_refs),
            "publication_revision": self.publication_revision,
            "idempotency_key": self.idempotency_key,
        }
        if self.subject_projection is not None:
            wire["subject_projection"] = self.subject_projection
        else:
            wire["subject_digest"] = self.subject_digest
        return wire

    @property
    def subject_fact_digest(self) -> str:
        if self.subject_digest is not None:
            return self.subject_digest
        if self.subject_projection is None:  # pragma: no cover - protected by validation
            raise RuntimeError("validated publication lost its subject representation")
        return canonical_digest(self.subject_projection)

    def __repr__(self) -> str:
        return (
            "EmailMessagePublication("
            f"publication_ref={self.publication_ref!r}, site_id={self.site_id!r}, "
            f"mailbox_ref={self.mailbox_ref!r}, received_at={self.received_at!r}, "
            f"participant_count={len(self.participants)}, "
            f"evidence_count={len(self.evidence_refs)}, "
            "subject_projection=<redacted>, participants=<redacted>, evidence_refs=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    receipt_ref: str
    publication_ref: str
    site_id: str
    mailbox_ref: str
    observer_delivery_ref: str
    message_ref: str
    inbox_item_ref: str
    payload_digest: str
    received_at: datetime

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid receipt site")
        for name, value in (
            ("receipt ref", self.receipt_ref),
            ("publication ref", self.publication_ref),
            ("mailbox ref", self.mailbox_ref),
            ("observer delivery ref", self.observer_delivery_ref),
            ("message ref", self.message_ref),
            ("inbox item ref", self.inbox_item_ref),
        ):
            _safe_text(value, name, allow_at=False)
        _digest(self.payload_digest)
        _aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class ChannelMessage:
    message_ref: str
    site_id: str
    direction: str
    received_at: datetime
    participants: tuple[PublicationParticipant, ...]
    subject_projection: str | None
    subject_digest: str
    message_id_digest: str
    in_reply_to_digest: str | None
    references_digests: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    provider: str
    observer_delivery_refs: tuple[str, ...]
    revision: int

    def __post_init__(self) -> None:
        if self.direction != "inbound":
            raise ValidationError("invalid message direction")
        if self.provider not in PROVIDERS:
            raise ValidationError("invalid message provider")
        if self.revision < 1:
            raise ValidationError("invalid message revision")
        _aware(self.received_at, "received_at")
        _digest(self.subject_digest)
        _digest(self.message_id_digest)
        if self.in_reply_to_digest is not None:
            _digest(self.in_reply_to_digest)

    def __repr__(self) -> str:
        return (
            "ChannelMessage("
            f"message_ref={self.message_ref!r}, site_id={self.site_id!r}, "
            f"direction={self.direction!r}, revision={self.revision}, "
            "participants=<redacted>, subject_projection=<redacted>, evidence_refs=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class InboxItem:
    inbox_item_ref: str
    site_id: str
    mailbox_ref: str
    message_ref: str
    team_ref: str
    assignee_user_ref: str | None
    priority: int
    sla_due_at: datetime | None
    state: str
    conversation_ref: str | None
    business_links: tuple[str, ...]
    revision: int
    received_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.state not in INBOX_STATES:
            raise ValidationError("invalid inbox state")
        if self.revision < 1:
            raise ValidationError("invalid inbox revision")
        _aware(self.received_at, "received_at")
        _aware(self.updated_at, "updated_at")
        if self.sla_due_at is not None:
            _aware(self.sla_due_at, "sla_due_at")
        if self.updated_at < self.received_at:
            raise ValidationError("inbox timestamp regression")

    @classmethod
    def new(
        cls,
        *,
        site_id: str,
        mailbox_ref: str,
        message_ref: str,
        team_ref: str,
        received_at: datetime,
        state: str = "identity_pending",
    ) -> Self:
        return cls(
            inbox_item_ref=stable_ref("INB", site_id, mailbox_ref, message_ref),
            site_id=site_id,
            mailbox_ref=mailbox_ref,
            message_ref=message_ref,
            team_ref=team_ref,
            assignee_user_ref=None,
            priority=0,
            sla_due_at=None,
            state=state,
            conversation_ref=None,
            business_links=(),
            revision=1,
            received_at=received_at,
            updated_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class IntakeResult:
    receipt: PublicationReceipt
    message: ChannelMessage
    inbox_item: InboxItem


@dataclass(frozen=True, slots=True)
class IdentityProjection:
    site_id: str
    processing_purpose: str
    opaque_address_ref: str
    external_identity_ref: str
    external_identity_revision: int
    identity_type: str
    team_ref: str
    status: str
    projection_receipt_ref: str
    observed_at: datetime
    payload_digest: str

    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "site_id",
            "processing_purpose",
            "opaque_address_ref",
            "external_identity_ref",
            "external_identity_revision",
            "identity_type",
            "team_ref",
            "status",
            "projection_receipt",
            "observed_at",
        }
    )

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid projection site")
        if self.processing_purpose not in PROCESSING_PURPOSES:
            raise ValidationError("invalid projection purpose")
        if not _OPAQUE_EMAIL_IDENTITY.fullmatch(self.opaque_address_ref):
            raise ValidationError("projection address ref must be opaque")
        _safe_text(self.external_identity_ref, "external identity ref", allow_at=False)
        if self.external_identity_revision < 1:
            raise ValidationError("invalid external identity revision")
        if self.identity_type not in {"User", "Party"}:
            raise ValidationError("invalid identity type")
        _safe_text(self.team_ref, "team ref", allow_at=False)
        if self.status not in {"confirmed", "revoked"}:
            raise ValidationError("invalid identity status")
        _safe_text(self.projection_receipt_ref, "projection receipt", allow_at=False)
        _aware(self.observed_at, "observed_at")
        _digest(self.payload_digest)

    @classmethod
    def from_wire(cls, value: object, *, payload_digest: str) -> Self:
        if not isinstance(value, dict) or set(value) != cls.WIRE_FIELDS:
            raise ValidationError("unknown or missing identity projection field")
        site_id = _wire_string(value.get("site_id"), "projection site")
        purpose = _wire_string(value.get("processing_purpose"), "projection purpose")
        opaque_ref = _wire_string(value.get("opaque_address_ref"), "opaque address ref")
        revision = value.get("external_identity_revision")
        identity_type = _wire_string(value.get("identity_type"), "identity type")
        status = _wire_string(value.get("status"), "identity status")
        receipt = _wire_string(value.get("projection_receipt"), "projection receipt")
        observed = _wire_string(value.get("observed_at"), "observed_at")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ValidationError("invalid external identity revision")
        external_ref = _prefixed_ulid(
            value.get("external_identity_ref"), "EID", "external identity ref"
        )
        team_ref = _prefixed_ulid(value.get("team_ref"), "TEM", "team ref")
        try:
            observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid observed_at") from exc
        return cls(
            site_id=site_id,
            processing_purpose=purpose,
            opaque_address_ref=opaque_ref,
            external_identity_ref=external_ref,
            external_identity_revision=revision,
            identity_type=identity_type,
            team_ref=team_ref,
            status=status,
            projection_receipt_ref=receipt,
            observed_at=observed_at,
            payload_digest=payload_digest,
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "processing_purpose": self.processing_purpose,
            "opaque_address_ref": self.opaque_address_ref,
            "external_identity_ref": self.external_identity_ref,
            "external_identity_revision": self.external_identity_revision,
            "identity_type": self.identity_type,
            "team_ref": self.team_ref,
            "status": self.status,
            "projection_receipt": self.projection_receipt_ref,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
        }

    def __repr__(self) -> str:
        return (
            "IdentityProjection("
            f"site_id={self.site_id!r}, processing_purpose={self.processing_purpose!r}, "
            f"external_identity_revision={self.external_identity_revision}, "
            f"identity_type={self.identity_type!r}, status={self.status!r}, "
            "opaque_address_ref=<redacted>, external_identity_ref=<redacted>, team_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AuthorityRoute:
    route_status: str
    party_ref: str | None
    party_revision: int | None
    team_ref: str | None
    team_revision: int | None
    owner_user_ref: str | None
    owner_eligibility_revision: str | None
    safe_reason_code: str | None
    resolved_at: datetime

    ASSIGNED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "route_status",
            "party_ref",
            "party_revision",
            "team_ref",
            "team_revision",
            "owner_user_ref",
            "owner_eligibility_revision",
            "resolved_at",
        }
    )
    UNASSIGNED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"route_status", "safe_reason_code", "resolved_at"}
    )

    def __post_init__(self) -> None:
        _aware(self.resolved_at, "resolved_at")
        if self.route_status == "assigned":
            if (
                not self.party_ref
                or not self.team_ref
                or not self.owner_user_ref
                or not self.party_revision
                or not self.team_revision
                or not self.owner_eligibility_revision
                or self.safe_reason_code is not None
            ):
                raise ValidationError("invalid assigned authority route")
            _digest(self.owner_eligibility_revision, "owner eligibility revision")
        elif self.route_status == "unassigned":
            if (
                self.safe_reason_code is None
                or not _SAFE_CODE.fullmatch(self.safe_reason_code)
                or any(
                    value is not None
                    for value in (
                        self.party_ref,
                        self.party_revision,
                        self.team_ref,
                        self.team_revision,
                        self.owner_user_ref,
                        self.owner_eligibility_revision,
                    )
                )
            ):
                raise ValidationError("invalid unassigned authority route")
        else:
            raise ValidationError("invalid authority route status")

    @classmethod
    def assigned(
        cls,
        *,
        party_ref: str,
        party_revision: int,
        team_ref: str,
        team_revision: int,
        owner_user_ref: str,
        owner_eligibility_revision: str,
        resolved_at: datetime,
    ) -> Self:
        return cls(
            "assigned",
            party_ref,
            party_revision,
            team_ref,
            team_revision,
            owner_user_ref,
            owner_eligibility_revision,
            None,
            resolved_at,
        )

    @classmethod
    def unassigned(cls, safe_reason_code: str, resolved_at: datetime) -> Self:
        return cls(
            "unassigned",
            None,
            None,
            None,
            None,
            None,
            None,
            safe_reason_code,
            resolved_at,
        )

    @classmethod
    def from_wire(cls, value: object) -> Self:
        if not isinstance(value, dict):
            raise ValidationError("authority route must be an object")
        route_status = value.get("route_status")
        resolved = value.get("resolved_at")
        if not isinstance(resolved, str):
            raise ValidationError("invalid authority resolved_at")
        try:
            resolved_at = datetime.fromisoformat(resolved.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("invalid authority resolved_at") from exc
        if route_status == "assigned":
            if set(value) != cls.ASSIGNED_FIELDS:
                raise ValidationError("unknown or missing assigned authority field")
            party_revision = value.get("party_revision")
            team_revision = value.get("team_revision")
            owner_ref = value.get("owner_user_ref")
            owner_revision = value.get("owner_eligibility_revision")
            if (
                not isinstance(party_revision, int)
                or isinstance(party_revision, bool)
                or not isinstance(team_revision, int)
                or isinstance(team_revision, bool)
                or not isinstance(owner_ref, str)
                or not isinstance(owner_revision, str)
            ):
                raise ValidationError("invalid assigned authority value")
            return cls.assigned(
                party_ref=_prefixed_ulid(value.get("party_ref"), "PTY", "party ref"),
                party_revision=party_revision,
                team_ref=_prefixed_ulid(value.get("team_ref"), "TEM", "team ref"),
                team_revision=team_revision,
                owner_user_ref=owner_ref,
                owner_eligibility_revision=owner_revision,
                resolved_at=resolved_at,
            )
        if route_status == "unassigned":
            if set(value) != cls.UNASSIGNED_FIELDS:
                raise ValidationError("unknown or missing unassigned authority field")
            reason = value.get("safe_reason_code")
            if not isinstance(reason, str):
                raise ValidationError("invalid authority reason")
            return cls.unassigned(reason, resolved_at)
        raise ValidationError("invalid authority route status")

    def to_wire(self) -> dict[str, object]:
        if self.route_status == "assigned":
            return {
                "route_status": "assigned",
                "party_ref": self.party_ref,
                "party_revision": self.party_revision,
                "team_ref": self.team_ref,
                "team_revision": self.team_revision,
                "owner_user_ref": self.owner_user_ref,
                "owner_eligibility_revision": self.owner_eligibility_revision,
                "resolved_at": self.resolved_at.isoformat().replace("+00:00", "Z"),
            }
        return {
            "route_status": "unassigned",
            "safe_reason_code": self.safe_reason_code,
            "resolved_at": self.resolved_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    decision_ref: str
    site_id: str
    inbox_item_ref: str
    mailbox_ref: str
    route_status: str
    team_ref: str
    party_ref: str | None
    party_revision: int | None
    owner_user_ref: str | None
    owner_eligibility_revision: str | None
    safe_reason_code: str | None
    decided_at: datetime

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid route decision site")
        for name, value in (
            ("decision ref", self.decision_ref),
            ("inbox item ref", self.inbox_item_ref),
            ("mailbox ref", self.mailbox_ref),
            ("team ref", self.team_ref),
        ):
            _safe_text(value, name, allow_at=False)
        _aware(self.decided_at, "decided_at")
        if self.route_status == "assigned":
            if self.owner_user_ref is None or self.safe_reason_code is not None:
                raise ValidationError("invalid assigned route decision")
            authority_values = (
                self.party_ref,
                self.party_revision,
                self.owner_eligibility_revision,
            )
            if self.party_ref is None:
                if any(value is not None for value in authority_values):
                    raise ValidationError("invalid rule route decision")
            else:
                if self.party_revision is None or self.owner_eligibility_revision is None:
                    raise ValidationError("invalid authority route decision")
                if (
                    isinstance(self.party_revision, bool)
                    or not isinstance(self.party_revision, int)
                    or self.party_revision < 1
                ):
                    raise ValidationError("invalid party revision")
                _digest(self.owner_eligibility_revision, "owner eligibility revision")
        elif self.route_status == "unassigned":
            if any(
                value is not None
                for value in (
                    self.party_ref,
                    self.party_revision,
                    self.owner_user_ref,
                    self.owner_eligibility_revision,
                )
            ):
                raise ValidationError("invalid unassigned route decision")
            if self.safe_reason_code is None or not _SAFE_CODE.fullmatch(self.safe_reason_code):
                raise ValidationError("invalid route reason")
        else:
            raise ValidationError("invalid route decision status")


@dataclass(frozen=True, slots=True)
class RoutingRule:
    rule_ref: str
    site_id: str
    team_ref: str
    mailbox_ref: str
    owner_user_ref: str
    priority: int
    revision: int
    enabled: bool

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid routing rule site")
        for name, value in (
            ("rule ref", self.rule_ref),
            ("team ref", self.team_ref),
            ("mailbox ref", self.mailbox_ref),
            ("owner user ref", self.owner_user_ref),
        ):
            _safe_text(value, name)
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not 0 <= self.priority <= 1000
        ):
            raise ValidationError("invalid routing rule priority")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValidationError("invalid routing rule revision")
        if not isinstance(self.enabled, bool):
            raise ValidationError("invalid routing rule switch")


@dataclass(frozen=True, slots=True)
class ThreadSuggestion:
    suggestion_ref: str
    site_id: str
    team_ref: str
    left_inbox_ref: str
    right_inbox_ref: str
    signals: tuple[str, ...]
    confidence: float
    status: str
    revision: int
    reviewed_by: str | None
    reviewed_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid thread suggestion site")
        if self.left_inbox_ref == self.right_inbox_ref:
            raise ValidationError("thread suggestion requires distinct inbox items")
        if (
            not isinstance(self.signals, tuple)
            or not self.signals
            or len(self.signals) > 20
            or len(self.signals) != len(set(self.signals))
        ):
            raise ValidationError("invalid thread suggestion signals")
        for signal in self.signals:
            _safe_text(signal, "thread suggestion signal", maximum=80, allow_at=False)
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= self.confidence <= 1
        ):
            raise ValidationError("invalid thread suggestion confidence")
        if self.status not in {"proposed", "accepted", "rejected", "expired"}:
            raise ValidationError("invalid thread suggestion status")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValidationError("invalid thread suggestion revision")
        _aware(self.created_at, "created_at")
        if self.status == "proposed":
            if self.reviewed_by is not None or self.reviewed_at is not None:
                raise ValidationError("unreviewed suggestion has review metadata")
        elif self.reviewed_by is None or self.reviewed_at is None:
            raise ValidationError("reviewed suggestion requires review metadata")
        if self.reviewed_at is not None:
            _aware(self.reviewed_at, "reviewed_at")


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_ref: str
    site_id: str
    team_ref: str
    party_ref: str | None
    contact_ref: str | None
    owner_user_ref: str | None
    lifecycle_state: str
    first_message_at: datetime
    last_message_at: datetime
    message_refs: tuple[str, ...]
    inbox_item_refs: tuple[str, ...]
    revision: int

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid conversation site")
        if self.lifecycle_state not in {"open", "closed"}:
            raise ValidationError("invalid conversation lifecycle")
        if (
            not isinstance(self.message_refs, tuple)
            or not isinstance(self.inbox_item_refs, tuple)
            or not self.message_refs
            or len(self.message_refs) != len(self.inbox_item_refs)
            or len(self.message_refs) != len(set(self.message_refs))
            or len(self.inbox_item_refs) != len(set(self.inbox_item_refs))
        ):
            raise ValidationError("invalid conversation members")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValidationError("invalid conversation revision")
        _aware(self.first_message_at, "first_message_at")
        _aware(self.last_message_at, "last_message_at")
        if self.last_message_at < self.first_message_at:
            raise ValidationError("conversation timestamp regression")


@dataclass(frozen=True, slots=True)
class Draft:
    draft_ref: str
    site_id: str
    inbox_item_ref: str
    conversation_ref: str | None
    content_evidence_ref: str
    content_digest: str
    revision: int
    state: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.state not in {"editable", "discarded", "terminal"}:
            raise ValidationError("invalid draft state")
        if self.revision < 1:
            raise ValidationError("invalid draft revision")
        _digest(self.content_digest, "content digest")
        _aware(self.updated_at, "updated_at")

    def __repr__(self) -> str:
        return (
            "Draft("
            f"draft_ref={self.draft_ref!r}, site_id={self.site_id!r}, "
            f"revision={self.revision}, state={self.state!r}, "
            "content_evidence_ref=<redacted>, content_digest=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SendOutbox:
    send_ref: str
    site_id: str
    state: str
    payload_digest: str

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid send outbox site")
        _safe_text(self.send_ref, "send ref", allow_at=False)
        if self.state != "disabled":
            raise ValidationError("send outbox is not authorized")
        _digest(self.payload_digest)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    audit_ref: str
    site_id: str
    actor_ref: str
    event_type: str
    subject_ref: str
    request_id: str
    idempotency_key: str
    payload_digest: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not _SITE.fullmatch(self.site_id):
            raise ValidationError("invalid audit site")
        for name, value in (
            ("audit ref", self.audit_ref),
            ("actor ref", self.actor_ref),
            ("event type", self.event_type),
            ("subject ref", self.subject_ref),
            ("request id", self.request_id),
            ("idempotency key", self.idempotency_key),
        ):
            _safe_text(value, name)
        _digest(self.payload_digest)
        _aware(self.occurred_at, "occurred_at")

    def __repr__(self) -> str:
        return (
            "AuditEvent("
            f"audit_ref={self.audit_ref!r}, site_id={self.site_id!r}, "
            f"event_type={self.event_type!r}, occurred_at={self.occurred_at!r}, "
            "actor_ref=<redacted>, subject_ref=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ContentProjection:
    projection_ref: str
    site_id: str
    kind: str
    identity_ref: str | None
    evidence_ref: str
    expires_at: datetime
    observer_expiration_receipt_ref: str | None
    payload_digest: str
    active_draft_ref: str | None
    confirmed: bool

    def __post_init__(self) -> None:
        if self.kind not in {
            "unconfirmed_display",
            "unconfirmed_subject",
            "draft_projection",
            "confirmed_crm_metadata",
        }:
            raise ValidationError("invalid content projection kind")
        if self.identity_ref is not None and not _OPAQUE_EMAIL_IDENTITY.fullmatch(
            self.identity_ref
        ):
            raise ValidationError("content identity ref must be opaque")
        _aware(self.expires_at, "expires_at")
        _digest(self.payload_digest)
