from __future__ import annotations

from typing import Any

import pytest
from esan_gbos.api.v4.client import (
    LocalServiceClient,
    LocalServiceError,
    read_bounded_json,
)
from esan_gbos.domain.v4_dto import (
    V4DTOValidationError,
    map_communication_detail,
    map_model_usage,
    validate_connector_command,
    validate_period,
)


class RecordingTransport:
    def __init__(
        self,
        response: dict[str, Any] | None = None,
        *,
        status: int = 200,
    ) -> None:
        self.response = response or {"data": {"ok": True}}
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.status, self.response


class ByteResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int) -> bytes:
        return self.payload[:size]


def test_connector_command_is_closed_and_requires_revision_and_strong_key() -> None:
    assert validate_connector_command(
        {
            "instance_id": "wecom:sales",
            "expected_revision": 3,
            "idempotency_key": "pause-connector-001",
        }
    ) == {
        "instance_id": "wecom:sales",
        "expected_revision": 3,
        "idempotency_key": "pause-connector-001",
    }

    with pytest.raises(V4DTOValidationError, match="unexpected"):
        validate_connector_command(
            {
                "instance_id": "wecom:sales",
                "expected_revision": 3,
                "idempotency_key": "pause-connector-001",
                "token": "must-not-pass",
            }
        )
    with pytest.raises(V4DTOValidationError, match="idempotency_key"):
        validate_connector_command(
            {
                "instance_id": "wecom:sales",
                "expected_revision": 3,
                "idempotency_key": "short",
            }
        )


def test_restricted_communication_never_maps_original_text() -> None:
    value = map_communication_detail(
        {
            "observation_id": "OBS-001",
            "channel": "wecom",
            "occurred_at": "2026-08-08T01:00:00+00:00",
            "summary_zh": "客户询问交期。",
            "original_language": "zh",
            "classification": "Restricted",
            "review_status": "Pending",
            "team_ref": "TEM-001",
            "party_ref": "PTY-001",
            "evidence_count": 1,
            "evidence": [{"ref": "EVD-001", "locator": "context://EVD-001"}],
            "fact_proposals": [],
            "association_suggestions": [],
            "model": {"name": "deepseek-v4-flash", "version": "2026-08-01"},
            "raw_access_allowed": True,
            "original_text": "restricted source body",
        }
    )

    assert value["raw_access_allowed"] is False
    assert "original_text" not in value
    assert "restricted source body" not in repr(value)


def test_model_usage_maps_only_the_frozen_budget_units() -> None:
    value = map_model_usage(
        {
            "model": "deepseek-v4-flash",
            "period": "2026-08",
            "tokens": 1200,
            "token_state": "known",
            "cost": {"currency": "USD", "amount": 1.25, "state": "known"},
            "soft_limit_usd": 100.0,
            "hard_limit_usd": 150.0,
            "state": "normal",
        }
    )

    assert value["soft_limit_usd"] == 100.0
    assert value["hard_limit_usd"] == 150.0
    assert value["token_state"] == "known"
    assert set(value) == {
        "model",
        "period",
        "tokens",
        "token_state",
        "cost",
        "soft_limit_usd",
        "hard_limit_usd",
        "state",
    }


@pytest.mark.parametrize(
    "base_url",
    (
        "https://127.0.0.1:8091",
        "http://192.0.2.1:8091",
        "http://user:secret@127.0.0.1:8091",
        "http://127.0.0.1:8091/path",
    ),
)
def test_local_service_client_rejects_non_loopback_or_credentialed_urls(
    base_url: str,
) -> None:
    with pytest.raises(LocalServiceError, match="loopback|URL"):
        LocalServiceClient(
            service_name="Observer",
            base_url=base_url,
            token="local-token",
            auth_ref="observer-token-v1",
            transport=RecordingTransport(),
        )


def test_local_service_client_sends_governed_scope_without_secret_in_payload() -> None:
    transport = RecordingTransport()
    client = LocalServiceClient(
        service_name="Observer",
        base_url="http://127.0.0.1:8091",
        token="local-token",
        auth_ref="observer-token-v1",
        transport=transport,
        timeout_seconds=2.5,
    )

    result = client.request(
        method="POST",
        path="/internal/v1/bff/connectors/pause",
        site_id="gbos.localhost",
        purpose="connector_control",
        request_id="REQ-local-001",
        payload={"instance_id": "wecom:sales"},
        idempotency_key="pause-connector-001",
    )

    assert result == {"data": {"ok": True}}
    call = transport.calls[0]
    assert call["headers"] == {
        "Authorization": "Bearer local-token",
        "X-GBOS-Local-Auth-Ref": "observer-token-v1",
        "X-Site-ID": "gbos.localhost",
        "X-Processing-Purpose": "connector_control",
        "X-Request-ID": "REQ-local-001",
        "Idempotency-Key": "pause-connector-001",
        "Content-Type": "application/json",
    }
    assert call["payload"] == {"instance_id": "wecom:sales"}
    assert "local-token" not in repr(call["payload"])
    assert call["timeout_seconds"] == 2.5


def test_bounded_json_rejects_oversize_and_non_object_responses() -> None:
    with pytest.raises(LocalServiceError, match="size budget"):
        read_bounded_json(ByteResponse(b"{" + b"x" * 128 + b"}"), max_response_bytes=64)
    with pytest.raises(LocalServiceError, match="JSON object"):
        read_bounded_json(ByteResponse(b"[]"), max_response_bytes=64)


def test_local_service_client_preserves_only_safe_downstream_error_code() -> None:
    client = LocalServiceClient(
        service_name="Observer",
        base_url="http://127.0.0.1:8091",
        token="local-token",
        auth_ref="observer-token-v1",
        transport=RecordingTransport(
            response={"error": {"code": "idempotency_conflict", "raw": "do not surface"}},
            status=409,
        ),
    )

    with pytest.raises(LocalServiceError) as raised:
        client.request(
            method="POST",
            path="/internal/v1/bff/connectors/pause",
            site_id="gbos.localhost",
            purpose="connector_control",
            request_id="REQ-local-001",
            payload={"instance_id": "wecom:sales"},
        )

    assert raised.value.status == 409
    assert raised.value.error_code == "idempotency_conflict"
    assert "do not surface" not in str(raised.value)


@pytest.mark.parametrize("value", ("2026-08", "2024-02"))
def test_model_period_accepts_only_a_real_calendar_month(value: str) -> None:
    assert validate_period(value) == value


@pytest.mark.parametrize("value", ("2026-8", "2026-13", "all", ""))
def test_model_period_rejects_unbounded_queries(value: str) -> None:
    with pytest.raises(V4DTOValidationError, match="period"):
        validate_period(value)
