from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .decision_storage import DecisionStorage, FactSnapshot, ProposalSnapshot


class InvalidDecision(ValueError):
    """The requested decision is incomplete or violates a governed invariant."""


class StaleRevision(InvalidDecision):
    """An optimistic version or revision check failed."""


class EvidenceMissing(InvalidDecision):
    """An exact evidence record is absent from the tenant scope."""


class ExplicitSupersessionRequired(InvalidDecision):
    """A current fact cannot be silently replaced."""


class TraceIntegrityError(InvalidDecision):
    """Stored decision lineage cannot be resolved exactly."""


class DecisionKind(StrEnum):
    HUMAN = "human"
    RULE = "rule"


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    site_id: str
    processing_purpose: str
    proposal_ref: str
    expected_proposal_version: str
    expected_proposal_revision: int
    decision_id: str
    verified_fact_id: str
    decision_kind: DecisionKind
    operator: str
    decision_basis: str
    evidence_refs: tuple[str, ...]
    valid_start: datetime
    valid_end: datetime | None
    effective_at: datetime
    correlation_id: str
    rule_version: str | None = None
    supersedes_fact_ref: str | None = None
    supersedes_fact_version: int | None = None
    expected_current_fact_version: int | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    decision: dict[str, Any]
    fact: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    fact_id: str
    fact_version: int


@dataclass(frozen=True, slots=True)
class ConflictRequest:
    site_id: str
    processing_purpose: str
    conflict_id: str
    proposal_ref: str
    expected_proposal_version: str
    expected_proposal_revision: int
    conflicting_fact: ConflictCandidate
    correlation_id: str


def _timestamp(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidDecision(f"{field} must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valid_time(start: datetime, end: datetime | None) -> dict[str, str | None]:
    start_value = _timestamp(start, "valid_time.start")
    end_value = None if end is None else _timestamp(end, "valid_time.end")
    if end is not None and end <= start:
        raise InvalidDecision("valid_time.end must be after valid_time.start")
    return {"start": start_value, "end": end_value}


def _fact_ref(fact: FactSnapshot) -> dict[str, object]:
    return {"fact_id": fact.fact_id, "fact_version": fact.fact_version}


class DecisionService:
    """Deterministic Context confirmation and trace workflow."""

    def __init__(
        self,
        storage: DecisionStorage,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._storage = storage
        self._clock = clock

    def confirm(self, request: ConfirmationRequest) -> ConfirmationResult:
        proposal = self._checked_proposal(
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            proposal_ref=request.proposal_ref,
            expected_version=request.expected_proposal_version,
            expected_revision=request.expected_proposal_revision,
        )
        self._check_confirmation(request, proposal)
        self._check_evidence(request.site_id, request.evidence_refs)
        current = self._storage.get_current_fact(
            request.site_id,
            proposal.subject_ref,
            proposal.predicate,
        )
        self._check_supersession(request, current)
        recorded_time = self._clock()
        if recorded_time < proposal.recorded_time:
            raise InvalidDecision("recorded_time cannot precede the proposal recorded_time")
        valid_time = _valid_time(request.valid_start, request.valid_end)
        fact_version = 1 if current is None else current.fact_version + 1
        fact = self._build_fact(
            request=request,
            proposal=proposal,
            fact_version=fact_version,
            valid_time=valid_time,
            recorded_time=recorded_time,
        )
        decision = self._build_decision(
            request=request,
            current=current,
            fact_version=fact_version,
            valid_time=valid_time,
            recorded_time=recorded_time,
        )
        self._storage.save_confirmation(
            decision=decision,
            fact=fact,
            expected_proposal_version=request.expected_proposal_version,
            expected_proposal_revision=request.expected_proposal_revision,
            expected_current_fact_ref=(None if current is None else current.fact_id),
            expected_current_fact_version=request.expected_current_fact_version,
        )
        return ConfirmationResult(decision=deepcopy(decision), fact=deepcopy(fact))

    def create_conflict(self, request: ConflictRequest) -> dict[str, Any]:
        proposal = self._checked_proposal(
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            proposal_ref=request.proposal_ref,
            expected_version=request.expected_proposal_version,
            expected_revision=request.expected_proposal_revision,
        )
        fact = self._storage.get_fact(
            request.site_id,
            request.conflicting_fact.fact_id,
            request.conflicting_fact.fact_version,
        )
        if fact is None:
            raise InvalidDecision("conflicting fact version does not exist in site scope")
        evidence_refs = tuple(dict.fromkeys((*fact.evidence_refs, *proposal.evidence_refs)))
        self._check_evidence(request.site_id, evidence_refs)
        now = self._clock()
        conflict = {
            "schema_version": "1.0",
            "conflict_id": request.conflict_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "candidates": [
                {
                    "record_kind": "verified_fact",
                    "record_ref": fact.fact_id,
                    "record_version": str(fact.fact_version),
                    "record_revision": fact.fact_version,
                    "evidence_refs": list(fact.evidence_refs),
                },
                {
                    "record_kind": "fact_proposal",
                    "record_ref": proposal.proposal_ref,
                    "record_version": proposal.proposal_version,
                    "record_revision": proposal.proposal_revision,
                    "evidence_refs": list(proposal.evidence_refs),
                },
            ],
            "status": "open",
            "evidence_refs": list(evidence_refs),
            "detected_at": _timestamp(now, "detected_at"),
            "recorded_time": _timestamp(now, "recorded_time"),
            "correlation_id": request.correlation_id,
        }
        self._storage.save_conflict(
            conflict=conflict,
            expected_proposal_version=request.expected_proposal_version,
            expected_proposal_revision=request.expected_proposal_revision,
        )
        return deepcopy(conflict)

    def trace(self, site_id: str, decision_id: str) -> dict[str, Any]:
        decision = self._storage.get_decision(site_id, decision_id)
        if decision is None:
            raise TraceIntegrityError("decision does not exist in site scope")
        decision_revision = int(decision["decision_revision"])
        expected_fact_refs = tuple(
            sorted(
                (
                    role,
                    str(reference["fact_id"]),
                    int(reference["fact_version"]),
                )
                for role, references in (
                    ("input", decision["input_fact_refs"]),
                    ("output", decision["output_fact_refs"]),
                )
                for reference in references
            )
        )
        if (
            self._storage.get_decision_fact_refs(
                site_id,
                decision_id,
                decision_revision,
            )
            != expected_fact_refs
        ):
            raise TraceIntegrityError("decision relational fact lineage disagrees")
        expected_evidence_refs = tuple(sorted(str(value) for value in decision["evidence_refs"]))
        if (
            self._storage.get_decision_evidence_refs(
                site_id,
                decision_id,
                decision_revision,
            )
            != expected_evidence_refs
        ):
            raise TraceIntegrityError("decision relational evidence lineage disagrees")
        proposal = self._storage.get_proposal(site_id, str(decision["proposal_ref"]))
        if proposal is None:
            raise TraceIntegrityError("decision proposal is missing")
        if (
            proposal.proposal_version != decision["proposal_version"]
            or proposal.proposal_revision != decision["proposal_revision"]
        ):
            raise TraceIntegrityError("decision proposal version is not exact")
        facts: list[dict[str, Any]] = []
        for reference in decision["output_fact_refs"]:
            fact = self._storage.get_fact(
                site_id,
                str(reference["fact_id"]),
                int(reference["fact_version"]),
            )
            if fact is None:
                raise TraceIntegrityError("decision fact version is missing")
            if fact.document.get("confirmation_decision_ref") != decision_id:
                raise TraceIntegrityError("fact does not point back to decision")
            if (
                fact.document.get("proposal_ref") != decision["proposal_ref"]
                or fact.document.get("proposal_version") != decision["proposal_version"]
                or fact.document.get("proposal_revision") != decision["proposal_revision"]
            ):
                raise TraceIntegrityError("fact proposal lineage is not exact")
            expected_fact_evidence = tuple(sorted(fact.evidence_refs))
            if (
                self._storage.get_fact_evidence_refs(
                    site_id,
                    fact.fact_id,
                    fact.fact_version,
                )
                != expected_fact_evidence
            ):
                raise TraceIntegrityError("fact relational evidence lineage disagrees")
            facts.append(deepcopy(dict(fact.document)))
        evidence: list[dict[str, Any]] = []
        for evidence_ref in decision["evidence_refs"]:
            record = self._storage.get_evidence(site_id, str(evidence_ref))
            if record is None:
                raise TraceIntegrityError("decision evidence record is missing")
            evidence.append(deepcopy(dict(record.document)))
        return {
            "schema_version": "1.0",
            "site_id": site_id,
            "decision": deepcopy(decision),
            "proposal": {
                "proposal_ref": proposal.proposal_ref,
                "proposal_version": proposal.proposal_version,
                "proposal_revision": proposal.proposal_revision,
                "payload_digest": proposal.payload_digest,
            },
            "facts": facts,
            "evidence": evidence,
        }

    def _checked_proposal(
        self,
        *,
        site_id: str,
        processing_purpose: str,
        proposal_ref: str,
        expected_version: str,
        expected_revision: int,
    ) -> ProposalSnapshot:
        proposal = self._storage.get_proposal(site_id, proposal_ref)
        if proposal is None:
            raise InvalidDecision("proposal does not exist in site scope")
        if proposal.processing_purpose != processing_purpose:
            raise InvalidDecision("processing purpose does not match proposal")
        if (
            proposal.proposal_version != expected_version
            or proposal.proposal_revision != expected_revision
        ):
            raise StaleRevision("proposal version or revision is stale")
        return proposal

    def _check_confirmation(
        self,
        request: ConfirmationRequest,
        proposal: ProposalSnapshot,
    ) -> None:
        if not request.operator or not request.decision_basis:
            raise InvalidDecision("operator and decision_basis are required")
        if request.decision_kind is DecisionKind.RULE and not request.rule_version:
            raise InvalidDecision("rule confirmation requires rule_version")
        requested = set(request.evidence_refs)
        proposed = set(proposal.evidence_refs)
        if requested != proposed:
            raise InvalidDecision("evidence_refs must exactly match the proposal evidence")
        if not request.evidence_refs:
            raise InvalidDecision("confirmation requires evidence")
        if request.valid_start != proposal.valid_start or request.valid_end != proposal.valid_end:
            raise InvalidDecision("valid_time must exactly match the proposal")
        _timestamp(request.effective_at, "effective_at")

    def _check_evidence(self, site_id: str, evidence_refs: tuple[str, ...]) -> None:
        for evidence_ref in evidence_refs:
            if self._storage.get_evidence(site_id, evidence_ref) is None:
                raise EvidenceMissing(f"evidence record is missing: {evidence_ref}")

    @staticmethod
    def _check_supersession(
        request: ConfirmationRequest,
        current: FactSnapshot | None,
    ) -> None:
        if current is None:
            if (
                request.expected_current_fact_version is not None
                or request.supersedes_fact_ref is not None
                or request.supersedes_fact_version is not None
            ):
                raise StaleRevision("no current fact exists for requested supersession")
            return
        if (
            request.supersedes_fact_ref != current.fact_id
            or request.supersedes_fact_version != current.fact_version
        ):
            raise ExplicitSupersessionRequired(
                "current fact requires an explicit exact supersession"
            )
        if request.expected_current_fact_version != current.fact_version:
            raise StaleRevision("current fact version is stale")

    @staticmethod
    def _build_fact(
        *,
        request: ConfirmationRequest,
        proposal: ProposalSnapshot,
        fact_version: int,
        valid_time: dict[str, str | None],
        recorded_time: datetime,
    ) -> dict[str, Any]:
        lineage = deepcopy(dict(proposal.source_lineage))
        source_refs = list(lineage["source_record_refs"])
        if proposal.proposal_ref not in source_refs:
            source_refs.append(proposal.proposal_ref)
        lineage["source_record_refs"] = source_refs
        lineage["transformation_version"] = "gate4-confirm-v1"
        fact: dict[str, Any] = {
            "schema_version": "1.0",
            "fact_id": request.verified_fact_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "proposal_ref": proposal.proposal_ref,
            "proposal_version": proposal.proposal_version,
            "proposal_revision": proposal.proposal_revision,
            "subject_ref": proposal.subject_ref,
            "predicate": proposal.predicate,
            "value": deepcopy(dict(proposal.value)),
            "fact_version": fact_version,
            "status": "confirmed",
            "evidence_refs": list(request.evidence_refs),
            "confirmation_decision_ref": request.decision_id,
            "valid_time": valid_time,
            "recorded_time": _timestamp(recorded_time, "recorded_time"),
            "review_status": (
                "human_reviewed" if request.decision_kind is DecisionKind.HUMAN else "rule_reviewed"
            ),
            "source_lineage": lineage,
            "correlation_id": request.correlation_id,
        }
        if request.supersedes_fact_ref is not None:
            fact["supersedes_fact_ref"] = request.supersedes_fact_ref
            fact["supersedes_fact_version"] = request.supersedes_fact_version
        return fact

    @staticmethod
    def _build_decision(
        *,
        request: ConfirmationRequest,
        current: FactSnapshot | None,
        fact_version: int,
        valid_time: dict[str, str | None],
        recorded_time: datetime,
    ) -> dict[str, Any]:
        decision: dict[str, Any] = {
            "schema_version": "1.0",
            "decision_id": request.decision_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "decision_version": "gate4-decision-v1",
            "decision_revision": 1,
            "decision_type": request.decision_kind.value,
            "proposal_ref": request.proposal_ref,
            "proposal_version": request.expected_proposal_version,
            "proposal_revision": request.expected_proposal_revision,
            "input_fact_refs": [] if current is None else [_fact_ref(current)],
            "output_fact_refs": [
                {
                    "fact_id": request.verified_fact_id,
                    "fact_version": fact_version,
                }
            ],
            "evidence_refs": list(request.evidence_refs),
            "operator": request.operator,
            "decision_basis": request.decision_basis,
            "outcome": "confirmed",
            "effective_at": _timestamp(request.effective_at, "effective_at"),
            "valid_time": valid_time,
            "recorded_time": _timestamp(recorded_time, "recorded_time"),
            "review_status": (
                "human_reviewed" if request.decision_kind is DecisionKind.HUMAN else "rule_reviewed"
            ),
            "correlation_id": request.correlation_id,
        }
        if request.rule_version is not None:
            decision["rule_version"] = request.rule_version
        return decision
