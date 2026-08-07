from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from jsonschema import Draft202012Validator

from services.agent_runtime.invocations import (
    BudgetAuditStatus,
    CostMetadata,
    InvocationReferences,
    ModelInvocationRecord,
    TokenUsageMetadata,
)

from .tokenization import contains_obvious_pii

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_CHAT_PATH = "/chat/completions"
DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_INPUT_TOKENS = 32_768
MAX_OUTPUT_TOKENS = 4_096
MAX_NETWORK_ATTEMPTS = 3
LOW_CONFIDENCE_THRESHOLD = 0.75
SOFT_MONTHLY_LIMIT_USD = Decimal("50")
HARD_MONTHLY_LIMIT_USD = Decimal("100")
MODEL_GATEWAY_POLICY_VERSION = "model-gateway-policy-v1"
OUTPUT_SCHEMA_VERSIONS = {
    "sales": "sales-proposal-v1.0",
    "purchase": "purchase-proposal-v1.0",
    "product": "product-proposal-v1.0",
    "ceo": "ceo-proposal-v1.0",
}


class GatewayFailure(RuntimeError):
    """The provider failed a closed protocol or validation boundary."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "internal_error",
        network_calls: int = 0,
        response_id: str | None = None,
        observed_model: str | None = None,
        finish_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.network_calls = network_calls
        self.response_id = response_id
        self.observed_model = observed_model
        self.finish_code = finish_code
        self.cost = CostSnapshot(status="unknown")
        self.audit_records: tuple[ModelInvocationRecord, ...] = ()


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

    def reserve(
        self,
        *,
        reservation_id: str,
        amount_usd: Decimal,
        price_catalog_version: str,
        token_counter_version: str,
    ) -> BudgetReservation: ...

    def ensure_can_attempt(self, reservation: BudgetReservation) -> None: ...

    def settle(
        self,
        reservation: BudgetReservation,
        *,
        usage: UsageSnapshot,
        cost: CostSnapshot,
    ) -> None: ...

    def consume(self, reservation: BudgetReservation) -> CostSnapshot: ...

    def release(self, reservation: BudgetReservation) -> None: ...


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
class BudgetReservation:
    reservation_id: str
    amount_usd: Decimal
    price_catalog_version: str
    token_counter_version: str
    budget_month: date | None = None


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
    invocations: tuple[ModelInvocationRecord, ...] = ()


class InMemoryUsageLedger:
    def __init__(self, *, monthly_cost_usd: Decimal = Decimal("0")) -> None:
        if monthly_cost_usd < 0:
            raise ValueError("monthly_cost_usd must be non-negative")
        self._monthly_cost_usd = monthly_cost_usd
        self.records: list[tuple[UsageSnapshot, CostSnapshot]] = []
        self._reservations: dict[str, BudgetReservation] = {}
        self._settled: dict[str, CostSnapshot] = {}

    def monthly_cost_usd(self) -> Decimal:
        return self._monthly_cost_usd

    def reserve(
        self,
        *,
        reservation_id: str,
        amount_usd: Decimal,
        price_catalog_version: str,
        token_counter_version: str,
    ) -> BudgetReservation:
        if amount_usd < 0 or not amount_usd.is_finite():
            raise GatewayFailure("budget reservation is invalid", error_code="pricing_error")
        existing = self._reservations.get(reservation_id)
        if existing is not None:
            if (
                existing.amount_usd != amount_usd
                or existing.price_catalog_version != price_catalog_version
                or existing.token_counter_version != token_counter_version
            ):
                raise GatewayFailure(
                    "budget reservation metadata conflict",
                    error_code="internal_error",
                )
            return existing
        if reservation_id in self._settled:
            raise GatewayFailure(
                "budget reservation was already settled",
                error_code="internal_error",
            )
        reserved = sum(
            (item.amount_usd for item in self._reservations.values()),
            Decimal("0"),
        )
        if (
            self._monthly_cost_usd >= HARD_MONTHLY_LIMIT_USD
            or self._monthly_cost_usd + reserved + amount_usd > HARD_MONTHLY_LIMIT_USD
        ):
            raise BudgetHardStop(
                "monthly model budget cannot cover the worst-case call cost",
                error_code="budget_hard_stop",
            )
        reservation = BudgetReservation(
            reservation_id=reservation_id,
            amount_usd=amount_usd,
            price_catalog_version=price_catalog_version,
            token_counter_version=token_counter_version,
        )
        self._reservations[reservation_id] = reservation
        return reservation

    def ensure_can_attempt(self, reservation: BudgetReservation) -> None:
        if self._reservations.get(reservation.reservation_id) != reservation:
            raise BudgetHardStop(
                "model budget reservation is unavailable",
                error_code="budget_hard_stop",
            )

    def settle(
        self,
        reservation: BudgetReservation,
        *,
        usage: UsageSnapshot,
        cost: CostSnapshot,
    ) -> None:
        self.ensure_can_attempt(reservation)
        if (
            cost.status != "known"
            or cost.amount_usd is None
            or cost.catalog_version != reservation.price_catalog_version
            or cost.amount_usd > reservation.amount_usd
        ):
            raise GatewayFailure(
                "settled model cost is unknown or exceeds its reservation",
                error_code="pricing_error",
            )
        self._reservations.pop(reservation.reservation_id)
        self._settled[reservation.reservation_id] = cost
        self.records.append((usage, cost))
        self._monthly_cost_usd += cost.amount_usd

    def consume(self, reservation: BudgetReservation) -> CostSnapshot:
        self.ensure_can_attempt(reservation)
        cost = CostSnapshot(
            status="known",
            amount_usd=reservation.amount_usd,
            catalog_version=reservation.price_catalog_version,
        )
        self._reservations.pop(reservation.reservation_id)
        self._settled[reservation.reservation_id] = cost
        self.records.append((UsageSnapshot(status="unknown"), cost))
        self._monthly_cost_usd += reservation.amount_usd
        return cost

    def release(self, reservation: BudgetReservation) -> None:
        if self._reservations.get(reservation.reservation_id) == reservation:
            self._reservations.pop(reservation.reservation_id)

    def record(self, *, usage: UsageSnapshot, cost: CostSnapshot) -> None:
        """Compatibility helper for deterministic tests outside the adapter."""
        self.records.append((usage, cost))
        if cost.amount_usd is not None:
            self._monthly_cost_usd += cost.amount_usd


@dataclass(frozen=True, slots=True)
class _CallResult:
    output: dict[str, Any]
    usage: UsageSnapshot
    observed_model: str
    response_id: str | None
    finish_code: str
    network_calls: int
    cost: CostSnapshot


@dataclass(frozen=True, slots=True)
class _AuditedCall:
    call: _CallResult
    cost: CostSnapshot
    record: ModelInvocationRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        audit_recorder: Callable[[ModelInvocationRecord], object] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be injected")
        self._api_key = api_key
        self._token_counter = token_counter
        self._price_calculator = price_calculator
        self._usage_ledger = usage_ledger
        self._network_enabled = network_enabled
        self._retry_delay = retry_delay
        self._clock = clock
        self._monotonic = monotonic
        self._audit_recorder = audit_recorder
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
        monthly_before = self._usage_ledger.monthly_cost_usd()

        schema = _load_schema(request.agent_kind)
        audited_calls: list[_AuditedCall] = []
        try:
            first = self._invoke_logical_call(
                self._client,
                request,
                schema=schema,
                thinking=False,
                prior_output=None,
                attempt=1,
            )
            audited_calls.append(first)
            confidence = first.call.output.get("confidence")
            needs_review = (
                request.complex_multi_entity
                or not isinstance(confidence, int | float)
                or isinstance(confidence, bool)
                or confidence < LOW_CONFIDENCE_THRESHOLD
            )
            final = first
            if needs_review:
                final = self._invoke_logical_call(
                    self._client,
                    request,
                    schema=schema,
                    thinking=True,
                    prior_output=first.call.output,
                    attempt=2,
                )
                audited_calls.append(final)
        except GatewayFailure as exc:
            exc.audit_records = tuple(item.record for item in audited_calls) + exc.audit_records
            raise

        usage = _combine_usage(tuple(item.call.usage for item in audited_calls))
        cost = _combine_cost(tuple(item.cost for item in audited_calls))
        monthly_after = self._usage_ledger.monthly_cost_usd()
        budget_status = (
            BudgetStatus.WARNING
            if monthly_before >= SOFT_MONTHLY_LIMIT_USD or monthly_after >= SOFT_MONTHLY_LIMIT_USD
            else BudgetStatus.NORMAL
        )
        network_calls = sum(item.call.network_calls for item in audited_calls)
        return GatewayResult(
            output=final.call.output,
            provider_version=self.provider_version,
            tool_version=self.tool_version,
            observed_model=final.call.observed_model,
            usage=usage,
            cost=cost,
            budget_status=budget_status,
            network_calls=network_calls,
            model_api_calls=network_calls,
            tool_calls=0,
            invocations=tuple(item.record for item in audited_calls),
        )

    def _invoke_logical_call(
        self,
        client: httpx.Client,
        request: TokenizedModelRequest,
        *,
        schema: dict[str, Any],
        thinking: bool,
        prior_output: dict[str, Any] | None,
        attempt: int,
    ) -> _AuditedCall:
        started_at = self._validated_clock_now()
        started_tick = self._monotonic()
        call: _CallResult | None = None
        try:
            call = self._call(
                client,
                request,
                schema=schema,
                thinking=thinking,
                prior_output=prior_output,
                reservation_id=_stable_invocation_id(request, attempt),
            )
            cost = call.cost
        except GatewayFailure as exc:
            completed_at = self._validated_clock_now()
            latency_ms = max(0, int((self._monotonic() - started_tick) * 1000))
            record = self._audit_record(
                request=request,
                attempt=attempt,
                started_at=started_at,
                completed_at=completed_at,
                latency_ms=latency_ms,
                status="failed",
                usage=UsageSnapshot(status="unknown") if call is None else call.usage,
                cost=exc.cost,
                network_calls=exc.network_calls if call is None else call.network_calls,
                observed_model=(exc.observed_model if call is None else call.observed_model),
                response_id=exc.response_id if call is None else call.response_id,
                finish_code=exc.finish_code if call is None else call.finish_code,
                error_code=exc.error_code,
                output=None,
            )
            self._emit_audit(record)
            exc.audit_records = (record,)
            raise
        completed_at = self._validated_clock_now()
        latency_ms = max(0, int((self._monotonic() - started_tick) * 1000))
        record = self._audit_record(
            request=request,
            attempt=attempt,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            status="succeeded",
            usage=call.usage,
            cost=cost,
            network_calls=call.network_calls,
            observed_model=call.observed_model,
            response_id=call.response_id,
            finish_code=call.finish_code,
            error_code=None,
            output=call.output,
        )
        self._emit_audit(record)
        return _AuditedCall(call=call, cost=cost, record=record)

    def _audit_record(
        self,
        *,
        request: TokenizedModelRequest,
        attempt: int,
        started_at: datetime,
        completed_at: datetime,
        latency_ms: int,
        status: Literal["succeeded", "failed"],
        usage: UsageSnapshot,
        cost: CostSnapshot,
        network_calls: int,
        observed_model: str | None,
        response_id: str | None,
        finish_code: str | None,
        error_code: str | None,
        output: Mapping[str, Any] | None,
    ) -> ModelInvocationRecord:
        invocation_id = _stable_invocation_id(request, attempt)
        monthly = self._usage_ledger.monthly_cost_usd()
        if error_code == "network_disabled":
            budget_status: BudgetAuditStatus = "network_disabled"
        elif error_code == "budget_hard_stop":
            budget_status = "hard_stop"
        elif monthly >= SOFT_MONTHLY_LIMIT_USD:
            budget_status = "warning"
        else:
            budget_status = "normal"
        return ModelInvocationRecord(
            invocation_id=invocation_id,
            site_id=request.site_id,
            provider=DEEPSEEK_PROVIDER,
            requested_model=DEEPSEEK_MODEL,
            observed_model=observed_model,
            prompt_version=request.prompt_version,
            output_schema_version=_output_schema_version(request.agent_kind),
            policy_version=MODEL_GATEWAY_POLICY_VERSION,
            tokenizer_version=request.tokenizer_version,
            request_id=request.request_id,
            response_id=response_id,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            status=status,
            token_usage=_audit_usage(usage),
            cost=_audit_cost(cost),
            network_call_count=network_calls,
            tool_call_count=0,
            external_send_count=0,
            references=InvocationReferences(
                observation_event_refs=(),
                evidence_refs=request.evidence_refs,
                tokenization_receipt_refs=(request.tokenization_receipt_id,),
            ),
            idempotency_key=_stable_idempotency_key(request, attempt),
            attempt=attempt,
            retry_count=max(network_calls - 1, 0),
            finish_code=_sanitize_code(finish_code),
            error_code=error_code,
            budget_status=budget_status,
            price_catalog_version=cost.catalog_version,
            output_digest=None if output is None else _output_digest(output),
        )

    def _validated_clock_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise GatewayFailure(
                "audit clock must return a timezone-aware datetime",
                error_code="internal_error",
            )
        return value

    def _emit_audit(self, record: ModelInvocationRecord) -> None:
        if self._audit_recorder is not None:
            self._audit_recorder(record)

    def _call(
        self,
        client: httpx.Client,
        request: TokenizedModelRequest,
        *,
        schema: dict[str, Any],
        thinking: bool,
        prior_output: dict[str, Any] | None,
        reservation_id: str,
    ) -> _CallResult:
        if not self._network_enabled:
            raise ModelNetworkDisabled(
                "model network is disabled",
                error_code="network_disabled",
            )
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
            raise GatewayFailure(
                "model input token limit exceeded",
                error_code="input_token_limit",
            )
        worst_case_per_attempt = self._known_price(
            input_tokens=MAX_INPUT_TOKENS,
            output_tokens=MAX_OUTPUT_TOKENS,
        )
        assert worst_case_per_attempt.amount_usd is not None
        worst_case_cost = CostSnapshot(
            status="known",
            amount_usd=worst_case_per_attempt.amount_usd * MAX_NETWORK_ATTEMPTS,
            catalog_version=worst_case_per_attempt.catalog_version,
        )
        assert worst_case_cost.amount_usd is not None
        assert worst_case_cost.catalog_version is not None
        reservation = self._usage_ledger.reserve(
            reservation_id=reservation_id,
            amount_usd=worst_case_cost.amount_usd,
            price_catalog_version=worst_case_cost.catalog_version,
            token_counter_version=self._token_counter.version,
        )
        network_calls = 0
        ambiguous_attempts = 0
        try:
            response: httpx.Response | None = None
            for attempt in range(MAX_NETWORK_ATTEMPTS):
                self._usage_ledger.ensure_can_attempt(reservation)
                network_calls += 1
                try:
                    candidate = client.post(DEEPSEEK_CHAT_PATH, json=payload)
                except httpx.TransportError as exc:
                    if not isinstance(
                        exc,
                        (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
                    ):
                        ambiguous_attempts += 1
                    if attempt == MAX_NETWORK_ATTEMPTS - 1:
                        raise GatewayFailure(
                            "model transport failed after retries",
                            error_code="transport_exhausted",
                            network_calls=network_calls,
                        ) from exc
                    self._retry_delay(0.1 * (2**attempt))
                    continue
                if candidate.status_code == 429 or candidate.status_code >= 500:
                    if candidate.status_code >= 500:
                        ambiguous_attempts += 1
                    if attempt == MAX_NETWORK_ATTEMPTS - 1:
                        raise GatewayFailure(
                            "retryable model response exhausted retries",
                            error_code="retry_exhausted",
                            network_calls=network_calls,
                        )
                    self._retry_delay(0.1 * (2**attempt))
                    continue
                if not 200 <= candidate.status_code < 300:
                    raise GatewayFailure(
                        "non-retryable model response",
                        error_code="provider_http_error",
                        network_calls=network_calls,
                    )
                response = candidate
                break
            if response is None:
                raise GatewayFailure(
                    "model response unavailable",
                    error_code="internal_error",
                    network_calls=network_calls,
                )
            output, usage, observed_model, response_id, finish_code = _parse_response(
                response,
                request=request,
                schema=schema,
            )
            if usage.status == "unknown":
                raise GatewayFailure(
                    "model cost is unknown",
                    error_code="pricing_error",
                    network_calls=network_calls,
                    response_id=response_id,
                    observed_model=observed_model,
                    finish_code=finish_code,
                )
            cost = self._calculate_cost(usage)
            if ambiguous_attempts:
                assert cost.amount_usd is not None
                cost = CostSnapshot(
                    status="known",
                    amount_usd=(
                        cost.amount_usd + worst_case_per_attempt.amount_usd * ambiguous_attempts
                    ),
                    catalog_version=cost.catalog_version,
                )
            self._usage_ledger.settle(reservation, usage=usage, cost=cost)
        except GatewayFailure as exc:
            exc.network_calls = network_calls
            if network_calls > 0:
                exc.cost = self._usage_ledger.consume(reservation)
            else:
                self._usage_ledger.release(reservation)
            raise
        return _CallResult(
            output=output,
            usage=usage,
            observed_model=observed_model,
            response_id=response_id,
            finish_code=finish_code,
            network_calls=network_calls,
            cost=cost,
        )

    def _known_price(self, *, input_tokens: int, output_tokens: int) -> CostSnapshot:
        try:
            amount = self._price_calculator.calculate(
                model=DEEPSEEK_MODEL,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except (ArithmeticError, ValueError) as exc:
            raise GatewayFailure(
                "price calculator failed",
                error_code="pricing_error",
            ) from exc
        if amount < 0 or not amount.is_finite():
            raise GatewayFailure(
                "price calculator returned an invalid cost",
                error_code="pricing_error",
            )
        if not self._price_calculator.catalog_version:
            raise GatewayFailure(
                "price catalog version is unavailable",
                error_code="pricing_error",
            )
        return CostSnapshot(
            status="known",
            amount_usd=amount,
            catalog_version=self._price_calculator.catalog_version,
        )

    def _calculate_cost(self, usage: UsageSnapshot) -> CostSnapshot:
        if usage.status == "unknown":
            raise GatewayFailure("model cost is unknown", error_code="pricing_error")
        assert usage.input_tokens is not None
        assert usage.output_tokens is not None
        return self._known_price(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
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
) -> tuple[dict[str, Any], UsageSnapshot, str, str | None, str]:
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GatewayFailure(
            "model HTTP response was not valid JSON",
            error_code="response_invalid_json",
        ) from exc
    if not isinstance(body, dict):
        raise GatewayFailure(
            "model HTTP response was not an object",
            error_code="response_protocol_error",
        )
    response_id = body.get("id")
    if not isinstance(response_id, str) or not response_id or len(response_id) > 256:
        response_id = None
    observed_model = body.get("model")
    safe_observed_model = (
        observed_model
        if isinstance(observed_model, str) and 0 < len(observed_model) <= 160
        else None
    )
    if observed_model != DEEPSEEK_MODEL:
        raise GatewayFailure(
            "observed model mismatch",
            error_code="model_mismatch",
            response_id=response_id,
            observed_model=safe_observed_model,
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise GatewayFailure(
            "model response choices were invalid",
            error_code="response_protocol_error",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
        )
    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    safe_finish_code = _sanitize_code(finish_reason if isinstance(finish_reason, str) else None)
    if finish_reason != "stop":
        raise GatewayFailure(
            "model response was rejected or truncated",
            error_code="response_protocol_error",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code=safe_finish_code,
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise GatewayFailure(
            "model response message was invalid",
            error_code="response_protocol_error",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        )
    if "tool_calls" in message or message.get("refusal") is not None:
        raise GatewayFailure(
            "model response attempted tools or refusal",
            error_code="response_protocol_error",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise GatewayFailure(
            "model response content was empty",
            error_code="response_protocol_error",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        )
    try:
        output = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GatewayFailure(
            "model content was not valid JSON",
            error_code="output_invalid_json",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        ) from exc
    if not isinstance(output, dict):
        raise GatewayFailure(
            "model content must be a JSON object",
            error_code="output_invalid_json",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        )
    errors = tuple(Draft202012Validator(schema).iter_errors(output))
    if errors:
        raise GatewayFailure(
            "model content failed the per-agent schema",
            error_code="output_schema_invalid",
            response_id=response_id,
            observed_model=DEEPSEEK_MODEL,
            finish_code="stop",
        )
    try:
        _validate_request_binding(output, request)
        _validate_recursive_safety(output)
    except GatewayFailure as exc:
        exc.response_id = response_id
        exc.observed_model = DEEPSEEK_MODEL
        exc.finish_code = "stop"
        raise
    usage = _parse_usage(body.get("usage"))
    return output, usage, DEEPSEEK_MODEL, response_id, "stop"


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


def _combine_cost(values: tuple[CostSnapshot, ...]) -> CostSnapshot:
    if any(value.status == "unknown" for value in values):
        return CostSnapshot(status="unknown")
    amounts = tuple(value.amount_usd for value in values)
    versions = {value.catalog_version for value in values}
    if any(amount is None for amount in amounts) or len(versions) != 1:
        return CostSnapshot(status="unknown")
    return CostSnapshot(
        status="known",
        amount_usd=sum((amount for amount in amounts if amount is not None), Decimal("0")),
        catalog_version=versions.pop(),
    )


def _load_schema(agent_kind: str) -> dict[str, Any]:
    output_schema_version = _output_schema_version(agent_kind)
    path = (
        Path(__file__).parents[2]
        / "contracts"
        / "local_pilot"
        / f"{output_schema_version}.schema.json"
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
        raise GatewayFailure(
            "model content did not bind to the request",
            error_code="request_binding_failed",
        )


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
    "执行付款",
    "发起付款",
    "执行折扣",
    "批准折扣",
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
                raise GatewayFailure(
                    "model content contained a forbidden result field",
                    error_code="unsafe_output",
                )
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
            raise GatewayFailure(
                "model content contained forbidden action language",
                error_code="unsafe_output",
            )


def _audit_usage(usage: UsageSnapshot) -> TokenUsageMetadata:
    if usage.status == "unknown":
        return TokenUsageMetadata.unknown()
    assert usage.input_tokens is not None
    assert usage.output_tokens is not None
    assert usage.total_tokens is not None
    return TokenUsageMetadata.known(
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
    )


def _audit_cost(cost: CostSnapshot) -> CostMetadata:
    if cost.status == "unknown":
        return CostMetadata.unknown()
    assert cost.amount_usd is not None
    return CostMetadata.known(cost.amount_usd, "USD")


def _stable_invocation_id(request: TokenizedModelRequest, attempt: int) -> str:
    return f"invocation-{_invocation_identity_digest(request, attempt)}"


def _stable_idempotency_key(request: TokenizedModelRequest, attempt: int) -> str:
    return f"model-call-{_invocation_identity_digest(request, attempt)}"


def _invocation_identity_digest(request: TokenizedModelRequest, attempt: int) -> str:
    material = {
        "agent_kind": request.agent_kind,
        "attempt": attempt,
        "complex_multi_entity": request.complex_multi_entity,
        "evidence_refs": list(request.evidence_refs),
        "mapping_digest": request.mapping_digest,
        "output_schema_version": _output_schema_version(request.agent_kind),
        "policy_version": MODEL_GATEWAY_POLICY_VERSION,
        "prompt_version": request.prompt_version,
        "provider": DEEPSEEK_PROVIDER,
        "provider_version": DeepSeekAdapter.provider_version,
        "purpose": request.purpose,
        "request_id": request.request_id,
        "requested_model": DEEPSEEK_MODEL,
        "site_id": request.site_id,
        "subject_ref": request.subject_ref,
        "tokenization_receipt_id": request.tokenization_receipt_id,
        "tokenizer_version": request.tokenizer_version,
        "tool_version": DeepSeekAdapter.tool_version,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(canonical).hexdigest()


def _output_schema_version(agent_kind: str) -> str:
    try:
        return OUTPUT_SCHEMA_VERSIONS[agent_kind]
    except KeyError as exc:
        raise GatewayFailure(
            "agent output schema version is unavailable",
            error_code="internal_error",
        ) from exc


def _output_digest(output: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(canonical).hexdigest()


def _sanitize_code(value: str | None) -> str | None:
    if value is None or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) is None:
        return None
    return value
