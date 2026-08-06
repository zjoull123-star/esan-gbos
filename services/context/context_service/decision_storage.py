from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol, cast


def _immutable_mapping(value: dict[str, Any]) -> MappingProxyType[str, Any]:
    return MappingProxyType(deepcopy(value))


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


@dataclass(frozen=True, slots=True)
class ProposalSnapshot:
    """Immutable Gate 3 proposal state consumed by Gate 4."""

    site_id: str
    processing_purpose: str
    proposal_ref: str
    proposal_version: str
    proposal_revision: int
    subject_ref: str
    predicate: str
    value: dict[str, Any]
    evidence_refs: tuple[str, ...]
    valid_start: datetime
    valid_end: datetime | None
    recorded_time: datetime
    source_lineage: dict[str, Any]
    payload_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _immutable_mapping(self.value))
        object.__setattr__(self, "source_lineage", _immutable_mapping(self.source_lineage))


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    site_id: str
    evidence_record_id: str
    document: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _immutable_mapping(self.document))


@dataclass(frozen=True, slots=True)
class FactSnapshot:
    site_id: str
    fact_id: str
    fact_version: int
    document: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _immutable_mapping(self.document))

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> FactSnapshot:
        site_id = document.get("site_id")
        fact_id = document.get("fact_id")
        fact_version = document.get("fact_version")
        if not isinstance(site_id, str) or not site_id:
            raise ValueError("fact site_id is required")
        if not isinstance(fact_id, str) or not fact_id:
            raise ValueError("fact_id is required")
        if not isinstance(fact_version, int) or isinstance(fact_version, bool):
            raise ValueError("fact_version must be an integer")
        return cls(
            site_id=site_id,
            fact_id=fact_id,
            fact_version=fact_version,
            document=document,
        )

    @property
    def subject_ref(self) -> str:
        return cast(str, self.document["subject_ref"])

    @property
    def predicate(self) -> str:
        return cast(str, self.document["predicate"])

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(cast(list[str], self.document["evidence_refs"]))

    @property
    def recorded_time(self) -> datetime:
        return _parse_timestamp(self.document["recorded_time"], "recorded_time")


class DecisionStorage(Protocol):
    """Atomic persistence boundary for the local decision workflow."""

    def get_proposal(self, site_id: str, proposal_ref: str) -> ProposalSnapshot | None: ...

    def get_evidence(self, site_id: str, evidence_ref: str) -> EvidenceSnapshot | None: ...

    def get_fact(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> FactSnapshot | None: ...

    def get_current_fact(
        self,
        site_id: str,
        subject_ref: str,
        predicate: str,
    ) -> FactSnapshot | None: ...

    def save_conflict(
        self,
        *,
        conflict: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
    ) -> None: ...

    def save_confirmation(
        self,
        *,
        decision: dict[str, Any],
        fact: dict[str, Any],
        expected_proposal_version: str,
        expected_proposal_revision: int,
        expected_current_fact_ref: str | None,
        expected_current_fact_version: int | None,
    ) -> None: ...

    def get_decision(self, site_id: str, decision_id: str) -> dict[str, Any] | None: ...

    def get_decision_fact_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[tuple[str, str, int], ...]: ...

    def get_decision_evidence_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[str, ...]: ...

    def get_fact_evidence_refs(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> tuple[str, ...]: ...
