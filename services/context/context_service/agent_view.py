"""Refs-only, bounded Context projection for the local Agent runtime."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .decision_storage import DecisionStorage, FactSnapshot
from .models import TenantScope

MAX_FACT_VERSION_REFS = 16
MAX_EVIDENCE_REFS = 32
MAX_CANONICAL_CONTEXT_BYTES = 65_536

_SENSITIVE_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "identity_map",
        "identity_mapping",
        "mapping_reference",
        "message",
        "message_body",
        "message_text",
        "object_ref",
        "raw",
        "raw_body",
        "raw_context",
        "raw_message",
        "source_lineage",
        "token_mapping",
    }
)


class AgentContextError(ValueError):
    """Context could not prove an exact, safe task lineage."""


@dataclass(frozen=True, slots=True)
class AgentFactVersionRef:
    fact_id: str
    fact_version: int

    def __post_init__(self) -> None:
        _require_ref(self.fact_id, "fact_id")
        if (
            not isinstance(self.fact_version, int)
            or isinstance(self.fact_version, bool)
            or self.fact_version < 1
        ):
            raise ValueError("fact_version must be a positive integer")

    def to_wire(self) -> dict[str, str | int]:
        return {"fact_id": self.fact_id, "fact_version": self.fact_version}


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentContextRequest:
    site_id: str
    processing_purpose: str
    subject_type: str
    subject_ref: str
    decision_ref: str
    fact_version_refs: tuple[AgentFactVersionRef, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        TenantScope(self.site_id, self.processing_purpose)
        _require_ref(self.subject_type, "subject_type")
        _require_ref(self.subject_ref, "subject_ref")
        _require_ref(self.decision_ref, "decision_ref")
        _require_unique_bounded(
            tuple((item.fact_id, item.fact_version) for item in self.fact_version_refs),
            "fact_version_refs",
            maximum=MAX_FACT_VERSION_REFS,
        )
        _require_unique_bounded(
            self.evidence_refs,
            "evidence_refs",
            maximum=MAX_EVIDENCE_REFS,
        )
        for value in self.evidence_refs:
            _require_ref(value, "evidence_ref")


@dataclass(frozen=True, slots=True)
class AgentContextBundle:
    request: AgentContextRequest
    facts: tuple[dict[str, Any], ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "site_id": self.request.site_id,
            "processing_purpose": self.request.processing_purpose,
            "subject_type": self.request.subject_type,
            "subject_ref": self.request.subject_ref,
            "decision_ref": self.request.decision_ref,
            "fact_version_refs": [
                reference.to_wire() for reference in self.request.fact_version_refs
            ],
            "evidence_refs": list(self.request.evidence_refs),
            "facts": deepcopy(list(self.facts)),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_wire(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class AgentContextView:
    """Resolve only exact confirmed facts; never expose evidence documents."""

    def __init__(
        self,
        storage: DecisionStorage,
        *,
        max_context_bytes: int = MAX_CANONICAL_CONTEXT_BYTES,
    ) -> None:
        if max_context_bytes < 1:
            raise ValueError("max_context_bytes must be positive")
        self._storage = storage
        self._max_context_bytes = max_context_bytes

    def resolve(self, request: AgentContextRequest) -> AgentContextBundle:
        decision = self._storage.get_decision(request.site_id, request.decision_ref)
        if decision is None:
            raise AgentContextError("decision is missing from site scope")
        self._validate_decision(request, decision)
        proposal = self._storage.get_proposal(
            request.site_id,
            _string(decision, "proposal_ref"),
        )
        if proposal is None:
            raise AgentContextError("decision proposal lineage is missing")
        if (
            proposal.site_id != request.site_id
            or proposal.processing_purpose != request.processing_purpose
            or proposal.subject_ref != request.subject_ref
            or proposal.proposal_version != decision.get("proposal_version")
            or proposal.proposal_revision != decision.get("proposal_revision")
        ):
            raise AgentContextError("decision proposal lineage is not exact")

        facts: list[dict[str, Any]] = []
        seen_evidence: set[str] = set()
        for reference in request.fact_version_refs:
            fact = self._storage.get_fact(
                request.site_id,
                reference.fact_id,
                reference.fact_version,
            )
            if fact is None:
                raise AgentContextError("exact verified fact version is missing")
            facts.append(self._canonical_fact(request, decision, fact))
            seen_evidence.update(fact.evidence_refs)
        if seen_evidence != set(request.evidence_refs):
            raise AgentContextError("fact evidence lineage does not exactly match task refs")

        for evidence_ref in request.evidence_refs:
            evidence = self._storage.get_evidence(request.site_id, evidence_ref)
            if evidence is None:
                raise AgentContextError("evidence lineage is missing from site scope")
            if (
                evidence.site_id != request.site_id
                or evidence.document.get("site_id") != request.site_id
                or evidence.document.get("processing_purpose") != request.processing_purpose
            ):
                raise AgentContextError("evidence scope or purpose is not exact")

        bundle = AgentContextBundle(request=request, facts=tuple(facts))
        if len(bundle.canonical_json().encode("utf-8")) > self._max_context_bytes:
            raise AgentContextError("canonical agent context exceeds byte limit")
        return bundle

    def _validate_decision(
        self,
        request: AgentContextRequest,
        decision: dict[str, Any],
    ) -> None:
        if (
            decision.get("decision_id") != request.decision_ref
            or decision.get("site_id") != request.site_id
        ):
            raise AgentContextError("decision site binding is not exact")
        if decision.get("processing_purpose") != request.processing_purpose:
            raise AgentContextError("decision purpose is not exact")
        decision_revision = _positive_int(decision, "decision_revision")
        requested_fact_refs = tuple(
            sorted(
                (reference.fact_id, reference.fact_version)
                for reference in request.fact_version_refs
            )
        )
        output_refs = _fact_refs(decision.get("output_fact_refs"))
        if output_refs != requested_fact_refs:
            raise AgentContextError("decision fact lineage is not exact")
        decision_evidence = _string_refs(decision.get("evidence_refs"), "decision evidence")
        if tuple(sorted(decision_evidence)) != tuple(sorted(request.evidence_refs)):
            raise AgentContextError("decision evidence lineage is not exact")

        expected_relational = tuple(
            sorted(
                (
                    role,
                    fact_id,
                    fact_version,
                )
                for role, key in (
                    ("input", "input_fact_refs"),
                    ("output", "output_fact_refs"),
                )
                for fact_id, fact_version in _fact_refs(decision.get(key))
            )
        )
        actual_relational = tuple(
            sorted(
                self._storage.get_decision_fact_refs(
                    request.site_id,
                    request.decision_ref,
                    decision_revision,
                )
            )
        )
        if actual_relational != expected_relational:
            raise AgentContextError("decision relational fact lineage is not exact")
        actual_evidence = self._storage.get_decision_evidence_refs(
            request.site_id,
            request.decision_ref,
            decision_revision,
        )
        if tuple(sorted(actual_evidence)) != tuple(sorted(decision_evidence)):
            raise AgentContextError("decision relational evidence lineage is not exact")

    def _canonical_fact(
        self,
        request: AgentContextRequest,
        decision: dict[str, Any],
        fact: FactSnapshot,
    ) -> dict[str, Any]:
        document = fact.document
        if (
            fact.site_id != request.site_id
            or document.get("site_id") != request.site_id
            or document.get("processing_purpose") != request.processing_purpose
            or document.get("subject_ref") != request.subject_ref
        ):
            raise AgentContextError("verified fact site, purpose, or subject is not exact")
        if (
            document.get("status") != "confirmed"
            or document.get("confirmation_decision_ref") != request.decision_ref
        ):
            raise AgentContextError("verified fact decision lineage is not exact")
        if (
            document.get("proposal_ref") != decision.get("proposal_ref")
            or document.get("proposal_version") != decision.get("proposal_version")
            or document.get("proposal_revision") != decision.get("proposal_revision")
        ):
            raise AgentContextError("verified fact proposal lineage is not exact")
        evidence_refs = _string_refs(document.get("evidence_refs"), "fact evidence")
        relational_evidence = self._storage.get_fact_evidence_refs(
            request.site_id,
            fact.fact_id,
            fact.fact_version,
        )
        if tuple(sorted(relational_evidence)) != tuple(sorted(evidence_refs)):
            raise AgentContextError("fact relational evidence lineage is not exact")
        if not set(evidence_refs).issubset(request.evidence_refs):
            raise AgentContextError("fact evidence is outside task refs")

        value = deepcopy(document.get("value"))
        if _contains_sensitive_key(value):
            raise AgentContextError("verified fact value contains a sensitive mapping or raw field")
        predicate = document.get("predicate")
        valid_time = deepcopy(document.get("valid_time"))
        recorded_time = document.get("recorded_time")
        review_status = document.get("review_status")
        if (
            not isinstance(predicate, str)
            or not predicate
            or not isinstance(valid_time, dict)
            or set(valid_time) != {"start", "end"}
            or not isinstance(recorded_time, str)
            or review_status not in {"human_reviewed", "rule_reviewed"}
        ):
            raise AgentContextError("verified fact canonical fields are invalid")
        return {
            "fact_id": fact.fact_id,
            "fact_version": fact.fact_version,
            "predicate": predicate,
            "value": value,
            "valid_time": valid_time,
            "recorded_time": recorded_time,
            "review_status": review_status,
        }


def _require_ref(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be non-empty and at most 256 characters")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} contains forbidden control characters")


def _require_unique_bounded(values: tuple[Any, ...], name: str, *, maximum: int) -> None:
    if not values:
        raise ValueError(f"{name} must be non-empty")
    if len(values) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} entries")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if normalized in _SENSITIVE_KEYS or _contains_sensitive_key(nested):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise AgentContextError(f"decision {key} is invalid")
    return value


def _positive_int(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentContextError(f"decision {key} is invalid")
    return value


def _fact_refs(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise AgentContextError("decision fact refs are invalid")
    parsed: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"fact_id", "fact_version"}:
            raise AgentContextError("decision fact refs are not closed")
        fact_id = item.get("fact_id")
        fact_version = item.get("fact_version")
        if (
            not isinstance(fact_id, str)
            or not fact_id
            or not isinstance(fact_version, int)
            or isinstance(fact_version, bool)
            or fact_version < 1
        ):
            raise AgentContextError("decision fact ref is invalid")
        parsed.append((fact_id, fact_version))
    if len(parsed) != len(set(parsed)):
        raise AgentContextError("decision fact refs must be unique")
    return tuple(sorted(parsed))


def _string_refs(value: object, name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise AgentContextError(f"{name} refs are invalid")
    parsed = tuple(value)
    if len(parsed) != len(set(parsed)):
        raise AgentContextError(f"{name} refs must be unique")
    return parsed


__all__ = [
    "AgentContextBundle",
    "AgentContextError",
    "AgentContextRequest",
    "AgentContextView",
    "AgentFactVersionRef",
    "MAX_CANONICAL_CONTEXT_BYTES",
    "MAX_EVIDENCE_REFS",
    "MAX_FACT_VERSION_REFS",
]
