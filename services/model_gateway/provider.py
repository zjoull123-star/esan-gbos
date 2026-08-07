from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from services.agent_runtime.agents import AgentExecutionError, AgentInput, AgentKind, ProviderOutput

from .deepseek import GatewayResult, TokenizedModelRequest
from .tokenization import StableTokenizer


class TokenizedGateway(Protocol):
    provider_version: str
    tool_version: str

    def invoke(self, request: TokenizedModelRequest) -> GatewayResult: ...


class DeepSeekAgentProvider:
    """Agent provider that tokenizes locally before entering the model gateway."""

    _PROMPT_VERSIONS = {
        AgentKind.SALES: "sales-local-pilot-v1",
        AgentKind.PURCHASE: "purchase-local-pilot-v1",
        AgentKind.PRODUCT: "product-local-pilot-v1",
        AgentKind.CEO: "ceo-local-pilot-v1",
    }
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

    def __init__(
        self,
        *,
        gateway: TokenizedGateway,
        tokenizer: StableTokenizer,
        clock: Callable[[], datetime],
        phrase_resolver: Callable[[AgentInput], tuple[str, ...]] = lambda _: (),
    ) -> None:
        self._gateway = gateway
        self._tokenizer = tokenizer
        self._clock = clock
        self._phrase_resolver = phrase_resolver

    @property
    def provider_version(self) -> str:
        return self._gateway.provider_version

    @property
    def tool_version(self) -> str:
        return self._gateway.tool_version

    def generate(self, request: AgentInput) -> ProviderOutput:
        prompt_version = self._PROMPT_VERSIONS.get(request.agent_kind)
        if prompt_version is None:
            raise AgentExecutionError("unsupported agent kind")
        tokenized = self._tokenizer.tokenize(
            request.raw_context,
            site_id=request.site_id,
            purpose=request.processing_purpose,
            phrases=self._phrase_resolver(request),
            now=self._clock(),
        )
        model_request = TokenizedModelRequest(
            request_id=request.task_id,
            site_id=request.site_id,
            purpose=request.processing_purpose,
            agent_kind=request.agent_kind.value,
            subject_ref=request.subject_ref,
            evidence_refs=request.evidence_refs,
            prompt_version=prompt_version,
            tokenized_context=tokenized.text,
            tokenization_receipt_id=tokenized.receipt.receipt_id,
            tokenizer_version=tokenized.receipt.tokenizer_version,
            mapping_digest=tokenized.receipt.mapping_digest,
            complex_multi_entity=len(request.candidate_refs) > 2,
        )
        result = self._gateway.invoke(model_request)
        action_type = result.output.get("action_type")
        payload = result.output.get("payload")
        confidence = result.output.get("confidence")
        if (
            not isinstance(action_type, str)
            or not isinstance(payload, dict)
            or not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
        ):
            raise AgentExecutionError("validated model result had an invalid provider shape")
        injection_detected = any(
            marker in request.raw_context.casefold() for marker in self._INJECTION_MARKERS
        )
        return ProviderOutput(
            action_type=action_type,
            payload=payload,
            confidence=float(confidence),
            injection_detected=injection_detected,
            prompt_version=prompt_version,
            network_calls=result.network_calls,
            model_api_calls=result.model_api_calls,
            tool_calls=result.tool_calls,
        )
