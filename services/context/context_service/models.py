from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

ALLOWED_PROCESSING_PURPOSES = frozenset(
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


class ValidationError(ValueError):
    """A governed persistence boundary rejected an invalid request."""


class IdempotencyConflict(ValidationError):
    """An idempotency key was replayed with a different payload digest."""


class RecordKind(StrEnum):
    EVIDENCE = "evidence_record"
    FACT_PROPOSAL = "fact_proposal"
    ENTITY_RESOLUTION_PROPOSAL = "entity_resolution_proposal"


RECORD_ID_FIELDS: dict[RecordKind, str] = {
    RecordKind.EVIDENCE: "evidence_record_id",
    RecordKind.FACT_PROPOSAL: "fact_proposal_record_id",
    RecordKind.ENTITY_RESOLUTION_PROPOSAL: "entity_resolution_proposal_id",
}


def canonical_payload_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("payload must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TenantScope:
    site_id: str
    processing_purpose: str

    def __post_init__(self) -> None:
        if not self.site_id or len(self.site_id) > 140:
            raise ValidationError("site_id must be present and at most 140 characters")
        if self.processing_purpose not in ALLOWED_PROCESSING_PURPOSES:
            raise ValidationError("processing_purpose is not governed or recognized")


@dataclass(frozen=True, slots=True)
class GovernedEnvelope:
    site_id: str
    processing_purpose: str
    idempotency_key: str
    payload_digest: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        TenantScope(self.site_id, self.processing_purpose)
        if not self.idempotency_key or len(self.idempotency_key) > 256:
            raise ValidationError("idempotency_key must be present and at most 256 characters")
        detached_payload = deepcopy(dict(self.payload))
        object.__setattr__(self, "payload", MappingProxyType(detached_payload))
        expected_digest = canonical_payload_digest(detached_payload)
        if self.payload_digest != expected_digest:
            raise ValidationError("payload_digest does not match canonical payload")
        if detached_payload.get("site_id") != self.site_id:
            raise ValidationError("payload site_id does not match envelope site")
        if detached_payload.get("processing_purpose") != self.processing_purpose:
            raise ValidationError("payload processing_purpose does not match envelope purpose")

    @classmethod
    def from_payload(
        cls,
        *,
        site_id: str,
        processing_purpose: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> GovernedEnvelope:
        return cls(
            site_id=site_id,
            processing_purpose=processing_purpose,
            idempotency_key=idempotency_key,
            payload_digest=canonical_payload_digest(payload),
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class RecordMetadata:
    kind: RecordKind
    record_id: str
    site_id: str
    processing_purpose: str
    idempotency_key: str
    payload_digest: str
    recorded_at: datetime

    @classmethod
    def create(
        cls,
        *,
        kind: RecordKind,
        record_id: str,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata:
        return cls(
            kind=kind,
            record_id=record_id,
            site_id=envelope.site_id,
            processing_purpose=envelope.processing_purpose,
            idempotency_key=envelope.idempotency_key,
            payload_digest=envelope.payload_digest,
            recorded_at=datetime.now(UTC),
        )


def record_id_for(kind: RecordKind, payload: Mapping[str, Any]) -> str:
    field = RECORD_ID_FIELDS[kind]
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value
