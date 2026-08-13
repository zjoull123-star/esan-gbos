from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CONNECTOR_NAMES = frozenset(
    {
        "email",
        "wecom",
        "whatsapp",
        "phone",
        "meeting",
        "file",
        "manual_import",
        "wechat_workphone",
    }
)
CHANNEL_NAMES = frozenset({"email", "chat", "call", "meeting", "document", "manual_import"})
CONSENT_BASES = frozenset(
    {
        "consent",
        "contract",
        "legal_obligation",
        "legitimate_interest",
        "manual_import_pending_review",
        "pilot_deferred_review",
    }
)
DATA_CLASSIFICATIONS = frozenset({"Public", "Internal", "Confidential", "Restricted"})
PROCESSING_PURPOSES = frozenset(
    {
        "business_operations",
        "observation_processing",
        "entity_resolution",
        "email_address_identity_confirmation",
        "customer_service",
        "sales_follow_up",
        "procurement_coordination",
        "product_sample_management",
        "risk_review",
        "metric_reporting",
        "audit_compliance",
    }
)


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_identifier(value: str, field_name: str, *, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {field_name}")


def _require_cursor(value: str | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or len(value) > 4096):
        raise ValueError(f"invalid {field_name}")


def stable_ulid(namespace: str, *parts: str) -> str:
    """Return a deterministic Crockford Base32 identifier with ULID wire shape."""

    material = "\x1f".join((namespace, *parts)).encode()
    value = int.from_bytes(hashlib.sha256(material).digest()[:16])
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        value, remainder = divmod(value, 32)
        encoded[index] = _CROCKFORD[remainder]
    return "".join(encoded)


def _summary_digest(value: str | bytes) -> str:
    content = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class TenantScope:
    site_id: str
    processing_purpose: str

    def __post_init__(self) -> None:
        if not _SITE_ID.fullmatch(self.site_id):
            raise ValueError("invalid site_id")
        if self.processing_purpose not in PROCESSING_PURPOSES:
            raise ValueError("unknown processing_purpose")


@dataclass(frozen=True, slots=True)
class Participant:
    role: str
    identity_ref: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"internal", "external", "system", "unknown"}:
            raise ValueError("invalid participant role")
        if not self.identity_ref or len(self.identity_ref) > 256:
            raise ValueError("invalid identity_ref")
        if self.display_name is not None and len(self.display_name) > 256:
            raise ValueError("display_name is too long")


@dataclass(frozen=True, slots=True)
class ConnectorKey:
    connector: str
    instance_id: str

    def __post_init__(self) -> None:
        if self.connector not in CONNECTOR_NAMES:
            raise ValueError("invalid connector")
        _require_identifier(self.instance_id, "instance_id")


@dataclass(frozen=True, slots=True)
class RawDelivery:
    delivery_id: str
    exact_bytes: bytes
    media_type: str
    received_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.delivery_id, "delivery_id", maximum=512)
        if not isinstance(self.exact_bytes, bytes):
            raise TypeError("exact_bytes must be bytes")
        _require_identifier(self.media_type, "media_type", maximum=255)
        _require_aware(self.received_at, "received_at")

    def __repr__(self) -> str:
        return (
            "RawDelivery("
            f"delivery_id_sha256='{_summary_digest(self.delivery_id)}', "
            f"media_type_sha256='{_summary_digest(self.media_type)}', "
            f"received_at={self.received_at.isoformat()!r}, "
            f"byte_size={len(self.exact_bytes)}, "
            f"body_sha256='{_summary_digest(self.exact_bytes)}'"
            ")"
        )


@dataclass(frozen=True, slots=True)
class ConnectorItem:
    provider_event_id: str
    occurred_at: datetime
    source_cursor: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_identifier(self.provider_event_id, "provider_event_id", maximum=512)
        _require_aware(self.occurred_at, "occurred_at")
        _require_cursor(self.source_cursor, "source_cursor")
        if not self.source_cursor:
            raise ValueError("invalid source_cursor")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def __repr__(self) -> str:
        return (
            "ConnectorItem("
            f"provider_event_id_sha256='{_summary_digest(self.provider_event_id)}', "
            f"occurred_at={self.occurred_at.isoformat()!r}, "
            f"source_cursor_sha256='{_summary_digest(self.source_cursor)}', "
            f"payload_entries={len(self.payload)}"
            ")"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceArtifact:
    media_type: str
    locator: str
    role: str
    content: bytes | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        if (self.content is None) == (self.reference is None):
            raise ValueError("evidence requires exactly one of content or reference")
        if self.content is not None and not isinstance(self.content, bytes):
            raise TypeError("evidence content must be bytes")
        if self.reference is not None:
            _require_identifier(self.reference, "reference", maximum=512)
        _require_identifier(self.media_type, "media_type", maximum=255)
        _require_identifier(self.locator, "locator", maximum=512)
        _require_identifier(self.role, "role", maximum=80)


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedObservationInput:
    channel: str
    participants: tuple[Participant, ...]
    evidence: tuple[EvidenceArtifact, ...]
    consent_basis: str
    data_classification: str
    retention_class: str
    original_language: str
    correlation_id: str

    def __post_init__(self) -> None:
        if self.channel not in CHANNEL_NAMES:
            raise ValueError("invalid channel")
        if not isinstance(self.participants, tuple) or not isinstance(self.evidence, tuple):
            raise TypeError("participants and evidence must be tuples")
        if not self.participants or not all(
            isinstance(participant, Participant) for participant in self.participants
        ):
            raise ValueError("participants must contain Participant values")
        if not self.evidence or not all(
            isinstance(artifact, EvidenceArtifact) for artifact in self.evidence
        ):
            raise ValueError("evidence must contain EvidenceArtifact values")
        if self.consent_basis not in CONSENT_BASES:
            raise ValueError("invalid consent_basis")
        if self.data_classification not in DATA_CLASSIFICATIONS:
            raise ValueError("invalid data_classification")
        _require_identifier(self.retention_class, "retention_class", maximum=80)
        _require_identifier(self.original_language, "original_language", maximum=35)
        if len(self.original_language) < 2:
            raise ValueError("invalid original_language")
        _require_identifier(self.correlation_id, "correlation_id")


@dataclass(frozen=True, slots=True)
class ConnectorBatch:
    expected_cursor: str | None
    next_cursor: str | None
    items: tuple[ConnectorItem, ...]

    def __post_init__(self) -> None:
        _require_cursor(self.expected_cursor, "expected_cursor")
        _require_cursor(self.next_cursor, "next_cursor")
        if not isinstance(self.items, tuple):
            raise TypeError("items must be a tuple")
        if not all(isinstance(item, ConnectorItem) for item in self.items):
            raise TypeError("items must contain ConnectorItem values")
        provider_ids = tuple(item.provider_event_id for item in self.items)
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("duplicate provider_event_id in connector batch")
        if self.items and not self.next_cursor:
            raise ValueError("next_cursor is required for a non-empty connector batch")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("transcript segment requires a positive half-open time range")
        _require_identifier(self.text, "text", maximum=20_000)


TranscriptSegments = tuple[TranscriptSegment, ...]


@dataclass(frozen=True, slots=True)
class ManualImportManifest:
    connector: str
    fixture_id: str
    occurred_at: datetime
    consent_basis: str
    data_classification: str
    retention_class: str
    participants: tuple[Participant, ...]
    correlation_id: str
    provider_event_id: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")
        if not self.fixture_id or len(self.fixture_id) > 256:
            raise ValueError("invalid fixture_id")
        if not self.correlation_id or len(self.correlation_id) > 256:
            raise ValueError("invalid correlation_id")
        if self.provider_event_id is not None and (
            not self.provider_event_id or len(self.provider_event_id) > 256
        ):
            raise ValueError("invalid provider_event_id")


@dataclass(frozen=True, slots=True)
class ManualImportMember:
    name: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("manual import content must be bytes")


@dataclass(frozen=True, slots=True)
class ByteLocator:
    """A zero-based, half-open locator over the exact stored byte sequence."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("locator must be a non-empty half-open byte range")

    def validate(self, content_length: int) -> None:
        if content_length < 0 or self.end > content_length:
            raise ValueError("locator is outside content bounds")

    def extract(self, content: bytes) -> bytes:
        self.validate(len(content))
        return content[self.start : self.end]


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_ref: str
    sha256: str
    size: int
    media_type: str


@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    event_id: str
    site_id: str
    processing_purpose: str
    connector: str
    channel: str
    occurred_at: datetime
    ingested_at: datetime
    original_language: str
    participants: tuple[Participant, ...]
    evidence_refs: tuple[str, ...]
    raw_sha256: str
    consent_basis: str
    data_classification: str
    retention_class: str
    correlation_id: str
    source_lineage: tuple[str, ...]
    processor_version: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    observation_event_id: str
    site_id: str
    processing_purpose: str
    data_classification: str
    source_lineage: tuple[str, ...]
    processor_version: str
    raw_sha256: str
    object_ref: str
    media_type: str
    locator: ByteLocator
    created_at: datetime
    retention_class: str


@dataclass(frozen=True, slots=True)
class FactProposal:
    fact_id: str
    site_id: str
    processing_purpose: str
    data_classification: str
    source_lineage: tuple[str, ...]
    processor_version: str
    rule_version: str
    output_version: str
    subject_ref: str
    predicate: str
    value: str
    summary_zh: str
    original_language: str
    confidence: float
    evidence_refs: tuple[str, ...]
    status: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class EntityResolutionProposal:
    proposal_id: str
    site_id: str
    processing_purpose: str
    data_classification: str
    source_lineage: tuple[str, ...]
    processor_version: str
    rule_version: str
    output_version: str
    observed_identity_ref: str
    candidate_identity_refs: tuple[str, ...]
    confidence: float
    evidence_refs: tuple[str, ...]
    status: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    fact_proposals: tuple[FactProposal, ...]
    entity_resolution_proposals: tuple[EntityResolutionProposal, ...]


@dataclass(frozen=True, slots=True)
class ImportResult:
    observation: CanonicalObservation
    evidence: tuple[EvidenceRecord, ...]
    fact_proposals: tuple[FactProposal, ...]
    entity_resolution_proposals: tuple[EntityResolutionProposal, ...]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    action: str
    site_id: str
    processing_purpose: str
    event_id: str
    evidence_ids: tuple[str, ...]
    body_sha256: str
    status: str
    recorded_at: datetime
