from __future__ import annotations

from threading import RLock
from typing import Protocol

from .models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
    record_id_for,
)

FORBIDDEN_GATE4_FIELDS = frozenset(
    {
        "verified_fact",
        "verified_facts",
        "conflict",
        "conflicts",
        "decision",
        "decisions",
        "action",
        "actions",
        "action_proposal",
        "draft",
        "draft_mutation",
        "approved_command",
        "command",
        "review_case",
        "agent_task",
    }
)


class ContextRepository(Protocol):
    def save(
        self,
        scope: TenantScope,
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata: ...

    def get(
        self,
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
    ) -> RecordMetadata | None: ...


def validate_governed_record(
    scope: TenantScope,
    kind: RecordKind,
    envelope: GovernedEnvelope,
) -> str:
    if scope.site_id != envelope.site_id:
        raise ValidationError("TenantScope site does not match governed envelope")
    if scope.processing_purpose != envelope.processing_purpose:
        raise ValidationError("TenantScope purpose does not match governed envelope")
    forbidden = FORBIDDEN_GATE4_FIELDS.intersection(envelope.payload)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise ValidationError(f"Gate 4 fields are forbidden at Gate 3: {names}")
    if kind is RecordKind.FACT_PROPOSAL:
        fact = envelope.payload.get("fact")
        if not isinstance(fact, dict) or fact.get("status") != "proposed":
            raise ValidationError("fact status must remain proposed")
    if (
        kind is RecordKind.ENTITY_RESOLUTION_PROPOSAL
        and envelope.payload.get("status") != "proposed"
    ):
        raise ValidationError("entity resolution status must remain proposed")
    return record_id_for(kind, envelope.payload)


class InMemoryContextRepository:
    """A deterministic, process-local repository for unit tests.

    Raw payloads are retained privately to model persistence, while every
    public method returns immutable metadata only.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, RecordKind, str], RecordMetadata] = {}
        self._payloads: dict[tuple[str, RecordKind, str], GovernedEnvelope] = {}
        self._idempotency: dict[tuple[str, str], RecordMetadata] = {}
        self._lock = RLock()

    def save(
        self,
        scope: TenantScope,
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata:
        record_id = validate_governed_record(scope, kind, envelope)
        idempotency_key = (scope.site_id, envelope.idempotency_key)
        record_key = (scope.site_id, kind, record_id)
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                if existing.payload_digest != envelope.payload_digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different payload"
                    )
                return existing
            if record_key in self._records:
                raise ValidationError("record_id already exists with another request")
            metadata = RecordMetadata.create(
                kind=kind,
                record_id=record_id,
                envelope=envelope,
            )
            self._records[record_key] = metadata
            self._payloads[record_key] = envelope
            self._idempotency[idempotency_key] = metadata
            return metadata

    def get(
        self,
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
    ) -> RecordMetadata | None:
        return self._records.get((scope.site_id, kind, record_id))
