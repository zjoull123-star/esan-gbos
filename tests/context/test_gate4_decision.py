from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.context.context_service.decision import (
    ConfirmationRequest,
    ConflictCandidate,
    ConflictRequest,
    DecisionKind,
    DecisionService,
    EvidenceMissing,
    ExplicitSupersessionRequired,
    InvalidDecision,
    StaleRevision,
    TraceIntegrityError,
)
from services.context.context_service.decision_storage import (
    DecisionStorage,
    EvidenceSnapshot,
    FactSnapshot,
    ProposalSnapshot,
)

NOW = datetime(2026, 8, 6, 3, 5, tzinfo=UTC)
VALID_START = datetime(2026, 8, 6, 1, 59, tzinfo=UTC)


class FakeDecisionStorage(DecisionStorage):
    def __init__(self) -> None:
        self.proposals: dict[tuple[str, str], ProposalSnapshot] = {}
        self.evidence: dict[tuple[str, str], EvidenceSnapshot] = {}
        self.facts: dict[tuple[str, str, int], FactSnapshot] = {}
        self.decisions: dict[tuple[str, str], dict[str, Any]] = {}
        self.conflicts: dict[tuple[str, str], dict[str, Any]] = {}
        self.proposal_documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.decision_fact_refs: dict[tuple[str, str, int], tuple[tuple[str, str, int], ...]] = {}
        self.decision_evidence_refs: dict[tuple[str, str, int], tuple[str, ...]] = {}
        self.fact_evidence_refs: dict[tuple[str, str, int], tuple[str, ...]] = {}

    def get_proposal(self, site_id: str, proposal_ref: str) -> ProposalSnapshot | None:
        return self.proposals.get((site_id, proposal_ref))

    def get_evidence(self, site_id: str, evidence_ref: str) -> EvidenceSnapshot | None:
        return self.evidence.get((site_id, evidence_ref))

    def get_fact(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> FactSnapshot | None:
        return self.facts.get((site_id, fact_id, fact_version))

    def get_current_fact(
        self,
        site_id: str,
        subject_ref: str,
        predicate: str,
    ) -> FactSnapshot | None:
        matching = [
            fact
            for (fact_site, _, _), fact in self.facts.items()
            if fact_site == site_id
            and fact.document["subject_ref"] == subject_ref
            and fact.document["predicate"] == predicate
            and not any(
                other.document.get("supersedes_fact_ref") == fact.fact_id
                and other.document.get("supersedes_fact_version") == fact.fact_version
                for (other_site, _, _), other in self.facts.items()
                if other_site == site_id
            )
        ]
        return max(matching, key=lambda fact: fact.fact_version, default=None)

    def save_conflict(
        self,
        *,
        conflict: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
    ) -> None:
        proposal = self.get_proposal(conflict["site_id"], conflict["candidates"][1]["record_ref"])
        if proposal is None:
            raise StaleRevision("proposal no longer exists")
        if (
            proposal.proposal_version != expected_proposal_version
            or proposal.proposal_revision != expected_proposal_revision
        ):
            raise StaleRevision("proposal changed during conflict creation")
        self.conflicts[(conflict["site_id"], conflict["conflict_id"])] = deepcopy(conflict)

    def save_confirmation(
        self,
        *,
        decision: dict[str, Any],
        fact: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
        expected_current_fact_ref: str | None,
        expected_current_fact_version: int | None,
    ) -> None:
        proposal = self.get_proposal(decision["site_id"], decision["proposal_ref"])
        if proposal is None:
            raise StaleRevision("proposal no longer exists")
        if (
            proposal.proposal_version != expected_proposal_version
            or proposal.proposal_revision != expected_proposal_revision
        ):
            raise StaleRevision("proposal changed during confirmation")
        current = self.get_current_fact(
            decision["site_id"],
            fact["subject_ref"],
            fact["predicate"],
        )
        current_identity = (
            (None, None) if current is None else (current.fact_id, current.fact_version)
        )
        if current_identity != (
            expected_current_fact_ref,
            expected_current_fact_version,
        ):
            raise StaleRevision("current fact changed during confirmation")
        self.decisions[(decision["site_id"], decision["decision_id"])] = deepcopy(decision)
        snapshot = FactSnapshot.from_document(fact)
        self.facts[(snapshot.site_id, snapshot.fact_id, snapshot.fact_version)] = snapshot
        decision_key = (
            decision["site_id"],
            decision["decision_id"],
            decision["decision_revision"],
        )
        self.decision_fact_refs[decision_key] = tuple(
            (
                role,
                reference["fact_id"],
                reference["fact_version"],
            )
            for role, references in (
                ("input", decision["input_fact_refs"]),
                ("output", decision["output_fact_refs"]),
            )
            for reference in references
        )
        self.decision_evidence_refs[decision_key] = tuple(decision["evidence_refs"])
        self.fact_evidence_refs[(snapshot.site_id, snapshot.fact_id, snapshot.fact_version)] = (
            tuple(fact["evidence_refs"])
        )

    def get_decision(self, site_id: str, decision_id: str) -> dict[str, Any] | None:
        decision = self.decisions.get((site_id, decision_id))
        return deepcopy(decision) if decision is not None else None

    def get_decision_fact_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[tuple[str, str, int], ...]:
        return self.decision_fact_refs.get(
            (site_id, decision_id, decision_revision),
            (),
        )

    def get_decision_evidence_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[str, ...]:
        return self.decision_evidence_refs.get(
            (site_id, decision_id, decision_revision),
            (),
        )

    def get_fact_evidence_refs(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> tuple[str, ...]:
        return self.fact_evidence_refs.get((site_id, fact_id, fact_version), ())


def _proposal() -> ProposalSnapshot:
    return ProposalSnapshot(
        site_id="gbos.localhost",
        processing_purpose="business_operations",
        proposal_ref="fact-proposal-SYNTH-001",
        proposal_version="fact-proposal-v1",
        proposal_revision=1,
        subject_ref="contact-SYNTH-001",
        predicate="requested_quantity",
        value={"type": "number", "number": 1000, "unit": "pcs"},
        evidence_refs=("evidence-record-SYNTH-001",),
        valid_start=VALID_START,
        valid_end=None,
        recorded_time=datetime(2026, 8, 6, 2, 0, 4, tzinfo=UTC),
        source_lineage={
            "source_system": "manual_import",
            "source_record_refs": [
                "event-SYNTH-001",
                "evidence-record-SYNTH-001",
            ],
            "retrieved_at": "2026-08-06T02:00:03Z",
            "transformation_version": "fact-proposal-v1",
            "evidence_status": "synthetic",
        },
        payload_digest="b" * 64,
    )


def _evidence() -> EvidenceSnapshot:
    return EvidenceSnapshot(
        site_id="gbos.localhost",
        evidence_record_id="evidence-record-SYNTH-001",
        document={
            "schema_version": "2.0",
            "evidence_record_id": "evidence-record-SYNTH-001",
            "site_id": "gbos.localhost",
            "processing_purpose": "business_operations",
            "evidence_ref": {
                "schema_version": "1.0",
                "evidence_id": "evidence-SYNTH-001",
                "observation_event_id": "01K20B8BV5C6P4YFAT8YQ3D4S5",
                "raw_sha256": "b" * 64,
                "object_ref": "cos://synthetic/evidence-SYNTH-001",
                "media_type": "text/plain",
                "locator": {"message_start": 0, "message_end": 42},
                "created_at": "2026-08-06T01:00:03Z",
            },
            "source_lineage": {
                "source_system": "synthetic_observer",
                "source_record_refs": ["01K20B8BV5C6P4YFAT8YQ3D4S5"],
                "retrieved_at": "2026-08-06T01:00:03Z",
                "transformation_version": "gate2-fixture-v1",
                "evidence_status": "synthetic",
            },
            "data_classification": "Restricted",
            "review_status": "human_reviewed",
            "recorded_at": "2026-08-06T01:00:04Z",
        },
    )


def _storage() -> FakeDecisionStorage:
    storage = FakeDecisionStorage()
    proposal = _proposal()
    evidence = _evidence()
    storage.proposals[(proposal.site_id, proposal.proposal_ref)] = proposal
    storage.evidence[(evidence.site_id, evidence.evidence_record_id)] = evidence
    storage.proposal_documents[(proposal.site_id, proposal.proposal_ref)] = {
        "status": "proposed",
        "proposal_version": proposal.proposal_version,
        "proposal_revision": proposal.proposal_revision,
        "payload_digest": proposal.payload_digest,
    }
    return storage


def _confirmation(**overrides: object) -> ConfirmationRequest:
    values: dict[str, object] = {
        "site_id": "gbos.localhost",
        "processing_purpose": "business_operations",
        "proposal_ref": "fact-proposal-SYNTH-001",
        "expected_proposal_version": "fact-proposal-v1",
        "expected_proposal_revision": 1,
        "decision_id": "decision-SYNTH-001",
        "verified_fact_id": "verified-fact-SYNTH-001",
        "decision_kind": DecisionKind.HUMAN,
        "operator": "reviewer-SYNTH-001",
        "decision_basis": "The exact proposal is supported by retained evidence.",
        "evidence_refs": ("evidence-record-SYNTH-001",),
        "valid_start": VALID_START,
        "valid_end": None,
        "effective_at": NOW,
        "correlation_id": "corr-SYNTH-001",
    }
    values.update(overrides)
    return ConfirmationRequest(**values)  # type: ignore[arg-type]


def _existing_fact() -> FactSnapshot:
    return FactSnapshot.from_document(
        {
            "schema_version": "1.0",
            "fact_id": "verified-fact-SYNTH-000",
            "site_id": "gbos.localhost",
            "processing_purpose": "business_operations",
            "proposal_ref": "fact-proposal-SYNTH-000",
            "proposal_version": "fact-proposal-v1",
            "proposal_revision": 1,
            "subject_ref": "contact-SYNTH-001",
            "predicate": "requested_quantity",
            "value": {"type": "number", "number": 900, "unit": "pcs"},
            "fact_version": 1,
            "status": "confirmed",
            "evidence_refs": ["evidence-record-SYNTH-000"],
            "confirmation_decision_ref": "decision-SYNTH-000",
            "valid_time": {"start": "2026-08-05T01:00:00Z", "end": None},
            "recorded_time": "2026-08-05T02:00:00Z",
            "review_status": "human_reviewed",
            "source_lineage": {
                "source_system": "manual_import",
                "source_record_refs": ["evidence-record-SYNTH-000"],
                "retrieved_at": "2026-08-05T01:00:00Z",
                "transformation_version": "gate4-confirm-v1",
                "evidence_status": "synthetic",
            },
            "correlation_id": "corr-SYNTH-000",
        }
    )


def test_human_confirmation_emits_exact_decision_and_fact_without_mutating_proposal() -> None:
    storage = _storage()
    before = deepcopy(storage.proposal_documents[("gbos.localhost", "fact-proposal-SYNTH-001")])
    service = DecisionService(storage, clock=lambda: NOW)

    result = service.confirm(_confirmation())

    assert result.decision["proposal_version"] == "fact-proposal-v1"
    assert result.decision["proposal_revision"] == 1
    assert result.decision["evidence_refs"] == ["evidence-record-SYNTH-001"]
    assert result.fact["fact_version"] == 1
    assert result.fact["valid_time"] == {"start": "2026-08-06T01:59:00Z", "end": None}
    assert result.fact["recorded_time"] == "2026-08-06T03:05:00Z"
    assert storage.proposal_documents[("gbos.localhost", "fact-proposal-SYNTH-001")] == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_proposal_version", "fact-proposal-v0"),
        ("expected_proposal_revision", 0),
    ],
)
def test_confirmation_rejects_stale_proposal_version_or_revision(
    field: str,
    value: object,
) -> None:
    service = DecisionService(_storage(), clock=lambda: NOW)

    with pytest.raises(StaleRevision):
        service.confirm(_confirmation(**{field: value}))


def test_confirmation_requires_every_exact_evidence_record() -> None:
    storage = _storage()
    service = DecisionService(storage, clock=lambda: NOW)

    storage.evidence.clear()
    with pytest.raises(EvidenceMissing):
        service.confirm(_confirmation())

    with pytest.raises(InvalidDecision, match="exactly match"):
        service.confirm(
            _confirmation(
                evidence_refs=(
                    "evidence-record-SYNTH-001",
                    "evidence-record-EXTRA",
                )
            )
        )


def test_confirmation_rejects_a_subset_of_proposal_evidence() -> None:
    storage = _storage()
    proposal = _proposal()
    storage.proposals[(proposal.site_id, proposal.proposal_ref)] = ProposalSnapshot(
        site_id=proposal.site_id,
        processing_purpose=proposal.processing_purpose,
        proposal_ref=proposal.proposal_ref,
        proposal_version=proposal.proposal_version,
        proposal_revision=proposal.proposal_revision,
        subject_ref=proposal.subject_ref,
        predicate=proposal.predicate,
        value=dict(proposal.value),
        evidence_refs=(
            "evidence-record-SYNTH-001",
            "evidence-record-SYNTH-002",
        ),
        valid_start=proposal.valid_start,
        valid_end=proposal.valid_end,
        recorded_time=proposal.recorded_time,
        source_lineage=dict(proposal.source_lineage),
        payload_digest=proposal.payload_digest,
    )
    storage.evidence[("gbos.localhost", "evidence-record-SYNTH-002")] = EvidenceSnapshot(
        site_id="gbos.localhost",
        evidence_record_id="evidence-record-SYNTH-002",
        document={
            "site_id": "gbos.localhost",
            "evidence_record_id": "evidence-record-SYNTH-002",
        },
    )
    service = DecisionService(storage, clock=lambda: NOW)

    with pytest.raises(InvalidDecision, match="exactly match"):
        service.confirm(_confirmation(evidence_refs=("evidence-record-SYNTH-001",)))


def test_rule_confirmation_requires_rule_version() -> None:
    service = DecisionService(_storage(), clock=lambda: NOW)

    with pytest.raises(InvalidDecision, match="rule_version"):
        service.confirm(_confirmation(decision_kind=DecisionKind.RULE, operator="rule-engine"))

    result = service.confirm(
        _confirmation(
            decision_kind=DecisionKind.RULE,
            operator="rule-engine",
            rule_version="quantity-confirm-v1",
        )
    )
    assert result.decision["rule_version"] == "quantity-confirm-v1"
    assert result.decision["review_status"] == "rule_reviewed"


def test_confirmation_rejects_invalid_valid_time() -> None:
    service = DecisionService(_storage(), clock=lambda: NOW)

    with pytest.raises(InvalidDecision, match="valid_time"):
        service.confirm(
            _confirmation(
                valid_start=datetime(2026, 8, 7, tzinfo=UTC),
                valid_end=datetime(2026, 8, 6, tzinfo=UTC),
            )
        )


def test_existing_fact_requires_explicit_exact_supersession_and_optimistic_version() -> None:
    storage = _storage()
    current = _existing_fact()
    storage.facts[(current.site_id, current.fact_id, current.fact_version)] = current
    service = DecisionService(storage, clock=lambda: NOW)

    with pytest.raises(ExplicitSupersessionRequired):
        service.confirm(_confirmation())

    with pytest.raises(StaleRevision):
        service.confirm(
            _confirmation(
                supersedes_fact_ref=current.fact_id,
                supersedes_fact_version=current.fact_version,
                expected_current_fact_version=2,
            )
        )

    result = service.confirm(
        _confirmation(
            supersedes_fact_ref=current.fact_id,
            supersedes_fact_version=current.fact_version,
            expected_current_fact_version=1,
        )
    )
    assert result.fact["fact_version"] == 2
    assert result.fact["supersedes_fact_ref"] == current.fact_id
    assert result.decision["input_fact_refs"] == [
        {"fact_id": current.fact_id, "fact_version": current.fact_version}
    ]


def test_conflict_creation_retains_both_exact_candidates_and_does_not_mutate_proposal() -> None:
    storage = _storage()
    current = _existing_fact()
    storage.facts[(current.site_id, current.fact_id, current.fact_version)] = current
    storage.evidence[("gbos.localhost", "evidence-record-SYNTH-000")] = EvidenceSnapshot(
        site_id="gbos.localhost",
        evidence_record_id="evidence-record-SYNTH-000",
        document={
            "site_id": "gbos.localhost",
            "evidence_record_id": "evidence-record-SYNTH-000",
        },
    )
    before = storage.proposals[("gbos.localhost", "fact-proposal-SYNTH-001")]
    service = DecisionService(storage, clock=lambda: NOW)

    conflict = service.create_conflict(
        ConflictRequest(
            site_id="gbos.localhost",
            processing_purpose="business_operations",
            conflict_id="conflict-SYNTH-001",
            proposal_ref="fact-proposal-SYNTH-001",
            expected_proposal_version="fact-proposal-v1",
            expected_proposal_revision=1,
            conflicting_fact=ConflictCandidate(
                fact_id=current.fact_id,
                fact_version=current.fact_version,
            ),
            correlation_id="corr-SYNTH-001",
        )
    )

    assert conflict["candidates"][0]["record_ref"] == current.fact_id
    assert conflict["candidates"][1]["record_ref"] == before.proposal_ref
    assert storage.proposals[("gbos.localhost", before.proposal_ref)] == before


def test_decision_trace_returns_exact_fact_and_evidence_versions() -> None:
    storage = _storage()
    service = DecisionService(storage, clock=lambda: NOW)
    result = service.confirm(_confirmation())

    trace = service.trace("gbos.localhost", result.decision["decision_id"])

    assert trace["proposal"]["proposal_revision"] == 1
    assert trace["facts"] == [result.fact]
    assert trace["evidence"][0]["evidence_record_id"] == "evidence-record-SYNTH-001"


def test_decision_trace_fails_closed_when_exact_evidence_is_missing() -> None:
    storage = _storage()
    service = DecisionService(storage, clock=lambda: NOW)
    result = service.confirm(_confirmation())
    storage.evidence.clear()

    with pytest.raises(TraceIntegrityError, match="evidence"):
        service.trace("gbos.localhost", result.decision["decision_id"])


def test_decision_trace_fails_closed_when_relational_lineage_disagrees() -> None:
    storage = _storage()
    service = DecisionService(storage, clock=lambda: NOW)
    result = service.confirm(_confirmation())
    decision_key = (
        "gbos.localhost",
        result.decision["decision_id"],
        result.decision["decision_revision"],
    )
    storage.decision_evidence_refs[decision_key] = ()

    with pytest.raises(TraceIntegrityError, match="relational"):
        service.trace("gbos.localhost", result.decision["decision_id"])


def test_cross_site_confirmation_fails_closed() -> None:
    service = DecisionService(_storage(), clock=lambda: NOW)

    with pytest.raises(InvalidDecision, match="proposal"):
        service.confirm(_confirmation(site_id="other.localhost"))


def test_gate4_migration_enforces_site_composite_keys_rls_and_non_bypass_role() -> None:
    migration = (
        Path(__file__).parents[2] / "services" / "context" / "migrations" / "002_gate4_decision.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(migration.lower().split())

    for table in (
        "conflicts",
        "verified_facts",
        "decisions",
        "decision_fact_refs",
        "decision_evidence_refs",
    ):
        assert f"alter table context.{table} enable row level security" in normalized
        assert f"alter table context.{table} force row level security" in normalized
    assert "primary key (site_id, fact_id, fact_version)" in normalized
    assert "foreign key (site_id, proposal_ref)" in normalized
    assert "references context.fact_proposals (site_id, fact_proposal_record_id)" in normalized
    assert "nobypassrls" in normalized
    assert "grant select, insert on" in normalized
    assert "update context.fact_proposals" not in normalized

    hardening = (
        Path(__file__).parents[2]
        / "services"
        / "context"
        / "migrations"
        / "003_gate4_decision_hardening.sql"
    ).read_text(encoding="utf-8")
    hardening = " ".join(hardening.lower().split())
    assert "proposal_version is not null" in hardening
    assert "btrim(proposal_version) <> ''" in hardening
    assert (
        "on context.verified_facts ( site_id, subject_ref, predicate, fact_version )" in hardening
    )
    revision_migration = (
        Path(__file__).parents[2]
        / "services"
        / "context"
        / "migrations"
        / "004_gate4_decision_revision.sql"
    ).read_text(encoding="utf-8")
    revision_migration = " ".join(revision_migration.lower().split())
    assert "drop constraint if exists decisions_site_id_decision_id_key" in (revision_migration)
    assert "decisions_latest_revision_idx" in revision_migration


@pytest.mark.parametrize(
    "forbidden",
    ["frappe", "kingdee", "draft_mutation", "approved_command", "http://", "https://"],
)
def test_decision_runtime_has_no_execution_or_external_integration_surface(forbidden: str) -> None:
    root = Path(__file__).parents[2] / "services" / "context" / "context_service"
    source = "\n".join(
        (root / filename).read_text(encoding="utf-8").lower()
        for filename in ("decision.py", "decision_storage.py")
    )

    assert forbidden not in source
