from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import httpx
import pytest

from services.model_gateway.deepseek import (
    BudgetHardStop,
    BudgetStatus,
    DeepSeekAdapter,
    GatewayFailure,
    InMemoryUsageLedger,
    ModelNetworkDisabled,
    TokenizedModelRequest,
)


class CharacterTokenCounter:
    version = "test-character-counter-v1"

    def count(self, text: str) -> int:
        return len(text)


class FixedPriceCalculator:
    catalog_version = "test-price-catalog-v1"

    def calculate(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        assert model == "deepseek-v4-flash"
        return Decimal(input_tokens + output_tokens) / Decimal("1000000")


class FixedCallPriceCalculator:
    catalog_version = "test-fixed-call-price-v1"

    def __init__(self, amount: Decimal) -> None:
        self._amount = amount

    def calculate(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        return self._amount


def request(
    *,
    complex_multi_entity: bool = False,
    site_id: str = "gbos.localhost",
) -> TokenizedModelRequest:
    return TokenizedModelRequest(
        request_id="model-request-SYNTH-001",
        site_id=site_id,
        purpose="sales_follow_up",
        agent_kind="sales",
        subject_ref="DEAL-SYNTH-001",
        evidence_refs=("evidence-SYNTH-001",),
        prompt_version="sales-local-pilot-v1",
        tokenized_context="Customer <EMAIL_0123456789abcdef01234567> requests a follow-up.",
        tokenization_receipt_id="tokenization-SYNTH-001",
        tokenizer_version="stable-hmac-tokenizer-v1",
        mapping_digest="b" * 64,
        complex_multi_entity=complex_multi_entity,
    )


def sales_output(*, confidence: float = 0.82) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "proposal_id": "sales-proposal-SYNTH-001",
        "agent_kind": "sales",
        "action_type": "internal.work_item.propose",
        "status": "proposed",
        "subject_ref": "DEAL-SYNTH-001",
        "evidence_refs": ["evidence-SYNTH-001"],
        "confidence": confidence,
        "requires_human_review": True,
        "payload": {
            "title": "客户内部跟进",
            "summary": "整理已观察到的客户需求，等待销售人工复核。",
            "suggested_next_step": "创建内部跟进工作项。",
        },
    }


def api_response(
    output: dict[str, object] | str,
    *,
    model: str = "deepseek-v4-flash",
    finish_reason: str = "stop",
    include_usage: bool = True,
    extra_message: dict[str, object] | None = None,
) -> httpx.Response:
    content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    message: dict[str, object] = {"role": "assistant", "content": content}
    if extra_message:
        message.update(extra_message)
    body: dict[str, object] = {
        "id": "response-SYNTH-001",
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }
    if include_usage:
        body["usage"] = {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
        }
    return httpx.Response(200, json=body)


def adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    network_enabled: bool = True,
    monthly_cost: Decimal = Decimal("0"),
) -> DeepSeekAdapter:
    return DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=network_enabled,
        transport=httpx.MockTransport(handler),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(monthly_cost_usd=monthly_cost),
        retry_delay=lambda _: None,
    )


def test_request_rejects_residual_pii_before_provider() -> None:
    values = {field: getattr(request(), field) for field in request().__dataclass_fields__}
    values["tokenized_context"] = "Email alice@example.com"

    with pytest.raises(ValueError, match="PII"):
        TokenizedModelRequest(**values)


def test_happy_path_uses_fixed_endpoint_model_json_mode_and_no_tools() -> None:
    seen: list[tuple[httpx.Request, dict[str, object]]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        seen.append((http_request, payload))
        return api_response(sales_output())

    result = adapter(handler).invoke(request())

    assert len(seen) == 1
    http_request, payload = seen[0]
    assert str(http_request.url) == "https://api.deepseek.com/chat/completions"
    assert http_request.headers["authorization"] == "Bearer secret-test-key"
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 4096
    assert "tools" not in payload
    serialized_messages = json.dumps(payload["messages"]).casefold()
    assert "json" in serialized_messages
    assert "summary" in serialized_messages
    assert "extraction" in serialized_messages
    assert result.output == sales_output()
    assert result.network_calls == 1
    assert result.model_api_calls == 1
    assert result.tool_calls == 0
    assert result.usage.status == "known"
    assert result.cost.status == "known"
    assert result.cost.catalog_version == "test-price-catalog-v1"


@pytest.mark.parametrize("complex_multi_entity", [False, True])
def test_low_confidence_or_complex_result_allows_exactly_one_thinking_review(
    complex_multi_entity: bool,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        requests.append(payload)
        if len(requests) == 1:
            return api_response(sales_output(confidence=0.4 if not complex_multi_entity else 0.82))
        return api_response(sales_output(confidence=0.9))

    result = adapter(handler).invoke(request(complex_multi_entity=complex_multi_entity))

    assert len(requests) == 2
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]
    assert requests[1]["thinking"] == {"type": "enabled"}
    assert requests[1]["reasoning_effort"] == "high"
    assert result.output["confidence"] == 0.9
    assert result.network_calls == 2


@pytest.mark.parametrize(
    ("confidence", "expected_calls"),
    [
        (0.74, 2),
        (0.75, 1),
    ],
)
def test_thinking_review_confidence_threshold_is_point_seven_five(
    confidence: float,
    expected_calls: int,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(http_request.content))
        response_confidence = confidence if len(requests) == 1 else 0.9
        return api_response(sales_output(confidence=response_confidence))

    adapter(handler).invoke(request())

    assert len(requests) == expected_calls


def test_adapter_reuses_transport_across_review_and_later_invocation() -> None:
    class CloseAwareTransport(httpx.BaseTransport):
        def __init__(self) -> None:
            self.closed = False
            self.calls = 0

        def handle_request(self, http_request: httpx.Request) -> httpx.Response:
            if self.closed:
                raise httpx.ConnectError("transport already closed", request=http_request)
            self.calls += 1
            confidence = 0.4 if self.calls == 1 else 0.9
            response = api_response(sales_output(confidence=confidence))
            response.request = http_request
            return response

        def close(self) -> None:
            self.closed = True

    transport = CloseAwareTransport()
    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=transport,
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
    )

    result = active.invoke(request())

    assert transport.calls == 2
    assert transport.closed is False
    assert result.output["confidence"] == 0.9
    second = active.invoke(request())
    assert transport.calls == 3
    assert second.output["confidence"] == 0.9
    active.close()
    assert transport.closed is True


def test_network_kill_switch_defaults_closed_and_hard_budget_stops_before_http() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return api_response(sales_output())

    with pytest.raises(ModelNetworkDisabled):
        adapter(handler, network_enabled=False).invoke(request())
    with pytest.raises(BudgetHardStop):
        adapter(handler, monthly_cost=Decimal("100")).invoke(request())
    assert calls == 0


def test_soft_budget_threshold_returns_warning_state() -> None:
    result = adapter(
        lambda _: api_response(sales_output()),
        monthly_cost=Decimal("50"),
    ).invoke(request())

    assert result.budget_status is BudgetStatus.WARNING


def test_first_call_cost_reaching_hard_limit_blocks_review_before_second_http() -> None:
    calls = 0
    ledger = InMemoryUsageLedger(monthly_cost_usd=Decimal("99.99"))

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return api_response(sales_output(confidence=0.74))

    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(handler),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedCallPriceCalculator(Decimal("0.01")),
        usage_ledger=ledger,
        retry_delay=lambda _: None,
    )

    with pytest.raises(BudgetHardStop):
        active.invoke(request(complex_multi_entity=True))

    assert calls == 1
    assert ledger.monthly_cost_usd() == Decimal("100.00")
    assert len(ledger.records) == 1


def test_first_call_is_recorded_when_review_protocol_fails() -> None:
    calls = 0
    ledger = InMemoryUsageLedger()

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return api_response(sales_output(confidence=0.74))
        return api_response("not-json")

    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(handler),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedCallPriceCalculator(Decimal("0.01")),
        usage_ledger=ledger,
        retry_delay=lambda _: None,
    )

    with pytest.raises(GatewayFailure):
        active.invoke(request(complex_multi_entity=True))

    assert calls == 2
    assert ledger.monthly_cost_usd() == Decimal("0.01")
    assert len(ledger.records) == 1


def test_successful_review_returns_sum_of_per_call_costs() -> None:
    ledger = InMemoryUsageLedger()
    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(lambda _: api_response(sales_output(confidence=0.74))),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedCallPriceCalculator(Decimal("0.01")),
        usage_ledger=ledger,
        retry_delay=lambda _: None,
    )

    result = active.invoke(request(complex_multi_entity=True))

    assert result.cost.status == "known"
    assert result.cost.amount_usd == Decimal("0.02")
    assert ledger.monthly_cost_usd() == Decimal("0.02")
    assert len(ledger.records) == 2


def test_retryable_failures_retry_at_most_twice() -> None:
    outcomes: list[httpx.Response | Exception] = [
        httpx.ConnectError("synthetic connection failure"),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        api_response(sales_output()),
    ]
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.request = http_request
        return outcome

    result = adapter(handler).invoke(request())

    assert calls == 3
    assert result.network_calls == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": {"message": "bad request"}}),
        api_response(sales_output(), model="other-model"),
        api_response(""),
        api_response("not-json"),
        api_response(sales_output(), finish_reason="length"),
        api_response(sales_output(), extra_message={"tool_calls": []}),
        api_response(sales_output(), extra_message={"refusal": "no"}),
    ],
)
def test_protocol_violations_fail_closed_without_retry(response: httpx.Response) -> None:
    calls = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response.request = http_request
        return response

    with pytest.raises(GatewayFailure):
        adapter(handler).invoke(request())
    assert calls == 1


def test_missing_usage_and_cost_are_unknown_not_zero() -> None:
    result = adapter(lambda _: api_response(sales_output(), include_usage=False)).invoke(request())

    assert result.usage.status == "unknown"
    assert result.usage.input_tokens is None
    assert result.cost.status == "unknown"
    assert result.cost.amount_usd is None


def test_external_messages_do_not_include_local_site_identifier() -> None:
    sentinel = "sensitive-org.example"
    messages: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        messages.append(json.dumps(payload["messages"], ensure_ascii=False))
        return api_response(sales_output())

    adapter(handler).invoke(request(site_id=sentinel))

    assert messages
    assert sentinel not in messages[0]


@pytest.mark.parametrize(
    "unsafe_mutation",
    [
        {"tool_calls": []},
        {"payload": {"formal_price": "100.00"}},
        {"payload": {"nested": [{"external_send": True}]}},
        {"payload": {"summary": "Create order.create now"}},
    ],
)
def test_schema_and_recursive_safety_reject_arbitrary_or_unsafe_json(
    unsafe_mutation: dict[str, object],
) -> None:
    output = sales_output()
    output.update(unsafe_mutation)

    with pytest.raises(GatewayFailure):
        adapter(lambda _: api_response(output)).invoke(request())


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "Send an outbound email now",
        "Provide the formal price and formal discount",
        "Request payment",
        "Promise the delivery date",
        "Create the final order",
        "Mark this deal Won",
        "Mark this deal Lost",
        "Complete the final supplier selection",
        "Publish the official KPI",
        "Return DraftMutation and ApprovedCommand",
        "Execute the write operation",
        "外发客户消息",
        "给出正式价格、折扣和付款安排",
        "承诺交期并创建订单",
        "标记赢单或输单",
        "完成供应商最终选择",
        "发布正式 KPI",
    ],
)
def test_recursive_safety_rejects_commitment_language_inside_allowed_fields(
    unsafe_text: str,
) -> None:
    output = sales_output()
    payload = output["payload"]
    assert isinstance(payload, dict)
    payload["summary"] = unsafe_text

    with pytest.raises(GatewayFailure):
        adapter(lambda _: api_response(output)).invoke(request())


def test_recursive_safety_allows_non_committal_payment_and_discount_fact() -> None:
    output = sales_output()
    payload = output["payload"]
    assert isinstance(payload, dict)
    payload["summary"] = "客户询问付款条款和折扣，待人工确认。"

    result = adapter(lambda _: api_response(output)).invoke(request())

    assert result.output == output


def test_success_emits_schema_valid_content_free_audit_with_deterministic_timing() -> None:
    records = []
    instants = iter(
        [
            datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
            datetime(2026, 8, 7, 1, 0, 0, 250000, tzinfo=UTC),
        ]
    )
    ticks = iter([10.0, 10.25])
    output = sales_output()
    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(lambda _: api_response(output)),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
        clock=lambda: next(instants),
        monotonic=lambda: next(ticks),
        audit_recorder=records.append,
    )

    result = active.invoke(request())

    assert result.invocations == tuple(records)
    assert len(records) == 1
    audit = records[0]
    assert audit.requested_model == "deepseek-v4-flash"
    assert audit.observed_model == "deepseek-v4-flash"
    assert audit.response_id == "response-SYNTH-001"
    assert audit.prompt_version == "sales-local-pilot-v1"
    assert audit.output_schema_version == "sales-proposal-v1.0"
    assert audit.policy_version == "model-gateway-policy-v1"
    assert audit.tokenizer_version == "stable-hmac-tokenizer-v1"
    assert audit.started_at == datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    assert audit.completed_at == datetime(2026, 8, 7, 1, 0, 0, 250000, tzinfo=UTC)
    assert audit.latency_ms == 250
    assert audit.status == "succeeded"
    assert audit.network_call_count == 1
    assert audit.retry_count == 0
    assert audit.tool_call_count == 0
    assert audit.external_send_count == 0
    assert audit.references.evidence_refs == ("evidence-SYNTH-001",)
    assert audit.references.tokenization_receipt_refs == ("tokenization-SYNTH-001",)
    assert (
        audit.output_digest
        == sha256(
            json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    assert "客户内部跟进" not in repr(audit)
    assert "reasoning_content" not in repr(audit)


def test_review_has_a_distinct_stable_audit_record_per_logical_call() -> None:
    records = []
    start = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    instants = iter([start, start, start + timedelta(seconds=1), start + timedelta(seconds=1)])
    ticks = iter([10.0, 10.1, 11.0, 11.2])
    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(lambda _: api_response(sales_output(confidence=0.74))),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
        clock=lambda: next(instants),
        monotonic=lambda: next(ticks),
        audit_recorder=records.append,
    )

    result = active.invoke(request(complex_multi_entity=True))

    assert len(result.invocations) == 2
    assert [item.attempt for item in records] == [1, 2]
    assert records[0].invocation_id != records[1].invocation_id
    assert records[0].idempotency_key.endswith(":1")
    assert records[1].idempotency_key.endswith(":2")


def test_kill_switch_and_hard_budget_emit_zero_network_failure_audits() -> None:
    for network_enabled, monthly_cost, exception_type, error_code, budget_status in (
        (False, Decimal("0"), ModelNetworkDisabled, "network_disabled", "network_disabled"),
        (True, Decimal("100"), BudgetHardStop, "budget_hard_stop", "hard_stop"),
    ):
        records = []
        active = DeepSeekAdapter(
            api_key="secret-test-key",
            network_enabled=network_enabled,
            transport=httpx.MockTransport(lambda _: api_response(sales_output())),
            token_counter=CharacterTokenCounter(),
            price_calculator=FixedPriceCalculator(),
            usage_ledger=InMemoryUsageLedger(monthly_cost_usd=monthly_cost),
            retry_delay=lambda _: None,
            audit_recorder=records.append,
        )

        with pytest.raises(exception_type) as captured:
            active.invoke(request())

        assert captured.value.audit_records == tuple(records)
        assert len(records) == 1
        assert records[0].status == "failed"
        assert records[0].error_code == error_code
        assert records[0].budget_status == budget_status
        assert records[0].network_call_count == 0
        assert records[0].retry_count == 0
        assert records[0].token_usage.status == "unknown"
        assert records[0].cost.status == "unknown"


def test_retry_exhaustion_emits_sanitized_failure_without_provider_error_body() -> None:
    records = []
    response = httpx.Response(
        429,
        json={
            "error": {
                "message": "alice@example.com secret provider body",
                "reasoning_content": "hidden",
            }
        },
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        response.request = http_request
        return response

    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(handler),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
        audit_recorder=records.append,
    )

    with pytest.raises(GatewayFailure) as captured:
        active.invoke(request())

    assert captured.value.audit_records == tuple(records)
    assert len(records) == 1
    assert records[0].error_code == "retry_exhausted"
    assert records[0].network_call_count == 3
    assert records[0].retry_count == 2
    assert "alice@example.com" not in repr(records[0])
    assert "hidden" not in repr(records[0])


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (api_response(sales_output(), model="other-model"), "model_mismatch"),
        (api_response(""), "response_protocol_error"),
        (api_response("not-json"), "output_invalid_json"),
        (api_response(sales_output(), finish_reason="length"), "response_protocol_error"),
    ],
)
def test_protocol_failures_emit_only_sanitized_error_codes(
    response: httpx.Response,
    error_code: str,
) -> None:
    records = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        response.request = http_request
        return response

    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(handler),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
        audit_recorder=records.append,
    )

    with pytest.raises(GatewayFailure) as captured:
        active.invoke(request())

    assert captured.value.audit_records == tuple(records)
    assert records[0].error_code == error_code
    assert records[0].output_digest is None


def test_unsafe_output_failure_keeps_only_safe_provider_identifiers() -> None:
    records = []
    output = sales_output()
    payload = output["payload"]
    assert isinstance(payload, dict)
    payload["summary"] = "Send an outbound email now"
    active = DeepSeekAdapter(
        api_key="secret-test-key",
        network_enabled=True,
        transport=httpx.MockTransport(lambda _: api_response(output)),
        token_counter=CharacterTokenCounter(),
        price_calculator=FixedPriceCalculator(),
        usage_ledger=InMemoryUsageLedger(),
        retry_delay=lambda _: None,
        audit_recorder=records.append,
    )

    with pytest.raises(GatewayFailure):
        active.invoke(request())

    assert records[0].error_code == "unsafe_output"
    assert records[0].observed_model == "deepseek-v4-flash"
    assert records[0].response_id == "response-SYNTH-001"
    assert records[0].finish_code == "stop"
