from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from services.model_gateway.deepseek import (
    DEEPSEEK_MODEL,
    GatewayFailure,
    TokenizedModelRequest,
)
from services.model_gateway.observation_provider import DeepSeekObservationProvider
from services.observer.observer.model_projection import (
    CommunicationIntelligenceResponse,
    ObservationModelRequest,
)

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)


def communication_output() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": "gbos.localhost",
        "observation_id": "observation-SYNTH-001",
        "evidence_refs": ["evidence-SYNTH-001"],
        "summary_zh": "客户希望确认样品交期。",
        "original_language": "zh-CN",
        "confidence": 0.9,
        "review_status": "AI Draft",
        "fact_proposals": [
            {
                "subject_ref": "party-SYNTH-001",
                "predicate": "sample_delivery_intent",
                "value_display": "希望确认样品交期",
                "type": "text",
                "unit": None,
                "confidence": 0.9,
                "evidence_refs": ["evidence-SYNTH-001"],
                "status": "proposed",
            }
        ],
        "association_suggestions": [],
    }


@dataclass(frozen=True, slots=True, repr=False)
class _ObservationRequest:
    site_id: str = "gbos.localhost"
    observation_id: str = "observation-SYNTH-001"
    channel: str = "email"
    classification: str = "Restricted"
    occurred_at: datetime = NOW
    input_mode: Literal["raw", "local_tokenized"] = "local_tokenized"
    input_text: str = field(
        default="客户 <ENTITY_0123456789abcdef01234567> 希望确认样品交期。",
        repr=False,
    )
    evidence_refs: tuple[str, ...] = ("evidence-SYNTH-001",)
    participant_refs: tuple[str, ...] = ("party-SYNTH-001",)
    tokenization_refs: tuple[str, ...] = ("tokenization-SYNTH-001",)
    tokenizer_version: str | None = "stable-hmac-tokenizer-v1"
    mapping_digest: str | None = "d" * 64
    processing_purpose: str = "observation_processing"
    idempotency_key: str = "context-normalized:SYNTH-001"
    output_schema_version: str = "1.0"

    def __repr__(self) -> str:
        return "_ObservationRequest(identity=<redacted>, input_text=<redacted>)"


class _Gateway:
    provider_version = "deepseek-test-v1"
    tool_version = "no-tools-v1"

    def __init__(
        self,
        *,
        output: dict[str, Any] | None = None,
        observed_model: str = DEEPSEEK_MODEL,
        invocation_ids: tuple[str, ...] = ("invocation-SYNTH-001",),
    ) -> None:
        self.requests: list[TokenizedModelRequest] = []
        self.output = communication_output() if output is None else output
        self.observed_model = observed_model
        self.invocation_ids = invocation_ids

    def invoke(self, request: TokenizedModelRequest) -> object:
        self.requests.append(request)
        return SimpleNamespace(
            output=self.output,
            observed_model=self.observed_model,
            invocations=tuple(
                SimpleNamespace(invocation_id=value) for value in self.invocation_ids
            ),
        )


def test_provider_converts_only_local_tokenized_input_and_wraps_exact_identity() -> None:
    gateway = _Gateway()
    provider = DeepSeekObservationProvider(gateway=gateway)

    response = provider.project(_ObservationRequest())

    assert isinstance(response, CommunicationIntelligenceResponse)
    assert response.output == communication_output()
    assert response.model_name == DEEPSEEK_MODEL
    assert response.model_version == DEEPSEEK_MODEL
    assert response.invocation_refs == ("invocation-SYNTH-001",)
    model_request = gateway.requests[0]
    assert model_request.request_id == "context-normalized:SYNTH-001"
    assert model_request.site_id == "gbos.localhost"
    assert model_request.purpose == "observation_processing"
    assert model_request.agent_kind == "communication"
    assert model_request.subject_ref == "observation-SYNTH-001"
    assert model_request.prompt_version == "communication-intelligence-local-pilot-v1"
    assert model_request.tokenization_receipt_id == "tokenization-SYNTH-001"
    assert model_request.tokenizer_version == "stable-hmac-tokenizer-v1"
    assert model_request.mapping_digest == "d" * 64
    assert model_request.complex_multi_entity is False
    rendered = repr(provider)
    assert "客户" not in rendered
    assert "observation-SYNTH-001" not in rendered
    assert "<redacted>" in rendered


def test_provider_accepts_observer_request_contract_without_leaking_content() -> None:
    gateway = _Gateway()
    provider = DeepSeekObservationProvider(gateway=gateway)
    model_request = ObservationModelRequest(
        site_id="gbos.localhost",
        processing_purpose="sales_follow_up",
        observation_id="observation-SYNTH-001",
        channel="email",
        classification="Restricted",
        occurred_at=NOW,
        input_mode="local_tokenized",
        input_text="敏感正文 <ENTITY_0123456789abcdef01234567>",
        evidence_refs=("evidence-SYNTH-001",),
        participant_refs=("party-SYNTH-001",),
        tokenization_refs=("tokenization-SYNTH-001",),
        tokenizer_version="stable-hmac-tokenizer-v1",
        mapping_digest="d" * 64,
        idempotency_key="context-normalized:SYNTH-001",
    )

    response = provider.project(model_request)

    assert type(response) is CommunicationIntelligenceResponse
    assert gateway.requests[0].purpose == "sales_follow_up"
    assert "敏感正文" not in repr(provider)
    assert "observation-SYNTH-001" not in repr(response)


@pytest.mark.parametrize(
    "model_request",
    [
        replace(_ObservationRequest(), input_mode="raw", tokenization_refs=()),
        replace(_ObservationRequest(), tokenization_refs=()),
        replace(_ObservationRequest(), tokenization_refs=("one", "two")),
        replace(_ObservationRequest(), tokenizer_version=None),
        replace(_ObservationRequest(), mapping_digest=None),
        replace(_ObservationRequest(), mapping_digest="not-a-digest"),
        replace(_ObservationRequest(), processing_purpose=""),
        replace(_ObservationRequest(), output_schema_version="2.0"),
    ],
)
def test_provider_rejects_untrusted_or_incomplete_tokenization_before_gateway(
    model_request: _ObservationRequest,
) -> None:
    gateway = _Gateway()

    with pytest.raises(GatewayFailure):
        DeepSeekObservationProvider(gateway=gateway).project(model_request)

    assert gateway.requests == []


def test_provider_validation_failure_never_exposes_input_or_identity() -> None:
    model_request = replace(
        _ObservationRequest(),
        input_mode="raw",
        input_text="DO-NOT-LEAK-CONTENT",
        observation_id="DO-NOT-LEAK-IDENTITY",
        tokenization_refs=(),
        tokenizer_version=None,
        mapping_digest=None,
    )

    with pytest.raises(GatewayFailure) as captured:
        DeepSeekObservationProvider(gateway=_Gateway()).project(model_request)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert "DO-NOT-LEAK-CONTENT" not in rendered
    assert "DO-NOT-LEAK-IDENTITY" not in rendered


@pytest.mark.parametrize(
    "gateway",
    [
        _Gateway(observed_model="deepseek-chat"),
        _Gateway(invocation_ids=()),
        _Gateway(invocation_ids=("invocation-SYNTH-001", "invocation-SYNTH-001")),
    ],
)
def test_provider_rejects_model_or_audit_identity_mismatch(gateway: _Gateway) -> None:
    with pytest.raises(GatewayFailure):
        DeepSeekObservationProvider(gateway=gateway).project(_ObservationRequest())


@pytest.mark.parametrize(
    ("participant_refs", "evidence_refs", "expected_complex"),
    [
        (
            ("party-1", "party-2"),
            ("evidence-1", "evidence-2"),
            False,
        ),
        (
            ("party-1", "party-2", "party-3"),
            ("evidence-SYNTH-001",),
            True,
        ),
        (
            ("party-1",),
            ("evidence-1", "evidence-2", "evidence-3", "evidence-4", "evidence-5"),
            True,
        ),
    ],
)
def test_provider_uses_conservative_complexity_thresholds(
    participant_refs: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    expected_complex: bool,
) -> None:
    gateway = _Gateway()
    request = replace(
        _ObservationRequest(),
        participant_refs=participant_refs,
        evidence_refs=evidence_refs,
    )

    DeepSeekObservationProvider(gateway=gateway).project(request)

    assert gateway.requests[0].complex_multi_entity is expected_complex
