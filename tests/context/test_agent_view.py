from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from services.context.context_service.agent_view import (
    AgentContextError,
    AgentContextRequest,
    AgentContextView,
    AgentFactVersionRef,
)
from services.context.context_service.decision_storage import (
    EvidenceSnapshot,
    FactSnapshot,
    ProposalSnapshot,
)


class _Storage:
    def __init__(self) -> None:
        self.fact = FactSnapshot.from_document(
            {
                "schema_version": "1.0",
                "fact_id": "verified-fact-1",
                "fact_version": 2,
                "site_id": "gbos.localhost",
                "processing_purpose": "sales_follow_up",
                "subject_ref": "contact-1",
                "predicate": "requested_quantity",
                "value": {"type": "number", "number": 1000, "unit": "pcs"},
                "status": "confirmed",
                "evidence_refs": ["evidence-1"],
                "confirmation_decision_ref": "decision-1",
                "proposal_ref": "proposal-1",
                "proposal_version": "proposal-v1",
                "proposal_revision": 3,
                "valid_time": {"start": "2026-08-08T01:00:00Z", "end": None},
                "recorded_time": "2026-08-08T02:00:00Z",
                "review_status": "human_reviewed",
                "source_lineage": {
                    "source_record_refs": ["raw-message-1"],
                    "object_ref": "cos://private/raw-message-1",
                },
            }
        )
        self.decision = {
            "schema_version": "1.0",
            "decision_id": "decision-1",
            "decision_revision": 1,
            "site_id": "gbos.localhost",
            "processing_purpose": "sales_follow_up",
            "proposal_ref": "proposal-1",
            "proposal_version": "proposal-v1",
            "proposal_revision": 3,
            "input_fact_refs": [],
            "output_fact_refs": [{"fact_id": "verified-fact-1", "fact_version": 2}],
            "evidence_refs": ["evidence-1"],
        }
        self.proposal = ProposalSnapshot(
            site_id="gbos.localhost",
            processing_purpose="sales_follow_up",
            proposal_ref="proposal-1",
            proposal_version="proposal-v1",
            proposal_revision=3,
            subject_ref="contact-1",
            predicate="requested_quantity",
            value={"type": "number", "number": 1000, "unit": "pcs"},
            evidence_refs=("evidence-1",),
            valid_start=datetime(2026, 8, 8, 1, tzinfo=UTC),
            valid_end=None,
            recorded_time=datetime(2026, 8, 8, 1, 30, tzinfo=UTC),
            source_lineage={"source_record_refs": ["raw-message-1"]},
            payload_digest="a" * 64,
        )
        self.evidence = EvidenceSnapshot(
            site_id="gbos.localhost",
            evidence_record_id="evidence-1",
            document={
                "site_id": "gbos.localhost",
                "processing_purpose": "sales_follow_up",
                "message_body": "must never leave Context",
                "attachments": [{"object_ref": "cos://private/attachment"}],
            },
        )

    def get_fact(self, site_id: str, fact_id: str, fact_version: int) -> FactSnapshot | None:
        if (site_id, fact_id, fact_version) == ("gbos.localhost", "verified-fact-1", 2):
            return self.fact
        return None

    def get_decision(self, site_id: str, decision_id: str) -> dict[str, Any] | None:
        if (site_id, decision_id) == ("gbos.localhost", "decision-1"):
            return deepcopy(self.decision)
        return None

    def get_proposal(self, site_id: str, proposal_ref: str) -> ProposalSnapshot | None:
        if (site_id, proposal_ref) == ("gbos.localhost", "proposal-1"):
            return self.proposal
        return None

    def get_evidence(self, site_id: str, evidence_ref: str) -> EvidenceSnapshot | None:
        if (site_id, evidence_ref) == ("gbos.localhost", "evidence-1"):
            return self.evidence
        return None

    def get_decision_fact_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[tuple[str, str, int], ...]:
        return (("output", "verified-fact-1", 2),)

    def get_decision_evidence_refs(
        self,
        site_id: str,
        decision_id: str,
        decision_revision: int,
    ) -> tuple[str, ...]:
        return ("evidence-1",)

    def get_fact_evidence_refs(
        self,
        site_id: str,
        fact_id: str,
        fact_version: int,
    ) -> tuple[str, ...]:
        return ("evidence-1",)


def _request(**changes: Any) -> AgentContextRequest:
    values: dict[str, Any] = {
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "subject_type": "CRM Contact",
        "subject_ref": "contact-1",
        "decision_ref": "decision-1",
        "fact_version_refs": (AgentFactVersionRef("verified-fact-1", 2),),
        "evidence_refs": ("evidence-1",),
    }
    values.update(changes)
    return AgentContextRequest(**values)


def test_agent_view_returns_only_bounded_canonical_facts_and_exact_refs() -> None:
    bundle = AgentContextView(_Storage()).resolve(_request())
    wire = bundle.to_wire()

    assert wire == {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "processing_purpose": "sales_follow_up",
        "subject_type": "CRM Contact",
        "subject_ref": "contact-1",
        "decision_ref": "decision-1",
        "fact_version_refs": [{"fact_id": "verified-fact-1", "fact_version": 2}],
        "evidence_refs": ["evidence-1"],
        "facts": [
            {
                "fact_id": "verified-fact-1",
                "fact_version": 2,
                "predicate": "requested_quantity",
                "value": {"type": "number", "number": 1000, "unit": "pcs"},
                "valid_time": {"start": "2026-08-08T01:00:00Z", "end": None},
                "recorded_time": "2026-08-08T02:00:00Z",
                "review_status": "human_reviewed",
            }
        ],
    }
    serialized = bundle.canonical_json()
    for forbidden in (
        "message_body",
        "attachments",
        "object_ref",
        "source_lineage",
        "raw-message-1",
        "identity_mapping",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda storage: storage.decision.update(processing_purpose="metric_reporting"), "purpose"),
        (lambda storage: storage.decision.update(output_fact_refs=[]), "fact"),
        (lambda storage: storage.decision.update(evidence_refs=[]), "evidence"),
        (
            lambda storage: storage.fact.document.__class__,
            "",
        ),
    ],
)
def test_agent_view_fails_closed_on_non_exact_decision_lineage(
    mutation: Any,
    message: str,
) -> None:
    storage = _Storage()
    mutation(storage)
    if not message:
        storage.fact = FactSnapshot.from_document(
            {**dict(storage.fact.document), "confirmation_decision_ref": "decision-other"}
        )
        message = "decision"

    with pytest.raises(AgentContextError, match=message):
        AgentContextView(storage).resolve(_request())


def test_agent_view_rejects_sensitive_mapping_inside_verified_value() -> None:
    storage = _Storage()
    storage.fact = FactSnapshot.from_document(
        {
            **dict(storage.fact.document),
            "value": {"identity_mapping": {"person-1": "alice@example.com"}},
        }
    )

    with pytest.raises(AgentContextError, match="sensitive"):
        AgentContextView(storage).resolve(_request())


def test_agent_view_rejects_unbounded_or_duplicate_task_refs() -> None:
    with pytest.raises(ValueError, match="unique"):
        _request(evidence_refs=("evidence-1", "evidence-1"))
    with pytest.raises(ValueError, match="at most"):
        _request(
            fact_version_refs=tuple(
                AgentFactVersionRef(f"verified-fact-{index}", 1) for index in range(17)
            )
        )
