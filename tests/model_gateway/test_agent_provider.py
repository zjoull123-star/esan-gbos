from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from services.agent_runtime.agents import AgentInput, AgentKind, FactVersionRef
from services.model_gateway.deepseek import (
    BudgetStatus,
    CostSnapshot,
    GatewayResult,
    TokenizedModelRequest,
    UsageSnapshot,
)
from services.model_gateway.provider import DeepSeekAgentProvider
from services.model_gateway.tokenization import InMemoryMappingVault, StableTokenizer

NOW = datetime(2026, 8, 7, 3, 0, tzinfo=UTC)


class RecordingGateway:
    provider_version = "deepseek-chat-adapter-v1"
    tool_version = "no-tools-v1"

    def __init__(self) -> None:
        self.requests: list[TokenizedModelRequest] = []

    def invoke(self, request: TokenizedModelRequest) -> GatewayResult:
        self.requests.append(request)
        return GatewayResult(
            output={
                "schema_version": "1.0",
                "proposal_id": "sales-proposal-SYNTH-001",
                "agent_kind": "sales",
                "action_type": "internal.work_item.propose",
                "status": "proposed",
                "subject_ref": "DEAL-SYNTH-001",
                "evidence_refs": ["evidence-SYNTH-001"],
                "confidence": 0.82,
                "requires_human_review": True,
                "payload": {
                    "title": "客户内部跟进",
                    "summary": "整理已观察到的客户需求，等待销售人工复核。",
                    "suggested_next_step": "创建内部跟进工作项。",
                },
            },
            provider_version=self.provider_version,
            tool_version=self.tool_version,
            observed_model="deepseek-v4-flash",
            usage=UsageSnapshot(
                status="known",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            ),
            cost=CostSnapshot(
                status="known",
                amount_usd=Decimal("0.001"),
                catalog_version="test-v1",
            ),
            budget_status=BudgetStatus.NORMAL,
            network_calls=1,
            model_api_calls=1,
            tool_calls=0,
        )


def agent_input() -> AgentInput:
    return AgentInput(
        task_id="task-sales-SYNTH-001",
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        agent_kind=AgentKind.SALES,
        requested_by="sales-agent-SYNTH-001",
        subject_type="CRM Deal",
        subject_ref="DEAL-SYNTH-001",
        subject_revision=1,
        evidence_refs=("evidence-SYNTH-001",),
        fact_version_refs=(FactVersionRef("fact-SYNTH-001", 1),),
        decision_ref="decision-SYNTH-001",
        correlation_id="corr-SYNTH-001",
        raw_context="Email Alice at alice@example.com from Acme Trading.",
        expected_action_type="internal.work_item.propose",
    )


def test_agent_provider_tokenizes_raw_context_before_gateway_and_propagates_counters() -> None:
    gateway = RecordingGateway()
    provider = DeepSeekAgentProvider(
        gateway=gateway,
        tokenizer=StableTokenizer(
            hmac_key=b"k" * 32,
            vault=InMemoryMappingVault(),
        ),
        clock=lambda: NOW,
        phrase_resolver=lambda _: ("Alice", "Acme Trading"),
    )

    output = provider.generate(agent_input())

    assert len(gateway.requests) == 1
    model_request = gateway.requests[0]
    assert "alice@example.com" not in model_request.tokenized_context
    assert "Alice" not in model_request.tokenized_context
    assert model_request.tokenization_receipt_id.startswith("tokenization-")
    assert model_request.tokenizer_version == "stable-hmac-tokenizer-v1"
    assert len(model_request.mapping_digest) == 64
    assert output.action_type == "internal.work_item.propose"
    assert output.payload["suggested_next_step"] == "创建内部跟进工作项。"
    assert output.network_calls == 1
    assert output.model_api_calls == 1
    assert output.tool_calls == 0


def test_agent_provider_requires_explicit_phrase_resolver() -> None:
    with pytest.raises(TypeError, match="phrase_resolver"):
        DeepSeekAgentProvider(  # type: ignore[call-arg]
            gateway=RecordingGateway(),
            tokenizer=StableTokenizer(
                hmac_key=b"k" * 32,
                vault=InMemoryMappingVault(),
            ),
            clock=lambda: NOW,
        )
