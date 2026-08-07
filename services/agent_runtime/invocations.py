from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, runtime_checkable

from .models import IdempotencyConflict, ValidationError
from .postgres import Connection, Cursor

InvocationStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]
UsageStatus = Literal["known", "unknown"]
BudgetAuditStatus = Literal["normal", "warning", "hard_stop", "network_disabled", "unknown"]

_SITE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_ERROR_CODES = frozenset(
    {
        "network_disabled",
        "budget_hard_stop",
        "input_token_limit",
        "transport_exhausted",
        "retry_exhausted",
        "provider_http_error",
        "response_invalid_json",
        "model_mismatch",
        "response_protocol_error",
        "output_invalid_json",
        "output_schema_invalid",
        "request_binding_failed",
        "unsafe_output",
        "pricing_error",
        "internal_error",
    }
)
_RECORD_COLUMNS = """
    invocation_id, site_id, provider, requested_model, observed_model,
    prompt_version, output_schema_version, policy_version, tokenizer_version,
    request_id, response_id, started_at, completed_at, latency_ms, status,
    token_usage_status, input_tokens, output_tokens, total_tokens,
    cost_status, cost_amount, cost_currency, network_call_count,
    tool_call_count, external_send_count, observation_event_refs,
    evidence_refs, tokenization_receipt_refs, idempotency_key, attempt,
    retry_count, finish_code, error_code, budget_status,
    price_catalog_version, output_digest
"""


def _require_text(value: str, name: str, *, maximum: int) -> None:
    if not value or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty and at most {maximum} characters")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TokenUsageMetadata:
    status: UsageStatus
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        if self.status == "unknown":
            if any(value is not None for value in values):
                raise ValueError("unknown token usage cannot contain token counts")
            return
        if self.status != "known":
            raise ValueError("invalid token usage status")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("known token usage requires non-negative integer counts")
        assert self.input_tokens is not None
        assert self.output_tokens is not None
        assert self.total_tokens is not None
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")

    @classmethod
    def unknown(cls) -> TokenUsageMetadata:
        return cls(status="unknown")

    @classmethod
    def known(
        cls,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> TokenUsageMetadata:
        return cls(
            status="known",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def to_wire(self) -> dict[str, str | int]:
        if self.status == "unknown":
            return {"status": "unknown"}
        assert self.input_tokens is not None
        assert self.output_tokens is not None
        assert self.total_tokens is not None
        return {
            "status": "known",
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class CostMetadata:
    status: UsageStatus
    amount: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.status == "unknown":
            if self.amount is not None or self.currency is not None:
                raise ValueError("unknown cost cannot contain amount or currency")
            return
        if self.status != "known":
            raise ValueError("invalid cost status")
        if self.amount is None or self.amount < 0 or not self.amount.is_finite():
            raise ValueError("known cost requires a finite non-negative amount")
        if self.currency is None or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("known cost requires an ISO-style currency code")

    @classmethod
    def unknown(cls) -> CostMetadata:
        return cls(status="unknown")

    @classmethod
    def known(cls, amount: Decimal, currency: str) -> CostMetadata:
        return cls(status="known", amount=amount, currency=currency)

    def to_wire(self) -> dict[str, str | float]:
        if self.status == "unknown":
            return {"status": "unknown"}
        assert self.amount is not None
        assert self.currency is not None
        return {"status": "known", "amount": float(self.amount), "currency": self.currency}


@dataclass(frozen=True, slots=True)
class InvocationReferences:
    observation_event_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    tokenization_receipt_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "observation_event_refs",
            "evidence_refs",
            "tokenization_receipt_refs",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
            if any(not value or len(value) > 256 for value in values):
                raise ValueError(f"{name} entries must be non-empty and at most 256 characters")

    def to_wire(self) -> dict[str, list[str]]:
        return {
            "observation_event_refs": list(self.observation_event_refs),
            "evidence_refs": list(self.evidence_refs),
            "tokenization_receipt_refs": list(self.tokenization_receipt_refs),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelInvocationRecord:
    invocation_id: str
    site_id: str
    provider: str
    requested_model: str
    observed_model: str | None
    prompt_version: str
    output_schema_version: str
    policy_version: str
    tokenizer_version: str
    request_id: str
    response_id: str | None
    started_at: datetime
    completed_at: datetime | None
    latency_ms: int | None
    status: InvocationStatus
    token_usage: TokenUsageMetadata
    cost: CostMetadata
    network_call_count: int
    tool_call_count: int
    external_send_count: int
    references: InvocationReferences
    idempotency_key: str
    attempt: int
    retry_count: int
    finish_code: str | None
    error_code: str | None
    budget_status: BudgetAuditStatus
    price_catalog_version: str | None
    output_digest: str | None
    schema_version: Literal["1.0"] = "1.0"

    def __post_init__(self) -> None:
        _require_text(self.invocation_id, "invocation_id", maximum=256)
        _require_text(self.site_id, "site_id", maximum=140)
        if _SITE_PATTERN.fullmatch(self.site_id) is None:
            raise ValueError("site_id has an invalid format")
        for name, maximum in (
            ("provider", 80),
            ("requested_model", 160),
            ("prompt_version", 80),
            ("output_schema_version", 80),
            ("policy_version", 80),
            ("tokenizer_version", 80),
            ("request_id", 256),
            ("idempotency_key", 256),
        ):
            _require_text(getattr(self, name), name, maximum=maximum)
        for name, maximum in (
            ("observed_model", 160),
            ("response_id", 256),
            ("price_catalog_version", 80),
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name, maximum=maximum)
        _require_aware(self.started_at, "started_at")
        if self.completed_at is None:
            if self.latency_ms is not None:
                raise ValueError("latency_ms requires completed_at")
        else:
            _require_aware(self.completed_at, "completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("completed_at cannot precede started_at")
            if self.latency_ms is None:
                raise ValueError("completed records require latency_ms")
        if self.latency_ms is not None and (
            not isinstance(self.latency_ms, int)
            or isinstance(self.latency_ms, bool)
            or self.latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer")
        if self.status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise ValueError("invalid invocation status")
        for name in ("network_call_count", "tool_call_count", "external_send_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
            or self.retry_count < 0
        ):
            raise ValueError("retry_count must be a non-negative integer")
        if self.network_call_count == 0 and self.retry_count != 0:
            raise ValueError("zero-network invocations cannot have retries")
        if self.network_call_count > 0 and self.retry_count >= self.network_call_count:
            raise ValueError("retry_count must be less than network_call_count")
        if self.finish_code is not None and _CODE_PATTERN.fullmatch(self.finish_code) is None:
            raise ValueError("finish_code must be a sanitized code")
        if self.error_code is not None and self.error_code not in _ERROR_CODES:
            raise ValueError("error_code must be a known sanitized code")
        if self.status == "succeeded":
            if self.error_code is not None:
                raise ValueError("succeeded records cannot have an error_code")
            if self.output_digest is None:
                raise ValueError("succeeded records require output_digest")
        elif self.error_code is None:
            raise ValueError("non-succeeded records require an error_code")
        if self.budget_status not in {
            "normal",
            "warning",
            "hard_stop",
            "network_disabled",
            "unknown",
        }:
            raise ValueError("invalid budget_status")
        if self.output_digest is not None and _DIGEST_PATTERN.fullmatch(self.output_digest) is None:
            raise ValueError("output_digest must be a SHA-256 hex digest")
        if self.cost.status == "known" and self.price_catalog_version is None:
            raise ValueError("known cost requires price_catalog_version")
        if self.schema_version != "1.0":
            raise ValueError("schema_version must be 1.0")

    @staticmethod
    def load_wire_schema() -> dict[str, Any]:
        path = (
            Path(__file__).parents[2]
            / "contracts"
            / "local_pilot"
            / "model-invocation-v1.0.schema.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("model invocation schema must be an object")
        return value

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "site_id": self.site_id,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "observed_model": self.observed_model,
            "prompt_version": self.prompt_version,
            "output_schema_version": self.output_schema_version,
            "policy_version": self.policy_version,
            "tokenizer_version": self.tokenizer_version,
            "request_id": self.request_id,
            "response_id": self.response_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": (None if self.completed_at is None else self.completed_at.isoformat()),
            "latency_ms": self.latency_ms,
            "status": self.status,
            "token_usage": self.token_usage.to_wire(),
            "cost": self.cost.to_wire(),
            "network_call_count": self.network_call_count,
            "tool_call_count": self.tool_call_count,
            "external_send_count": self.external_send_count,
            "references": self.references.to_wire(),
        }


@runtime_checkable
class ModelInvocationRepository(Protocol):
    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord: ...

    def get(self, site_id: str, invocation_id: str) -> ModelInvocationRecord | None: ...

    def list(self, site_id: str, *, limit: int = 100) -> tuple[ModelInvocationRecord, ...]: ...

    def record_bundle(self, site_id: str) -> AbstractContextManager[ModelInvocationBundle]: ...


class ModelInvocationBundle(Protocol):
    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord: ...


class InMemoryModelInvocationRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ModelInvocationRecord] = {}
        self._idempotency: dict[tuple[str, str], ModelInvocationRecord] = {}
        self._lock = RLock()

    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord:
        with self._lock:
            return self._append(record)

    def _append(self, record: ModelInvocationRecord) -> ModelInvocationRecord:
        idempotency_key = (record.site_id, record.idempotency_key)
        existing = self._idempotency.get(idempotency_key)
        if existing is not None:
            if existing != record:
                raise IdempotencyConflict(
                    "model invocation idempotency key was reused with different metadata"
                )
            return existing
        primary_key = (record.site_id, record.invocation_id)
        if primary_key in self._records:
            raise IdempotencyConflict("model invocation id was reused with different metadata")
        self._records[primary_key] = record
        self._idempotency[idempotency_key] = record
        return record

    def get(self, site_id: str, invocation_id: str) -> ModelInvocationRecord | None:
        _validate_lookup_site(site_id)
        with self._lock:
            return self._records.get((site_id, invocation_id))

    def list(self, site_id: str, *, limit: int = 100) -> tuple[ModelInvocationRecord, ...]:
        _validate_lookup_site(site_id)
        _validate_limit(limit)
        with self._lock:
            records = (record for record in self._records.values() if record.site_id == site_id)
            return tuple(
                sorted(records, key=lambda item: (item.started_at, item.invocation_id))[:limit]
            )

    @contextmanager
    def record_bundle(self, site_id: str) -> Iterator[_InMemoryBundle]:
        _validate_lookup_site(site_id)
        with self._lock:
            yield _InMemoryBundle(self, site_id)


class _InMemoryBundle:
    def __init__(self, repository: InMemoryModelInvocationRepository, site_id: str) -> None:
        self._repository = repository
        self._site_id = site_id

    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord:
        if record.site_id != self._site_id:
            raise ValidationError("record site does not match transaction site")
        return self._repository._append(record)


class PostgresModelInvocationRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord:
        with self.record_bundle(record.site_id) as bundle:
            return bundle.append(record)

    def get(self, site_id: str, invocation_id: str) -> ModelInvocationRecord | None:
        _validate_lookup_site(site_id)
        _require_text(invocation_id, "invocation_id", maximum=256)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM agent_runtime.model_invocations
                WHERE site_id = %s AND invocation_id = %s
                """,
                (site_id, invocation_id),
            )
            row = cursor.fetchone()
            return None if row is None else _record_from_row(row)

    def list(self, site_id: str, *, limit: int = 100) -> tuple[ModelInvocationRecord, ...]:
        _validate_lookup_site(site_id)
        _validate_limit(limit)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_RECORD_COLUMNS}
                FROM agent_runtime.model_invocations
                WHERE site_id = %s
                ORDER BY started_at ASC, invocation_id ASC
                LIMIT %s
                """,
                (site_id, limit),
            )
            return tuple(_record_from_row(row) for row in cursor.fetchall())

    @contextmanager
    def record_bundle(self, site_id: str) -> Iterator[_PostgresBundle]:
        _validate_lookup_site(site_id)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            yield _PostgresBundle(cursor, site_id)

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))


class _PostgresBundle:
    def __init__(self, cursor: Cursor, site_id: str) -> None:
        self._cursor = cursor
        self._site_id = site_id

    def append(self, record: ModelInvocationRecord) -> ModelInvocationRecord:
        if record.site_id != self._site_id:
            raise ValidationError("record site does not match transaction site")
        self._cursor.execute(
            f"""
            INSERT INTO agent_runtime.model_invocations (
                {_RECORD_COLUMNS}
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            ON CONFLICT (site_id, idempotency_key) DO NOTHING
            RETURNING {_RECORD_COLUMNS}
            """,
            _record_params(record),
        )
        row = self._cursor.fetchone()
        if row is not None:
            return _record_from_row(row)
        self._cursor.execute(
            f"""
            SELECT {_RECORD_COLUMNS}
            FROM agent_runtime.model_invocations
            WHERE site_id = %s AND idempotency_key = %s
            """,
            (record.site_id, record.idempotency_key),
        )
        row = self._cursor.fetchone()
        if row is None:
            raise IdempotencyConflict("model invocation id was reused with different metadata")
        existing = _record_from_row(row)
        if existing != record:
            raise IdempotencyConflict(
                "model invocation idempotency key was reused with different metadata"
            )
        return existing


def _record_params(record: ModelInvocationRecord) -> tuple[Any, ...]:
    return (
        record.invocation_id,
        record.site_id,
        record.provider,
        record.requested_model,
        record.observed_model,
        record.prompt_version,
        record.output_schema_version,
        record.policy_version,
        record.tokenizer_version,
        record.request_id,
        record.response_id,
        record.started_at,
        record.completed_at,
        record.latency_ms,
        record.status,
        record.token_usage.status,
        record.token_usage.input_tokens,
        record.token_usage.output_tokens,
        record.token_usage.total_tokens,
        record.cost.status,
        record.cost.amount,
        record.cost.currency,
        record.network_call_count,
        record.tool_call_count,
        record.external_send_count,
        json.dumps(record.references.observation_event_refs),
        json.dumps(record.references.evidence_refs),
        json.dumps(record.references.tokenization_receipt_refs),
        record.idempotency_key,
        record.attempt,
        record.retry_count,
        record.finish_code,
        record.error_code,
        record.budget_status,
        record.price_catalog_version,
        record.output_digest,
    )


def _record_from_row(row: tuple[Any, ...]) -> ModelInvocationRecord:
    return ModelInvocationRecord(
        invocation_id=row[0],
        site_id=row[1],
        provider=row[2],
        requested_model=row[3],
        observed_model=row[4],
        prompt_version=row[5],
        output_schema_version=row[6],
        policy_version=row[7],
        tokenizer_version=row[8],
        request_id=row[9],
        response_id=row[10],
        started_at=row[11],
        completed_at=row[12],
        latency_ms=row[13],
        status=row[14],
        token_usage=TokenUsageMetadata(
            status=row[15],
            input_tokens=row[16],
            output_tokens=row[17],
            total_tokens=row[18],
        ),
        cost=CostMetadata(status=row[19], amount=row[20], currency=row[21]),
        network_call_count=row[22],
        tool_call_count=row[23],
        external_send_count=row[24],
        references=InvocationReferences(
            observation_event_refs=tuple(row[25]),
            evidence_refs=tuple(row[26]),
            tokenization_receipt_refs=tuple(row[27]),
        ),
        idempotency_key=row[28],
        attempt=row[29],
        retry_count=row[30],
        finish_code=row[31],
        error_code=row[32],
        budget_status=row[33],
        price_catalog_version=row[34],
        output_digest=row[35],
    )


def _validate_lookup_site(site_id: str) -> None:
    _require_text(site_id, "site_id", maximum=140)
    if _SITE_PATTERN.fullmatch(site_id) is None:
        raise ValidationError("site_id has an invalid format")


def _validate_limit(limit: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValidationError("limit must be between 1 and 1000")


__all__ = [
    "BudgetAuditStatus",
    "CostMetadata",
    "InMemoryModelInvocationRepository",
    "InvocationReferences",
    "ModelInvocationBundle",
    "ModelInvocationRecord",
    "ModelInvocationRepository",
    "PostgresModelInvocationRepository",
    "TokenUsageMetadata",
]
