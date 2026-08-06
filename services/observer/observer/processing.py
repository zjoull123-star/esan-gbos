from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .models import (
    EntityResolutionProposal,
    FactProposal,
    Participant,
    ProcessingResult,
    TenantScope,
    stable_ulid,
)

_CJK = re.compile(r"[\u3400-\u9fff]")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class CapabilityCounters:
    network_calls: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    kingdee_calls: int = 0
    external_sends: int = 0


class DisabledReviewCaseBridge:
    def __init__(self) -> None:
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def create_review_case(self, *_args: object, **_kwargs: object) -> None:
        self._call_count += 1
        raise RuntimeError("review case creation is disabled at Gate 3")


class DeterministicProcessor:
    """Tool-free fixture processor; input is always treated as untrusted text."""

    PROCESSOR_VERSION = "deterministic-test-processor-v1"
    RULE_VERSION = "observer-rules-v1"
    OUTPUT_VERSION = "gate3-proposal-v1"

    def __init__(self) -> None:
        self.counters = CapabilityCounters()
        self.process_calls = 0

    @staticmethod
    def detect_language(text: str) -> str:
        return "zh" if _CJK.search(text) else "en"

    @staticmethod
    def chinese_summary(text: str) -> str:
        normalized = _SPACE.sub(" ", text).strip()
        if len(normalized) > 160:
            normalized = normalized[:157] + "..."
        if _CJK.search(normalized):
            return f"确定性摘要：{normalized}"
        return f"确定性摘要（保留原文）：{normalized}"

    def process(
        self,
        *,
        scope: TenantScope,
        evidence_id: str,
        text: str,
        participants: tuple[Participant, ...],
        data_classification: str,
        source_lineage: tuple[str, ...],
        recorded_at: datetime,
    ) -> ProcessingResult:
        self.process_calls += 1
        language = self.detect_language(text)
        summary = self.chinese_summary(text)
        subject_ref = participants[0].identity_ref if participants else "party:unknown"
        fact = FactProposal(
            fact_id=stable_ulid(
                "fact-proposal",
                scope.site_id,
                scope.processing_purpose,
                evidence_id,
                summary,
            ),
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            data_classification=data_classification,
            source_lineage=source_lineage,
            processor_version=self.PROCESSOR_VERSION,
            rule_version=self.RULE_VERSION,
            output_version=self.OUTPUT_VERSION,
            subject_ref=subject_ref,
            predicate="communication_summary",
            value=summary,
            summary_zh=summary,
            original_language=language,
            confidence=1.0,
            evidence_refs=(evidence_id,),
            status="proposed",
            recorded_at=recorded_at,
        )
        entity_proposals = tuple(
            EntityResolutionProposal(
                proposal_id=stable_ulid(
                    "entity-resolution-proposal",
                    scope.site_id,
                    scope.processing_purpose,
                    evidence_id,
                    participant.identity_ref,
                ),
                site_id=scope.site_id,
                processing_purpose=scope.processing_purpose,
                data_classification=data_classification,
                source_lineage=source_lineage,
                processor_version=self.PROCESSOR_VERSION,
                rule_version=self.RULE_VERSION,
                output_version=self.OUTPUT_VERSION,
                observed_identity_ref=participant.identity_ref,
                candidate_identity_refs=(participant.identity_ref,),
                confidence=0.5,
                evidence_refs=(evidence_id,),
                status="proposed",
                recorded_at=recorded_at,
            )
            for participant in participants
            if participant.role in {"external", "unknown"}
        )
        return ProcessingResult(
            fact_proposals=(fact,),
            entity_resolution_proposals=entity_proposals,
        )
