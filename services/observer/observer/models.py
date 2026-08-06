from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

_SITE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")
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


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def stable_ulid(namespace: str, *parts: str) -> str:
    """Return a deterministic Crockford Base32 identifier with ULID wire shape."""

    material = "\x1f".join((namespace, *parts)).encode()
    value = int.from_bytes(hashlib.sha256(material).digest()[:16])
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        value, remainder = divmod(value, 32)
        encoded[index] = _CROCKFORD[remainder]
    return "".join(encoded)


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
