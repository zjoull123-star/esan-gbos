from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.context.context_service.client import (
    ContextClientError,
    HttpContextRepository,
)
from services.context.context_service.models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    TenantScope,
)

NOW = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)


class RecordingTransport:
    def __init__(self, status: int = 200, response: dict[str, Any] | None = None) -> None:
        self.status = status
        self.response = response or {}
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.status, self.response


def _payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "fact_proposal_record_id": "fact-record-001",
        "site_id": "gbos.localhost",
        "processing_purpose": "observation_processing",
        "data_classification": "Restricted",
        "fact": {
            "status": "proposed",
            "subject_ref": "contact-001",
            "predicate": "communication_summary",
            "confidence": 1.0,
            "evidence_refs": ["evidence-record-001"],
        },
    }


def _response(payload_digest: str) -> dict[str, Any]:
    return {
        "data": {
            "kind": "fact_proposal",
            "record_id": "fact-record-001",
            "site_id": "gbos.localhost",
            "processing_purpose": "observation_processing",
            "idempotency_key": "context-publish:fact-record-001",
            "payload_digest": payload_digest,
            "recorded_at": NOW.isoformat(),
        },
        "meta": {
            "request_id": "context-client-request",
            "schema_version": "1.0",
            "runtime": "gate3-local",
        },
    }


def test_http_context_repository_posts_governed_record_to_loopback_only() -> None:
    envelope = GovernedEnvelope.from_payload(
        site_id="gbos.localhost",
        processing_purpose="observation_processing",
        idempotency_key="context-publish:fact-record-001",
        payload=_payload(),
    )
    transport = RecordingTransport(response=_response(envelope.payload_digest))
    repository = HttpContextRepository(
        base_url="http://127.0.0.1:8092",
        token="synthetic-context-token",
        transport=transport,
        timeout_seconds=2.5,
    )

    metadata = repository.save(
        TenantScope("gbos.localhost", "observation_processing"),
        RecordKind.FACT_PROPOSAL,
        envelope,
    )

    assert metadata.record_id == "fact-record-001"
    assert metadata.payload_digest == envelope.payload_digest
    assert transport.calls == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:8092/internal/v1/context/fact-proposals",
            "headers": {
                "Authorization": "Bearer synthetic-context-token",
                "X-Site-ID": "gbos.localhost",
                "X-Processing-Purpose": "observation_processing",
                "X-Request-ID": (f"context-{envelope.payload_digest[:26]}"),
                "Idempotency-Key": "context-publish:fact-record-001",
                "Content-Type": "application/json",
            },
            "payload": _payload(),
            "timeout_seconds": 2.5,
        }
    ]


@pytest.mark.parametrize(
    "base_url",
    (
        "https://context.example.com",
        "http://192.0.2.1:8092",
        "http://user:secret@127.0.0.1:8092",
        "http://127.0.0.1:8092/path?token=secret",
    ),
)
def test_http_context_repository_rejects_non_loopback_or_credentialed_urls(
    base_url: str,
) -> None:
    with pytest.raises(ContextClientError, match="loopback|URL"):
        HttpContextRepository(
            base_url=base_url,
            token="synthetic-context-token",
            transport=RecordingTransport(),
        )


def test_http_context_repository_maps_conflict_and_rejects_mismatched_metadata() -> None:
    envelope = GovernedEnvelope.from_payload(
        site_id="gbos.localhost",
        processing_purpose="observation_processing",
        idempotency_key="context-publish:fact-record-001",
        payload=_payload(),
    )
    conflict = HttpContextRepository(
        base_url="http://localhost:8092",
        token="synthetic-context-token",
        transport=RecordingTransport(
            status=409,
            response={"detail": "idempotency_conflict"},
        ),
    )
    with pytest.raises(IdempotencyConflict, match="idempotency"):
        conflict.save(
            TenantScope("gbos.localhost", "observation_processing"),
            RecordKind.FACT_PROPOSAL,
            envelope,
        )

    mismatched = _response(envelope.payload_digest)
    mismatched["data"]["site_id"] = "other.localhost"
    repository = HttpContextRepository(
        base_url="http://localhost:8092",
        token="synthetic-context-token",
        transport=RecordingTransport(response=mismatched),
    )
    with pytest.raises(ContextClientError, match="metadata"):
        repository.save(
            TenantScope("gbos.localhost", "observation_processing"),
            RecordKind.FACT_PROPOSAL,
            envelope,
        )
