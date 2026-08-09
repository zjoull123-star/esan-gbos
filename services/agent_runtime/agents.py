from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from services.action_guard.models import ActionRequest, EvaluationPhase, GuardDecision, GuardOutcome
from services.action_guard.policy import ActionGuard

from .invocations import ModelInvocationRecord


class AgentExecutionError(RuntimeError):
    """The requested deterministic agent run failed closed."""


class BudgetExceeded(AgentExecutionError):
    """The run exceeded a declared deterministic budget."""


class AgentKind(StrEnum):
    SALES = "sales"
    PURCHASE = "purchase"
    PRODUCT = "product"
    CEO = "ceo"


@dataclass(frozen=True, slots=True)
class FactVersionRef:
    fact_id: str
    fact_version: int

    def __post_init__(self) -> None:
        if self.fact_version < 1:
            raise ValueError("fact_version must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentBudget:
    max_input_chars: int = 12_000
    max_output_chars: int = 4_000
    max_steps: int = 4

    def __post_init__(self) -> None:
        if min(self.max_input_chars, self.max_output_chars, self.max_steps) < 1:
            raise ValueError("agent budgets must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentInput:
    task_id: str
    site_id: str
    processing_purpose: str
    agent_kind: AgentKind
    requested_by: str
    subject_type: str
    subject_ref: str
    subject_revision: int
    evidence_refs: tuple[str, ...]
    fact_version_refs: tuple[FactVersionRef, ...]
    decision_ref: str
    correlation_id: str
    raw_context: str
    expected_action_type: str
    candidate_refs: tuple[str, ...] = ()
    requested_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.subject_revision < 0:
            raise ValueError("subject_revision must be non-negative")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        if len(self.fact_version_refs) != len(set(self.fact_version_refs)):
            raise ValueError("fact_version_refs must be unique")
        if len(self.requested_tools) != len(set(self.requested_tools)):
            raise ValueError("requested_tools must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderOutput:
    action_type: str
    payload: dict[str, Any]
    confidence: float
    injection_detected: bool
    prompt_version: str
    network_calls: int = 0
    model_api_calls: int = 0
    tool_calls: int = 0
    invocations: tuple[ModelInvocationRecord, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("provider confidence must be a number between zero and one")
        for counter in (self.network_calls, self.model_api_calls, self.tool_calls):
            if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
                raise ValueError("provider counters must be non-negative integers")
        if any(not isinstance(item, ModelInvocationRecord) for item in self.invocations):
            raise ValueError("provider invocations must be audit records")


@runtime_checkable
class ModelProvider(Protocol):
    provider_version: str
    tool_version: str

    def generate(self, request: AgentInput) -> ProviderOutput: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentExecutionResult:
    action_proposal: dict[str, Any] = field(repr=False)
    pre_guard: GuardDecision
    post_guard: GuardDecision
    provider_version: str
    tool_version: str
    prompt_version: str
    policy_version: str
    confidence: float
    injection_detected: bool
    network_calls: int
    model_api_calls: int
    tool_calls: int
    budget: AgentBudget
    invocations: tuple[ModelInvocationRecord, ...] = ()


class DeterministicLocalProvider:
    """A versioned, network-free provider used for Gate 4 verification."""

    provider_version = "deterministic-local-v1"
    tool_version = "no-tools-v1"

    _INJECTION_MARKERS = (
        "ignore previous",
        "system prompt",
        "reveal",
        "send whatsapp",
        "publish a quotation",
        "create a kingdee",
        "mark the deal won",
        "query the database",
        "official revenue forecast",
    )

    def __init__(self) -> None:
        self.execution_count = 0

    def generate(self, request: AgentInput) -> ProviderOutput:
        self.execution_count += 1
        injection_detected = any(
            marker in request.raw_context.casefold() for marker in self._INJECTION_MARKERS
        )
        if request.agent_kind is AgentKind.SALES:
            return ProviderOutput(
                action_type="internal.work_item.propose",
                payload={
                    "title": "客户内部跟进",
                    "summary": "创建内部跟进工作项，由销售人工确认下一步。",
                    "subject_ref": request.subject_ref,
                },
                confidence=0.82,
                injection_detected=injection_detected,
                prompt_version="sales-agent-prompt-v1",
            )
        if request.agent_kind is AgentKind.PURCHASE:
            return ProviderOutput(
                action_type="internal.review_case.propose",
                payload={
                    "title": "候选供应商比较审核",
                    "summary": "比较合成候选信息并提交人工审核，不执行供应商选择。",
                    "candidate_refs": list(request.candidate_refs),
                    "recommendation": "提交人工审核",
                    "subject_ref": request.subject_ref,
                },
                confidence=0.78,
                injection_detected=injection_detected,
                prompt_version="purchase-agent-prompt-v1",
            )
        if request.agent_kind is AgentKind.PRODUCT:
            return ProviderOutput(
                action_type="internal.work_item.propose",
                payload={
                    "title": "样品反馈内部处理",
                    "summary": "创建样品反馈工作项，由产品人员评估下一轮调整。",
                    "subject_ref": request.subject_ref,
                },
                confidence=0.8,
                injection_detected=injection_detected,
                prompt_version="product-agent-prompt-v1",
            )
        if request.agent_kind is AgentKind.CEO:
            return ProviderOutput(
                action_type="internal.ai_draft.propose",
                payload={
                    "title": "经营观察草稿（演示）",
                    "summary": "根据已确认的合成事实生成内部观察草稿，由负责人复核证据。",
                    "synthetic": True,
                    "display_label": "演示数据",
                    "source_mode": "synthetic_agent_context",
                    "is_official_metric": False,
                    "is_official_forecast": False,
                    "requires_human_review": True,
                    "subject_ref": request.subject_ref,
                },
                confidence=0.74,
                injection_detected=injection_detected,
                prompt_version="ceo-agent-prototype-prompt-v1",
            )
        raise AgentExecutionError("unsupported agent kind")


class AgentOrchestrator:
    """Bounded orchestration for the four Gate 4 deterministic agents."""

    _PROFILES = {
        AgentKind.SALES: (
            "sales_follow_up",
            "CRM Deal",
            "internal.work_item.propose",
        ),
        AgentKind.PURCHASE: (
            "procurement_coordination",
            "GBOS Sourcing Event",
            "internal.review_case.propose",
        ),
        AgentKind.PRODUCT: (
            "product_sample_management",
            "GBOS Sample Feedback",
            "internal.work_item.propose",
        ),
        AgentKind.CEO: (
            "metric_reporting",
            "GBOS Synthetic Executive Snapshot",
            "internal.ai_draft.propose",
        ),
    }

    def __init__(
        self,
        *,
        provider: ModelProvider,
        guard: ActionGuard,
        known_evidence_refs: set[str],
        known_fact_refs: set[tuple[str, int]],
        known_subject_refs: set[tuple[str, str]],
    ) -> None:
        self._provider = provider
        self._guard = guard
        self._known_evidence_refs = frozenset(known_evidence_refs)
        self._known_fact_refs = frozenset(known_fact_refs)
        self._known_subject_refs = frozenset(known_subject_refs)

    def execute(
        self,
        request: AgentInput,
        *,
        now: datetime,
        budget: AgentBudget | None = None,
    ) -> AgentExecutionResult:
        active_budget = budget or AgentBudget()
        self._validate_request(request, active_budget)
        guard_request = ActionRequest(
            request_id=request.task_id,
            site_id=request.site_id,
            processing_purpose=request.processing_purpose,
            action_type=request.expected_action_type,
            requested_by=request.requested_by,
            target_ref=request.subject_ref,
            target_revision=request.subject_revision,
            evidence_refs=request.evidence_refs,
            granted_scopes=("gbos-propose",),
            correlation_id=request.correlation_id,
            payload={"subject_type": request.subject_type},
        )
        pre_guard = self._guard.evaluate(guard_request, now=now)
        if pre_guard.outcome is not GuardOutcome.ALLOW:
            raise AgentExecutionError("pre-tool policy denied the agent proposal")

        provider_output = self._provider.generate(request)
        if provider_output.tool_calls:
            raise AgentExecutionError("provider tools are forbidden")
        if provider_output.action_type != request.expected_action_type:
            raise AgentExecutionError("provider attempted an unexpected action type")
        proposal = self._build_proposal(request, provider_output, now)
        if len(_canonical_json(proposal)) > active_budget.max_output_chars:
            raise BudgetExceeded("agent output character budget exceeded")

        post_guard = self._guard.evaluate(
            guard_request,
            phase=EvaluationPhase.POST_RESULT,
            result_payload={
                "site_id": request.site_id,
                "processing_purpose": request.processing_purpose,
                "target_revision": request.subject_revision,
                "evidence_refs": list(request.evidence_refs),
                "proposal": proposal,
            },
            now=now,
        )
        if post_guard.outcome is not GuardOutcome.ALLOW:
            raise AgentExecutionError("post-result policy denied the agent proposal")
        return AgentExecutionResult(
            action_proposal=proposal,
            pre_guard=pre_guard,
            post_guard=post_guard,
            provider_version=self._provider.provider_version,
            tool_version=self._provider.tool_version,
            prompt_version=provider_output.prompt_version,
            policy_version=self._guard.policy_version,
            confidence=provider_output.confidence,
            injection_detected=provider_output.injection_detected,
            network_calls=provider_output.network_calls,
            model_api_calls=provider_output.model_api_calls,
            tool_calls=provider_output.tool_calls,
            budget=active_budget,
            invocations=provider_output.invocations,
        )

    def _validate_request(self, request: AgentInput, budget: AgentBudget) -> None:
        if len(request.raw_context) > budget.max_input_chars:
            raise BudgetExceeded("agent input character budget exceeded")
        if budget.max_steps < 3:
            raise BudgetExceeded("agent step budget is insufficient")
        profile = self._PROFILES.get(request.agent_kind)
        if profile is None:
            raise AgentExecutionError("unknown agent profile")
        expected_purpose, expected_subject_type, expected_action_type = profile
        if (
            request.processing_purpose != expected_purpose
            or request.subject_type != expected_subject_type
            or request.expected_action_type != expected_action_type
        ):
            raise AgentExecutionError("agent profile, purpose, subject, or action mismatch")
        if request.requested_tools:
            raise AgentExecutionError("Gate 4 deterministic agents do not expose tools")
        if not request.evidence_refs or not set(request.evidence_refs).issubset(
            self._known_evidence_refs
        ):
            raise AgentExecutionError("unknown or missing evidence reference")
        if not request.fact_version_refs or not {
            (fact.fact_id, fact.fact_version) for fact in request.fact_version_refs
        }.issubset(self._known_fact_refs):
            raise AgentExecutionError("unknown or missing verified fact version")
        if (request.subject_type, request.subject_ref) not in self._known_subject_refs:
            raise AgentExecutionError("unknown subject reference")
        if request.agent_kind is AgentKind.PURCHASE and len(request.candidate_refs) < 2:
            raise AgentExecutionError("purchase comparison requires at least two candidates")

    def _build_proposal(
        self,
        request: AgentInput,
        provider_output: ProviderOutput,
        now: datetime,
    ) -> dict[str, Any]:
        payload_digest = hashlib.sha256(_canonical_json(provider_output.payload)).hexdigest()
        identity = {
            "task_id": request.task_id,
            "site_id": request.site_id,
            "agent_kind": request.agent_kind.value,
            "subject_type": request.subject_type,
            "subject_ref": request.subject_ref,
            "subject_revision": request.subject_revision,
            "payload_digest": payload_digest,
            "provider_version": self._provider.provider_version,
            "prompt_version": provider_output.prompt_version,
        }
        proposal_id = (
            f"action-proposal-{hashlib.sha256(_canonical_json(identity)).hexdigest()[:24]}"
        )
        return {
            "schema_version": "1.0",
            "action_proposal_id": proposal_id,
            "site_id": request.site_id,
            "processing_purpose": request.processing_purpose,
            "proposal_version": "action-proposal-v1",
            "proposal_revision": 1,
            "action_type": provider_output.action_type,
            "status": "proposed",
            "decision_ref": request.decision_ref,
            "fact_version_refs": [
                {"fact_id": fact.fact_id, "fact_version": fact.fact_version}
                for fact in request.fact_version_refs
            ],
            "evidence_refs": list(request.evidence_refs),
            "requested_by": request.requested_by,
            "target_ref": request.subject_ref,
            "target_revision": request.subject_revision,
            "payload": provider_output.payload,
            "payload_digest": payload_digest,
            "policy_version": self._guard.policy_version,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "correlation_id": request.correlation_id,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
