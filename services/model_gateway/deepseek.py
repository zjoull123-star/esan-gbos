from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from jsonschema import Draft202012Validator

from .tokenization import contains_obvious_pii

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_PATH = "/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_INPUT_TOKENS = 32_768
MAX_OUTPUT_TOKENS = 4_096
LOW_CONFIDENCE_THRESHOLD = 0.65
SOFT_MONTHLY_LIMIT_USD = Decimal("50")
HARD_MONTHLY_LIMIT_USD = Decimal("100")


class GatewayFailure(RuntimeError):
    """The provider failed a closed protocol or validation boundary."""


class ModelNetworkDisabled(GatewayFailure):
    """The default-off model network kill switch blocked an invocation."""


class BudgetHardStop(GatewayFailure):
    """The monthly hard limit blocked an invocation before HTTP."""


class BudgetStatus(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"


class TokenCounter(Protocol):
    version: str

    def count(self, text: str) -> int: ...


class PriceCalculator(Protocol):
    catalog_version: str

    def calculate(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal: ...


class UsageLedger(Protocol):
    def monthly_cost_usd(self) -> Decimal: ...

    def record(self, *, usage: UsageSnapshot, cost: CostSnapshot) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenizedModelRequest:
    request_id: str
    site_id: str
    purpose: str
    agent_kind: Literal["sales", "purchase", "product", "ceo"]
    subject_ref: str
    evidence_refs: tuple[str, ...]
    prompt_version: str
    tokenized_context: str
    tokenization_receipt_id: str
    tokenizer_version: str
    mapping_digest: str
    complex_multi_entity: bool = False

    def __post_init__(self) -> None:
        required = (
            self.request_id,
            self.site_id,
            self.purpose,
            self.subject_ref,
            self.prompt_version,
            self.tokenized_context,
            self.tokenization_receipt_id,
            self.tokenizer_version,
        )
        if not all(required):
            raise ValueError("tokenized model request fields must be non-empty")
        if not self.evidence_refs or len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be non-empty and unique")
        if not re.fullmatch(r"[a-f0-9]{64}", self.mapping_digest):
            raise ValueError("mapping_digest must be a SHA-256 hex digest")
        if contains_obvious_pii(self.tokenized_context):
            raise ValueError("tokenized model request contains residual PII")


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    status: Literal["known", "unknown"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    status: Literal["known", "unknown"]
    amount_usd: Decimal | None = None
    catalog_version: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayResult:
    output: dict[str, Any]
    provider_version: str
    tool_version: str
    observed_model: str
    usage: UsageSnapshot
    cost: CostSnapshot
    budget_status: BudgetStatus
    network_calls: int
    model_api_calls: int
    tool_calls: int


class InMemoryUsageLedger:
    def __init__(self, *, monthly_cost_usd: Decimal = Decimal("0")) -> None:
        if monthly_cost_usd < 0:
            raise ValueError("monthly_cost_usd must be non-negative")
        self._monthly_cost_usd = monthly_cost_usd
        self.records: list[tuple[UsageSnapshot, CostSnapshot]] = []

    def monthly_cost_usd(self) -> Decimal:
        return self._monthly_cost_usd

    def record(self, *, usage: UsageSnapshot, cost: CostSnapshot) -> None:
        self.records.append((usage, cost))
        if cost.amount_usd is not None:
            self._monthly_cost_usd += cost.amount_usd


@dataclass(frozen=True, slots=True)
class _CallResult:
    output: dict[str, Any]
    usage: UsageSnapshot
    observed_model: str
    network_calls: int


class DeepSeekAdapter:
    """Fixed-model, no-tools DeepSeek JSON gateway with local validation."""

    provider_version = "deepseek-chat-adapter-v1"
    tool_version = "no-tools-v1"

    def __init__(
        self,
        *,
        api_key: str,
        transport: httpx.BaseTransport,
        token_counter: TokenCounter,
        price_calculator: PriceCalculator,
        usage_ledger: UsageLedger,
        network_enabled: bool = False,
        retry_delay: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be injected")
        self._api_key = api_key
        self._token_counter = token_counter
        self._price_calculator = price_calculator
        self._usage_ledger = usage_ledger
        self._network_enabled = network_enabled
        self._retry_delay = retry_delay
        self._client = httpx.Client(
            base_url=DEEPSEEK_BASE_URL,
            transport=transport,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DeepSeekAdapter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def invoke(self, request: TokenizedModelRequest) -> GatewayResult:
        if not self._network_enabled:
            raise ModelNetworkDisabled("model network is disabled")
        monthly_before = self._usage_ledger.monthly_cost_usd()
        if monthly_before >= HARD_MONTHLY_LIMIT_USD:
            raise BudgetHardStop("monthly model budget hard stop reached")

        schema = _load_schema(request.agent_kind)
        first = self._call(
            self._client,
            request,
            schema=schema,
            thinking=False,
            prior_output=None,
        )
        calls = [first]
        confidence = first.output.get("confidence")
        needs_review = (
            request.complex_multi_entity
            or not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
            or confidence < LOW_CONFIDENCE_THRESHOLD
        )
        final = first
        if needs_review:
            final = self._call(
                self._client,
                request,
                schema=schema,
                thinking=True,
                prior_output=first.output,
            )
            calls.append(final)

        usage = _combine_usage(tuple(call.usage for call in calls))
        cost = self._calculate_cost(usage)
        self._usage_ledger.record(usage=usage, cost=cost)
        monthly_after = self._usage_ledger.monthly_cost_usd()
        budget_status = (
            BudgetStatus.WARNING
            if monthly_before >= SOFT_MONTHLY_LIMIT_USD or monthly_after >= SOFT_MONTHLY_LIMIT_USD
            else BudgetStatus.NORMAL
        )
        network_calls = sum(call.network_calls for call in calls)
        return GatewayResult(
            output=final.output,
            provider_version=self.provider_version,
            tool_version=self.tool_version,
            observed_model=final.observed_model,
            usage=usage,
            cost=cost,
            budget_status=budget_status,
            network_calls=network_calls,
            model_api_calls=network_calls,
            tool_calls=0,
        )

    def _call(
        self,
        client: httpx.Client,
        request: TokenizedModelRequest,
        *,
        schema: dict[str, Any],
        thinking: bool,
        prior_output: dict[str, Any] | None,
    ) -> _CallResult:
        payload = _request_payload(
            request,
            schema=schema,
            thinking=thinking,
            prior_output=prior_output,
        )
        counted_input = self._token_counter.count(
            json.dumps(payload["messages"], ensure_ascii=False, separators=(",", ":"))
        )
        if counted_input > MAX_INPUT_TOKENS:
            raise GatewayFailure("model input token limit exceeded")
        network_calls = 0
        response: httpx.Response | None = None
        for attempt in range(3):
            network_calls += 1
            try:
                candidate = client.post(DEEPSEEK_CHAT_PATH, json=payload)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise GatewayFailure("model transport failed after retries") from exc
                self._retry_delay(0.1 * (2**attempt))
                continue
            if candidate.status_code == 429 or candidate.status_code >= 500:
                if attempt == 2:
                    raise GatewayFailure("retryable model response exhausted retries")
                self._retry_delay(0.1 * (2**attempt))
                continue
            if not 200 <= candidate.status_code < 300:
                raise GatewayFailure("non-retryable model response")
            response = candidate
            break
        if response is None:
            raise GatewayFailure("model response unavailable")
        output, usage, observed_model = _parse_response(response, request=request, schema=schema)
        return _CallResult(
            output=output,
            usage=usage,
            observed_model=observed_model,
            network_calls=network_calls,
        )

    def _calculate_cost(self, usage: UsageSnapshot) -> CostSnapshot:
        if usage.status == "unknown":
            return CostSnapshot(status="unknown")
        assert usage.input_tokens is not None
        assert usage.output_tokens is not None
        amount = self._price_calculator.calculate(
            model=DEEPSEEK_MODEL,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        if amount < 0:
            raise GatewayFailure("price calculator returned a negative cost")
        return CostSnapshot(
            status="known",
            amount_usd=amount,
            catalog_version=self._price_calculator.catalog_version,
        )


def _request_payload(
    request: TokenizedModelRequest,
    *,
    schema: Mapping[str, Any],
    thinking: bool,
    prior_output: Mapping[str, Any] | None,
) -> dict[str, Any]:
    schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    system = (
        "Return one json object only. It must match this exact JSON schema; arbitrary keys are "
        f"forbidden. JSON structure example and schema: {schema_json}. "
        "Perform only summary and extraction for the internal proposal fields. "
        "Do not call tools, send externally, or create commitments."
    )
    user_content: dict[str, Any] = {
        "request_id": request.request_id,
        "site_id": request.site_id,
        "purpose": request.purpose,
        "agent_kind": request.agent_kind,
        "subject_ref": request.subject_ref,
        "evidence_refs": request.evidence_refs,
        "prompt_version": request.prompt_version,
        "tokenized_context": request.tokenized_context,
        "tokenization": {
            "receipt_id": request.tokenization_receipt_id,
            "tokenizer_version": request.tokenizer_version,
            "mapping_digest": request.mapping_digest,
        },
    }
    if prior_output is not None:
        user_content["review_instruction"] = (
            "Review the prior valid JSON once with high reasoning and return a corrected "
            "final JSON."
        )
        user_content["prior_output"] = prior_output
    payload: dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    user_content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    if thinking:
        payload["reasoning_effort"] = "high"
    return payload


def _parse_response(
    response: httpx.Response,
    *,
    request: TokenizedModelRequest,
    schema: dict[str, Any],
) -> tuple[dict[str, Any], UsageSnapshot, str]:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GatewayFailure("model HTTP response was not valid JSON") from exc
    if not isinstance(body, dict) or body.get("model") != DEEPSEEK_MODEL:
        raise GatewayFailure("observed model mismatch")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise GatewayFailure("model response choices were invalid")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise GatewayFailure("model response was rejected or truncated")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise GatewayFailure("model response message was invalid")
    if "tool_calls" in message or message.get("refusal") is not None:
        raise GatewayFailure("model response attempted tools or refusal")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise GatewayFailure("model response content was empty")
    try:
        output = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GatewayFailure("model content was not valid JSON") from exc
    if not isinstance(output, dict):
        raise GatewayFailure("model content must be a JSON object")
    errors = tuple(Draft202012Validator(schema).iter_errors(output))
    if errors:
        raise GatewayFailure("model content failed the per-agent schema")
    _validate_request_binding(output, request)
    _validate_recursive_safety(output)
    usage = _parse_usage(body.get("usage"))
    return output, usage, DEEPSEEK_MODEL


def _parse_usage(value: object) -> UsageSnapshot:
    if not isinstance(value, dict):
        return UsageSnapshot(status="unknown")
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    total = value.get("total_tokens")
    if not isinstance(prompt, int) or isinstance(prompt, bool) or prompt < 0:
        return UsageSnapshot(status="unknown")
    if not isinstance(completion, int) or isinstance(completion, bool) or completion < 0:
        return UsageSnapshot(status="unknown")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        return UsageSnapshot(status="unknown")
    if total != prompt + completion:
        return UsageSnapshot(status="unknown")
    return UsageSnapshot(
        status="known",
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=total,
    )


def _combine_usage(values: tuple[UsageSnapshot, ...]) -> UsageSnapshot:
    if any(value.status == "unknown" for value in values):
        return UsageSnapshot(status="unknown")
    input_tokens = sum(value.input_tokens or 0 for value in values)
    output_tokens = sum(value.output_tokens or 0 for value in values)
    total_tokens = sum(value.total_tokens or 0 for value in values)
    return UsageSnapshot(
        status="known",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _load_schema(agent_kind: str) -> dict[str, Any]:
    path = (
        Path(__file__).parents[2]
        / "contracts"
        / "local_pilot"
        / f"{agent_kind}-proposal-v1.0.schema.json"
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise GatewayFailure("agent output schema is invalid")
    return schema


def _validate_request_binding(output: Mapping[str, Any], request: TokenizedModelRequest) -> None:
    expected_action = {
        "sales": "internal.work_item.propose",
        "purchase": "internal.review_case.propose",
        "product": "internal.work_item.propose",
        "ceo": "internal.ai_draft.propose",
    }[request.agent_kind]
    if (
        output.get("agent_kind") != request.agent_kind
        or output.get("action_type") != expected_action
        or output.get("subject_ref") != request.subject_ref
        or output.get("evidence_refs") != list(request.evidence_refs)
    ):
        raise GatewayFailure("model content did not bind to the request")


_FORBIDDEN_KEYS = {
    "tool_calls",
    "external_send",
    "formal_price",
    "formal_discount",
    "payment",
    "delivery_commitment",
    "order",
    "won",
    "lost",
    "selected_supplier",
    "supplier_final_selection",
    "official_kpi",
    "draft_mutation",
    "approved_command",
    "execution",
    "execute",
}
_FORBIDDEN_TEXT = (
    "tool_calls",
    "external.message.send",
    "external send",
    "outbound email",
    "formal.quotation.publish",
    "formal price",
    "official price",
    "formal discount",
    "payment.create",
    "request payment",
    "order.create",
    "create order",
    "create the final order",
    "deal.won",
    "deal.lost",
    "deal won",
    "deal lost",
    "mark this deal won",
    "mark this deal lost",
    "promise the delivery",
    "delivery commitment",
    "final supplier selection",
    "selected supplier",
    "official kpi",
    "draftmutation",
    "approvedcommand",
    "execute the write",
    "外发",
    "正式报价",
    "正式价格",
    "正式折扣",
    "折扣",
    "付款",
    "承诺交期",
    "创建订单",
    "赢单",
    "输单",
    "最终供应商",
    "供应商最终选择",
    "正式kpi",
)


def _validate_recursive_safety(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if normalized in _FORBIDDEN_KEYS:
                raise GatewayFailure("model content contained a forbidden result field")
            _validate_recursive_safety(nested)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_recursive_safety(item)
        return
    if isinstance(value, str):
        compact = re.sub(r"[\s_-]+", "", value.casefold())
        if any(
            marker.casefold() in value.casefold()
            or re.sub(r"[\s_-]+", "", marker.casefold()) in compact
            for marker in _FORBIDDEN_TEXT
        ):
            raise GatewayFailure("model content contained forbidden action language")
