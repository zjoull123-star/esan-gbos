from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from services.context.context_service.contracts import ContextContractValidator
from services.context.context_service.models import (
    GovernedEnvelope,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)
from services.observer.observer.models import (
    ByteLocator,
    CanonicalObservation,
    EntityResolutionProposal,
    EvidenceRecord,
    FactProposal,
    ImportResult,
    Participant,
)

NOW = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)


class RecordingContextRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[TenantScope, RecordKind, GovernedEnvelope]] = []

    def save(
        self,
        scope: TenantScope,
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata:
        self.calls.append((scope, kind, envelope))
        record_id_field = {
            RecordKind.EVIDENCE: "evidence_record_id",
            RecordKind.FACT_PROPOSAL: "fact_proposal_record_id",
            RecordKind.ENTITY_RESOLUTION_PROPOSAL: "entity_resolution_proposal_id",
        }[kind]
        return RecordMetadata(
            kind=kind,
            record_id=str(envelope.payload[record_id_field]),
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            idempotency_key=envelope.idempotency_key,
            payload_digest=envelope.payload_digest,
            recorded_at=NOW,
        )

    def get(
        self,
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
    ) -> RecordMetadata | None:
        del scope, kind, record_id
        return None


def _publisher_class() -> type[Any]:
    module = importlib.import_module("services.context.context_service.publisher")
    publisher = getattr(module, "ContextPublisher", None)
    assert publisher is not None, "ContextPublisher must exist"
    return cast(type[Any], publisher)


def _result(
    *,
    evidence_site: str = "site-a",
    fact_purpose: str = "observation_processing",
) -> ImportResult:
    observation = CanonicalObservation(
        event_id="event-001",
        site_id="site-a",
        processing_purpose="observation_processing",
        connector="manual_import",
        channel="manual_import",
        occurred_at=NOW,
        ingested_at=NOW,
        original_language="zh",
        participants=(Participant("external", "contact-observed-001"),),
        evidence_refs=("evidence-001", "evidence-002"),
        raw_sha256="a" * 64,
        consent_basis="consent",
        data_classification="Restricted",
        retention_class="R1-operational",
        correlation_id="corr-001",
        source_lineage=("manual_import:fixture-001",),
        processor_version="manual-import-v1",
    )
    evidence = tuple(
        EvidenceRecord(
            evidence_id=f"evidence-00{index}",
            observation_event_id=observation.event_id,
            site_id=evidence_site,
            processing_purpose="observation_processing",
            data_classification="Restricted",
            source_lineage=(observation.event_id,),
            processor_version="manual-import-v1",
            raw_sha256=str(index) * 64,
            object_ref=f"local-object://evidence-00{index}",
            media_type="text/plain",
            locator=ByteLocator(index - 1, index),
            created_at=NOW,
            retention_class="R1-operational",
        )
        for index in (1, 2)
    )
    fact = FactProposal(
        fact_id="fact-001",
        site_id="site-a",
        processing_purpose=fact_purpose,
        data_classification="Restricted",
        source_lineage=(observation.event_id, "evidence-001"),
        processor_version="deterministic-test-processor-v1",
        rule_version="observer-rules-v1",
        output_version="gate3-proposal-v1",
        subject_ref="contact-observed-001",
        predicate="communication_summary",
        value="客户需要一份样品。",
        summary_zh="客户需要一份样品。",
        original_language="zh",
        confidence=1.0,
        evidence_refs=("evidence-001",),
        status="proposed",
        recorded_at=NOW,
    )
    entity = EntityResolutionProposal(
        proposal_id="entity-proposal-001",
        site_id="site-a",
        processing_purpose="observation_processing",
        data_classification="Restricted",
        source_lineage=(observation.event_id, "evidence-002"),
        processor_version="deterministic-test-processor-v1",
        rule_version="observer-rules-v1",
        output_version="gate3-proposal-v1",
        observed_identity_ref="contact-observed-001",
        candidate_identity_refs=("contact-candidate-001",),
        confidence=0.5,
        evidence_refs=("evidence-002",),
        status="proposed",
        recorded_at=NOW,
    )
    return ImportResult(observation, evidence, (fact,), (entity,))


def _payloads(
    repository: RecordingContextRepository,
) -> list[tuple[RecordKind, Mapping[str, Any]]]:
    return [(kind, envelope.payload) for _, kind, envelope in repository.calls]


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list | tuple):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def test_publisher_maps_to_valid_gate3_records_and_persists_evidence_first() -> None:
    repository = RecordingContextRepository()
    publisher = _publisher_class()(repository)

    metadata = publisher.publish(_result(), correlation_id="corr-001")

    assert [kind for _, kind, _ in repository.calls] == [
        RecordKind.EVIDENCE,
        RecordKind.EVIDENCE,
        RecordKind.FACT_PROPOSAL,
        RecordKind.ENTITY_RESOLUTION_PROPOSAL,
    ]
    assert isinstance(metadata, tuple)
    assert all(
        isinstance(item, RecordMetadata) and not hasattr(item, "payload") for item in metadata
    )

    validator = ContextContractValidator.repository_default()
    for kind, payload in _payloads(repository):
        validator.validate(kind, dict(payload))

    evidence_ids = [
        str(payload["evidence_record_id"])
        for kind, payload in _payloads(repository)
        if kind is RecordKind.EVIDENCE
    ]
    fact_payload = next(
        payload for kind, payload in _payloads(repository) if kind is RecordKind.FACT_PROPOSAL
    )
    entity_payload = next(
        payload
        for kind, payload in _payloads(repository)
        if kind is RecordKind.ENTITY_RESOLUTION_PROPOSAL
    )
    assert fact_payload["fact"]["evidence_refs"] == [evidence_ids[0]]
    assert entity_payload["evidence_refs"] == [evidence_ids[1]]


def test_publisher_is_deterministic_and_uses_distinct_stable_idempotency_keys() -> None:
    first_repository = RecordingContextRepository()
    second_repository = RecordingContextRepository()

    _publisher_class()(first_repository).publish(_result(), correlation_id="corr-001")
    _publisher_class()(second_repository).publish(_result(), correlation_id="corr-001")

    first = [
        (scope, kind, envelope.idempotency_key, envelope.payload_digest, dict(envelope.payload))
        for scope, kind, envelope in first_repository.calls
    ]
    second = [
        (scope, kind, envelope.idempotency_key, envelope.payload_digest, dict(envelope.payload))
        for scope, kind, envelope in second_repository.calls
    ]
    assert first == second
    keys = [envelope.idempotency_key for _, _, envelope in first_repository.calls]
    assert len(keys) == len(set(keys))


def test_publisher_uses_persisted_recorded_time_for_restart_safe_replay() -> None:
    first_repository = RecordingContextRepository()
    second_repository = RecordingContextRepository()
    first = _result()
    later = NOW + timedelta(minutes=5)
    retried = replace(
        first,
        observation=replace(first.observation, ingested_at=later),
        evidence=tuple(replace(item, created_at=later) for item in first.evidence),
        fact_proposals=tuple(replace(item, recorded_at=later) for item in first.fact_proposals),
        entity_resolution_proposals=tuple(
            replace(item, recorded_at=later) for item in first.entity_resolution_proposals
        ),
    )

    _publisher_class()(first_repository).publish(
        first,
        correlation_id="corr-001",
        recorded_at=NOW,
    )
    _publisher_class()(second_repository).publish(
        retried,
        correlation_id="corr-001",
        recorded_at=NOW,
    )

    assert [
        (kind, envelope.payload_digest, dict(envelope.payload))
        for _, kind, envelope in first_repository.calls
    ] == [
        (kind, envelope.payload_digest, dict(envelope.payload))
        for _, kind, envelope in second_repository.calls
    ]
    for _kind, payload in _payloads(first_repository):
        lineage = cast(Mapping[str, Any], payload["source_lineage"])
        assert lineage["evidence_status"] == "synthetic"


def test_publisher_never_emits_gate4_objects() -> None:
    repository = RecordingContextRepository()

    _publisher_class()(repository).publish(_result(), correlation_id="corr-001")

    forbidden = {
        "verified_fact",
        "verified_facts",
        "conflict",
        "conflicts",
        "decision",
        "decisions",
        "action",
        "actions",
        "draft",
        "draft_mutation",
        "approved_command",
        "command",
        "review_case",
        "agent_task",
    }
    assert not forbidden.intersection(
        set().union(*(_all_keys(payload) for _, payload in _payloads(repository)))
    )


@pytest.mark.parametrize(
    ("evidence_site", "fact_purpose", "message"),
    [
        ("site-b", "observation_processing", "site"),
        ("site-a", "audit_compliance", "purpose"),
    ],
)
def test_publisher_rejects_cross_scope_imports_before_persisting(
    evidence_site: str,
    fact_purpose: str,
    message: str,
) -> None:
    repository = RecordingContextRepository()

    with pytest.raises(ValidationError, match=message):
        _publisher_class()(repository).publish(
            _result(evidence_site=evidence_site, fact_purpose=fact_purpose),
            correlation_id="corr-001",
        )

    assert repository.calls == []


def test_publisher_rejects_mismatched_correlation_before_persisting() -> None:
    repository = RecordingContextRepository()

    with pytest.raises(ValidationError, match="correlation"):
        _publisher_class()(repository).publish(_result(), correlation_id="corr-other")

    assert repository.calls == []
