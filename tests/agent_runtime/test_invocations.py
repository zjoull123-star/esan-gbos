from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from services.agent_runtime.invocations import (
    CostMetadata,
    IdempotencyConflict,
    InMemoryModelInvocationRepository,
    InvocationReferences,
    ModelInvocationRecord,
    PostgresModelInvocationRepository,
    TokenUsageMetadata,
)

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def record(
    *,
    site_id: str = "site-a",
    invocation_id: str = "invocation-1",
    idempotency_key: str = "request-1:1",
    output_digest: str | None = "a" * 64,
    usage: TokenUsageMetadata | None = None,
    cost: CostMetadata | None = None,
) -> ModelInvocationRecord:
    cost_metadata = cost or CostMetadata.known(Decimal("0.0002"), "USD")
    return ModelInvocationRecord(
        invocation_id=invocation_id,
        site_id=site_id,
        provider="deepseek",
        requested_model="deepseek-v4-flash",
        observed_model="deepseek-v4-flash",
        prompt_version="sales-local-pilot-v1",
        output_schema_version="sales-proposal-v1.0",
        policy_version="model-gateway-policy-v1",
        tokenizer_version="stable-hmac-tokenizer-v1",
        request_id="request-1",
        response_id="response-1",
        started_at=NOW,
        completed_at=NOW,
        latency_ms=12,
        status="succeeded",
        token_usage=usage or TokenUsageMetadata.known(120, 80, 200),
        cost=cost_metadata,
        network_call_count=1,
        tool_call_count=0,
        external_send_count=0,
        references=InvocationReferences(
            observation_event_refs=(),
            evidence_refs=("evidence-1",),
            tokenization_receipt_refs=("receipt-1",),
        ),
        idempotency_key=idempotency_key,
        attempt=1,
        retry_count=0,
        finish_code="stop",
        error_code=None,
        budget_status="normal",
        price_catalog_version=("catalog-v1" if cost_metadata.status == "known" else None),
        output_digest=output_digest,
    )


def test_model_invocation_wire_payload_is_schema_valid_and_content_free() -> None:
    item = record()

    payload = item.to_wire()
    schema = ModelInvocationRecord.load_wire_schema()
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    json.dumps(payload)

    assert set(payload) == set(schema["required"])
    serialized = repr(payload).casefold()
    for forbidden in (
        "prompt_text",
        "response_text",
        "reasoning_content",
        "api_key",
        "token_map",
        "email",
        "phone",
        "person_name",
        "organization_text",
        "output_digest",
        "error_code",
    ):
        assert forbidden not in serialized


def test_unknown_usage_and_cost_round_trip_without_zero_values() -> None:
    repository = InMemoryModelInvocationRepository()
    item = record(
        usage=TokenUsageMetadata.unknown(),
        cost=CostMetadata.unknown(),
    )

    stored = repository.append(item)
    loaded = repository.get("site-a", item.invocation_id)

    assert stored == item
    assert loaded == item
    assert loaded is not None
    assert loaded.token_usage.to_wire() == {"status": "unknown"}
    assert loaded.cost.to_wire() == {"status": "unknown"}
    assert loaded.token_usage.input_tokens is None
    assert loaded.cost.amount is None


def test_in_memory_append_is_idempotent_and_rejects_changed_digest() -> None:
    repository = InMemoryModelInvocationRepository()
    item = record()

    assert repository.append(item) == item
    assert repository.append(item) == item

    with pytest.raises(IdempotencyConflict):
        repository.append(record(output_digest="b" * 64))


@pytest.mark.parametrize(
    "changes",
    [
        {"site_id": "../site"},
        {"latency_ms": -1},
        {"network_call_count": -1},
        {"completed_at": datetime(2026, 8, 7, 8, 59, tzinfo=UTC)},
        {"status": "succeeded", "error_code": "protocol_error"},
        {"status": "failed", "error_code": None},
        {"output_digest": "not-a-digest"},
    ],
)
def test_record_rejects_invalid_metadata(changes: dict[str, object]) -> None:
    values = {field: getattr(record(), field) for field in record().__dataclass_fields__}
    values.update(changes)

    with pytest.raises(ValueError):
        ModelInvocationRecord(**values)


def test_dataclass_has_no_content_bearing_fields() -> None:
    field_names = set(ModelInvocationRecord.__dataclass_fields__)

    assert not field_names.intersection(
        {
            "prompt",
            "response",
            "reasoning_content",
            "api_key",
            "token_map",
            "email",
            "phone",
            "name",
            "organization",
        }
    )


class RecordingCursor:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [row for row in self.rows if row is not None]


class RecordingConnection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.cursor_instance = RecordingCursor(rows)
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def row(item: ModelInvocationRecord) -> tuple[Any, ...]:
    return (
        item.invocation_id,
        item.site_id,
        item.provider,
        item.requested_model,
        item.observed_model,
        item.prompt_version,
        item.output_schema_version,
        item.policy_version,
        item.tokenizer_version,
        item.request_id,
        item.response_id,
        item.started_at,
        item.completed_at,
        item.latency_ms,
        item.status,
        item.token_usage.status,
        item.token_usage.input_tokens,
        item.token_usage.output_tokens,
        item.token_usage.total_tokens,
        item.cost.status,
        item.cost.amount,
        item.cost.currency,
        item.network_call_count,
        item.tool_call_count,
        item.external_send_count,
        list(item.references.observation_event_refs),
        list(item.references.evidence_refs),
        list(item.references.tokenization_receipt_refs),
        item.idempotency_key,
        item.attempt,
        item.retry_count,
        item.finish_code,
        item.error_code,
        item.budget_status,
        item.price_catalog_version,
        item.output_digest,
    )


def test_postgres_append_is_parameterized_transactional_and_metadata_only() -> None:
    item = record()
    connection = RecordingConnection([row(item)])
    repository = PostgresModelInvocationRepository(connection)

    assert repository.append(item) == item

    assert connection.transactions == 1
    statements = connection.cursor_instance.executed
    sql = "\n".join(statement for statement, _ in statements)
    assert "set_config('app.site_id', %s, true)" in sql
    assert "ON CONFLICT (site_id, idempotency_key) DO NOTHING" in sql
    assert item.site_id not in sql
    assert item.output_digest not in sql
    assert "prompt_text" not in sql.casefold()
    assert "response_text" not in sql.casefold()
    assert "reasoning_content" not in sql.casefold()
    assert all(params is not None for _, params in statements)


def test_postgres_append_rejects_same_key_with_changed_digest() -> None:
    conflicting = record(output_digest="b" * 64)
    connection = RecordingConnection([None, row(conflicting)])
    repository = PostgresModelInvocationRepository(connection)

    with pytest.raises(IdempotencyConflict):
        repository.append(record())


def test_record_bundle_reserves_transaction_scope_without_claiming_proposal_atomicity() -> None:
    item = record()
    connection = RecordingConnection([row(item)])
    repository = PostgresModelInvocationRepository(connection)

    with repository.record_bundle("site-a") as bundle:
        stored = bundle.append(item)

    assert stored == item
    assert connection.transactions == 1


def test_in_memory_record_bundle_rolls_back_appends_when_body_raises() -> None:
    repository = InMemoryModelInvocationRepository()
    item = record(invocation_id="invocation-rollback", idempotency_key="rollback-key")

    with (
        pytest.raises(RuntimeError, match="synthetic bundle failure"),
        repository.record_bundle("site-a") as bundle,
    ):
        bundle.append(item)
        raise RuntimeError("synthetic bundle failure")

    assert repository.get("site-a", item.invocation_id) is None
    assert repository.list("site-a") == ()


def test_in_memory_record_bundle_rolls_back_prior_append_on_later_conflict() -> None:
    repository = InMemoryModelInvocationRepository()
    existing = record(invocation_id="invocation-existing", idempotency_key="existing-key")
    repository.append(existing)
    first_in_bundle = record(
        invocation_id="invocation-bundle-first",
        idempotency_key="bundle-first-key",
    )
    conflicting = record(
        invocation_id=existing.invocation_id,
        idempotency_key=existing.idempotency_key,
        output_digest="b" * 64,
    )

    with (
        pytest.raises(IdempotencyConflict),
        repository.record_bundle("site-a") as bundle,
    ):
        bundle.append(first_in_bundle)
        bundle.append(conflicting)

    assert repository.get("site-a", first_in_bundle.invocation_id) is None
    assert repository.get("site-a", existing.invocation_id) == existing
    assert repository.list("site-a") == (existing,)
