from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.observer.observer.models import (
    EntityResolutionProposal,
    EvidenceRecord,
    FactProposal,
    ImportResult,
    stable_ulid,
)

from .contracts import ContextContractValidator
from .models import (
    GovernedEnvelope,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)
from .repositories import ContextRepository


@dataclass(frozen=True, slots=True)
class _PendingRecord:
    scope: TenantScope
    kind: RecordKind
    envelope: GovernedEnvelope


class ContextPublisher:
    """Map Observer output to governed Context records and persist metadata only."""

    def __init__(
        self,
        repository: ContextRepository,
        validator: ContextContractValidator | None = None,
    ) -> None:
        self._repository = repository
        self._validator = validator or ContextContractValidator.repository_default()

    def publish(
        self,
        result: ImportResult,
        *,
        correlation_id: str,
        recorded_at: datetime | None = None,
    ) -> tuple[RecordMetadata, ...]:
        observation = result.observation
        if correlation_id != observation.correlation_id:
            raise ValidationError("correlation_id does not match the Observer import")
        scope = TenantScope(observation.site_id, observation.processing_purpose)
        self._validate_scope(result, scope)
        stable_recorded_at = recorded_at or observation.ingested_at
        _timestamp(stable_recorded_at)

        evidence_record_ids = {
            evidence.evidence_id: stable_ulid(
                "context-evidence-record",
                scope.site_id,
                scope.processing_purpose,
                evidence.evidence_id,
            )
            for evidence in result.evidence
        }
        if len(evidence_record_ids) != len(result.evidence):
            raise ValidationError("Observer evidence IDs must be unique")

        pending = (
            *(
                self._pending(
                    scope,
                    RecordKind.EVIDENCE,
                    self._evidence_payload(
                        result,
                        evidence,
                        evidence_record_ids[evidence.evidence_id],
                        stable_recorded_at,
                    ),
                )
                for evidence in result.evidence
            ),
            *(
                self._pending(
                    scope,
                    RecordKind.FACT_PROPOSAL,
                    self._fact_payload(
                        result,
                        proposal,
                        correlation_id,
                        evidence_record_ids,
                        stable_recorded_at,
                    ),
                )
                for proposal in result.fact_proposals
            ),
            *(
                self._pending(
                    scope,
                    RecordKind.ENTITY_RESOLUTION_PROPOSAL,
                    self._entity_payload(
                        result,
                        proposal,
                        correlation_id,
                        evidence_record_ids,
                        stable_recorded_at,
                    ),
                )
                for proposal in result.entity_resolution_proposals
            ),
        )
        return tuple(
            self._repository.save(record.scope, record.kind, record.envelope) for record in pending
        )

    def _pending(
        self,
        scope: TenantScope,
        kind: RecordKind,
        payload: dict[str, Any],
    ) -> _PendingRecord:
        self._validator.validate(kind, payload)
        record_id = str(
            payload[
                {
                    RecordKind.EVIDENCE: "evidence_record_id",
                    RecordKind.FACT_PROPOSAL: "fact_proposal_record_id",
                    RecordKind.ENTITY_RESOLUTION_PROPOSAL: ("entity_resolution_proposal_id"),
                }[kind]
            ]
        )
        envelope = GovernedEnvelope.from_payload(
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            idempotency_key=f"context-publish:{kind.value}:{record_id}",
            payload=payload,
        )
        return _PendingRecord(scope, kind, envelope)

    @staticmethod
    def _validate_scope(result: ImportResult, scope: TenantScope) -> None:
        records: Iterable[EvidenceRecord | FactProposal | EntityResolutionProposal] = (
            *result.evidence,
            *result.fact_proposals,
            *result.entity_resolution_proposals,
        )
        for record in records:
            if record.site_id != scope.site_id:
                raise ValidationError("Observer record site does not match observation site")
            if record.processing_purpose != scope.processing_purpose:
                raise ValidationError("Observer record purpose does not match observation purpose")

    @staticmethod
    def _evidence_payload(
        result: ImportResult,
        evidence: EvidenceRecord,
        evidence_record_id: str,
        recorded_at: datetime,
    ) -> dict[str, Any]:
        observation = result.observation
        return {
            "schema_version": "2.0",
            "evidence_record_id": evidence_record_id,
            "site_id": evidence.site_id,
            "processing_purpose": evidence.processing_purpose,
            "evidence_ref": {
                "schema_version": "1.0",
                "evidence_id": evidence.evidence_id,
                "observation_event_id": evidence.observation_event_id,
                "raw_sha256": evidence.raw_sha256,
                "object_ref": evidence.object_ref,
                "media_type": evidence.media_type,
                "locator": {
                    "message_start": evidence.locator.start,
                    "message_end": evidence.locator.end,
                },
                "created_at": _timestamp(recorded_at),
            },
            "source_lineage": _source_lineage(
                source_system=observation.connector,
                refs=(
                    *evidence.source_lineage,
                    evidence.observation_event_id,
                    evidence.evidence_id,
                ),
                retrieved_at=recorded_at,
                transformation_version=evidence.processor_version,
            ),
            "data_classification": evidence.data_classification,
            "review_status": "unreviewed",
            "recorded_at": _timestamp(recorded_at),
        }

    @staticmethod
    def _fact_payload(
        result: ImportResult,
        proposal: FactProposal,
        correlation_id: str,
        evidence_record_ids: dict[str, str],
        recorded_at: datetime,
    ) -> dict[str, Any]:
        observation = result.observation
        translated_evidence = _translate_evidence_refs(
            proposal.evidence_refs,
            evidence_record_ids,
        )
        record_id = stable_ulid(
            "context-fact-proposal-record",
            proposal.site_id,
            proposal.processing_purpose,
            proposal.fact_id,
            correlation_id,
        )
        used_characters = len(proposal.value)
        return {
            "schema_version": "1.0",
            "fact_proposal_record_id": record_id,
            "site_id": proposal.site_id,
            "processing_purpose": proposal.processing_purpose,
            "data_classification": proposal.data_classification,
            "fact": {
                "schema_version": "1.0",
                "fact_id": proposal.fact_id,
                "subject_ref": proposal.subject_ref,
                "predicate": proposal.predicate,
                "value": {"type": "text", "text": proposal.value},
                "confidence": proposal.confidence,
                "evidence_refs": translated_evidence,
                "model": {
                    "provider": "synthetic",
                    "model": proposal.processor_version,
                    "prompt_version": proposal.rule_version,
                },
                "status": proposal.status,
                "extracted_at": _timestamp(recorded_at),
            },
            "source_lineage": _source_lineage(
                source_system=observation.connector,
                refs=(
                    *proposal.source_lineage,
                    observation.event_id,
                    *proposal.evidence_refs,
                ),
                retrieved_at=recorded_at,
                transformation_version=proposal.processor_version,
            ),
            "valid_time": {
                "start": _timestamp(observation.occurred_at),
                "end": None,
            },
            "recorded_time": _timestamp(recorded_at),
            "processor": {
                "processor_id": proposal.processor_version,
                "kind": "deterministic_test_processor",
            },
            "processor_version": proposal.processor_version,
            "rule_version": proposal.rule_version,
            "output_version": proposal.output_version,
            "budget": {
                "unit": "characters",
                "limit_units": max(1, used_characters),
                "used_units": used_characters,
                "network_calls": 0,
                "tool_calls": 0,
            },
            "correlation_id": correlation_id,
        }

    @staticmethod
    def _entity_payload(
        result: ImportResult,
        proposal: EntityResolutionProposal,
        correlation_id: str,
        evidence_record_ids: dict[str, str],
        recorded_at: datetime,
    ) -> dict[str, Any]:
        observation = result.observation
        translated_evidence = _translate_evidence_refs(
            proposal.evidence_refs,
            evidence_record_ids,
        )
        record_id = stable_ulid(
            "context-entity-resolution-proposal",
            proposal.site_id,
            proposal.processing_purpose,
            proposal.proposal_id,
            correlation_id,
        )
        candidate_count = len(proposal.candidate_identity_refs)
        return {
            "schema_version": "1.0",
            "entity_resolution_proposal_id": record_id,
            "site_id": proposal.site_id,
            "processing_purpose": proposal.processing_purpose,
            "data_classification": proposal.data_classification,
            "status": proposal.status,
            "entity_type": "Contact",
            "source_entity_ref": proposal.observed_identity_ref,
            "candidates": [
                {
                    "entity_ref": candidate,
                    "confidence": proposal.confidence,
                    "matching_attributes": ["external_reference"],
                }
                for candidate in proposal.candidate_identity_refs
            ],
            "evidence_refs": translated_evidence,
            "source_lineage": _source_lineage(
                source_system=observation.connector,
                refs=(
                    *proposal.source_lineage,
                    observation.event_id,
                    *proposal.evidence_refs,
                ),
                retrieved_at=recorded_at,
                transformation_version=proposal.processor_version,
            ),
            "valid_time": {
                "start": _timestamp(observation.occurred_at),
                "end": None,
            },
            "recorded_time": _timestamp(recorded_at),
            "processor": {
                "processor_id": proposal.processor_version,
                "kind": "deterministic_test_processor",
            },
            "processor_version": proposal.processor_version,
            "rule_version": proposal.rule_version,
            "output_version": proposal.output_version,
            "budget": {
                "unit": "records",
                "limit_units": max(1, candidate_count),
                "used_units": candidate_count,
                "network_calls": 0,
                "tool_calls": 0,
            },
            "correlation_id": correlation_id,
        }


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Context wire timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _source_lineage(
    *,
    source_system: str,
    refs: Iterable[str],
    retrieved_at: datetime,
    transformation_version: str,
) -> dict[str, Any]:
    return {
        "source_system": source_system,
        "source_record_refs": list(dict.fromkeys(refs)),
        "retrieved_at": _timestamp(retrieved_at),
        "transformation_version": transformation_version,
        "evidence_status": "synthetic",
    }


def _translate_evidence_refs(
    evidence_refs: tuple[str, ...],
    evidence_record_ids: dict[str, str],
) -> list[str]:
    try:
        return [evidence_record_ids[evidence_id] for evidence_id in evidence_refs]
    except KeyError as exc:
        raise ValidationError(
            f"proposal references unpublished Observer evidence: {exc.args[0]}"
        ) from exc
