from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Mapping
from typing import Any, cast

import pytest

from services.kingdee_adapter import (
    AdapterStatus,
    AuthContext,
    FrozenKingdeePolicy,
    KingdeeAdapter,
    LiveEntryGates,
    LiveTransport,
    QueryPlan,
    RequestRejected,
    VerificationStatus,
)
from services.kingdee_adapter.audit import InMemoryAuditSink
from services.kingdee_adapter.transport import (
    LiveDestination,
    SyntheticTransport,
    TransportResult,
)


def test_synthetic_transport_is_deterministic_and_uses_internal_plan(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    first = adapter.invoke("kingdee.material.get", material_request, auth=auth)
    second = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert first == second
    assert first.status is AdapterStatus.AVAILABLE
    assert first.synthetic is True
    assert len(first.rows) == 2
    assert set(first.rows[0]["values"]) == {
        "material_number",
        "material_name",
        "document_status",
    }
    assert first.controls.network_calls == 0
    assert first.controls.writer_tools_discovered == 0
    assert first.controls.mutation_attempts == 0
    assert first.controls.synthetic_fallbacks == 0


def test_metadata_and_business_verification_statuses_are_distinct(
    adapter: KingdeeAdapter,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    metadata = adapter.invoke("metadata.get", material_request, auth=auth)
    business = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert metadata.verification.startup is VerificationStatus.VERIFIED
    assert metadata.verification.authentication is VerificationStatus.VERIFIED
    assert metadata.verification.metadata is VerificationStatus.VERIFIED
    assert metadata.verification.business is VerificationStatus.NOT_ATTEMPTED
    assert business.verification.startup is VerificationStatus.VERIFIED
    assert business.verification.authentication is VerificationStatus.VERIFIED
    assert business.verification.metadata is VerificationStatus.VERIFIED
    assert business.verification.business is VerificationStatus.VERIFIED


def test_synthetic_transport_has_no_network_process_or_environment_dependency(
    monkeypatch: pytest.MonkeyPatch,
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("synthetic transport attempted an external capability")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)

    adapter = KingdeeAdapter(policy=policy, transport=SyntheticTransport())
    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert result.status is AdapterStatus.AVAILABLE
    assert result.controls.network_calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://kingdee.example.internal/mcp",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/mcp",
        "https://kingdee.example.internal:8443/mcp",
        "https://user:password@kingdee.example.internal/mcp",
        "https://kingdee.example.internal/mcp?target=http://127.0.0.1",
        "https://kingdee.example.internal/mcp#fragment",
    ],
)
def test_live_destination_rejects_ssrf_and_credential_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        LiveDestination(
            url=url,
            allowlisted_urls=frozenset({url}),
        )


def test_live_destination_requires_an_exact_allowlist_match() -> None:
    url = "https://kingdee.example.internal/mcp"

    destination = LiveDestination(url=url, allowlisted_urls=frozenset({url}))
    assert destination.url == url

    with pytest.raises(ValueError, match="allowlisted"):
        LiveDestination(
            url=url,
            allowlisted_urls=frozenset({"https://other.example.internal/mcp"}),
        )


def enabled_gates() -> LiveEntryGates:
    return LiveEntryGates(
        enabled=True,
        gate=5,
        runtime_mode="live",
        network_allowed=True,
        credentials_available=True,
        metadata_verified=True,
        business_reads_enabled=True,
    )


def test_live_entry_gates_require_exact_runtime_types() -> None:
    with pytest.raises(ValueError, match="boolean"):
        LiveEntryGates(
            enabled=cast(Any, "true"),
            gate=5,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        )
    with pytest.raises(ValueError, match="integer"):
        LiveEntryGates(
            enabled=True,
            gate=cast(Any, True),
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        )


def live_destination() -> LiveDestination:
    url = "https://kingdee.example.internal/mcp"
    return LiveDestination(url=url, allowlisted_urls=frozenset({url}))


@pytest.mark.parametrize(
    "gates",
    [
        LiveEntryGates(
            enabled=False,
            gate=5,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=4,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=5,
            runtime_mode="synthetic",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=5,
            runtime_mode="live",
            network_allowed=False,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=5,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=False,
            metadata_verified=True,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=5,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=False,
            business_reads_enabled=True,
        ),
        LiveEntryGates(
            enabled=True,
            gate=5,
            runtime_mode="live",
            network_allowed=True,
            credentials_available=True,
            metadata_verified=True,
            business_reads_enabled=False,
        ),
    ],
)
def test_live_transport_is_disabled_until_every_entry_gate_is_explicit(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
    gates: LiveEntryGates,
) -> None:
    calls = 0

    def backend(*_args: object, **_kwargs: object) -> TransportResult:
        nonlocal calls
        calls += 1
        return TransportResult.available(rows=(), metadata={})

    adapter = KingdeeAdapter(
        policy=policy,
        transport=LiveTransport(
            destination=live_destination(),
            gates=gates,
            backend=backend,
        ),
    )

    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert result.status is AdapterStatus.UNAVAILABLE
    assert result.verification.startup is VerificationStatus.UNAVAILABLE
    assert calls == 0
    assert result.controls.synthetic_fallbacks == 0


def test_live_failure_is_unavailable_and_never_falls_back_to_synthetic(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    def failing_backend(*_args: object, **_kwargs: object) -> TransportResult:
        raise TimeoutError("upstream timeout with token=secret-value")

    adapter = KingdeeAdapter(
        policy=policy,
        transport=LiveTransport(
            destination=live_destination(),
            gates=enabled_gates(),
            backend=failing_backend,
        ),
    )

    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert result.status is AdapterStatus.UNAVAILABLE
    assert result.synthetic is False
    assert result.rows == ()
    assert result.reason_code == "live_transport_unavailable"
    assert result.controls.synthetic_fallbacks == 0
    assert result.verification.startup is VerificationStatus.VERIFIED
    assert result.verification.authentication is VerificationStatus.VERIFIED
    assert result.verification.metadata is VerificationStatus.VERIFIED
    assert result.verification.business is VerificationStatus.UNAVAILABLE
    assert "secret-value" not in json.dumps(result.to_dict())


def test_live_backend_receives_only_validated_internal_query_plan(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    observed: dict[str, object] = {}

    def backend(*, destination: LiveDestination, plan: object, request: object) -> TransportResult:
        observed.update(destination=destination, plan=plan, request=request)
        return TransportResult.available(
            rows=(
                {
                    "record_ref": "material-live-0001",
                    "values": {
                        "material_number": "MAT-0001",
                        "material_name": "Allowed",
                        "document_status": "C",
                    },
                },
            ),
            metadata={"source": "live"},
            network_calls=1,
        )

    adapter = KingdeeAdapter(
        policy=policy,
        transport=LiveTransport(
            destination=live_destination(),
            gates=enabled_gates(),
            backend=backend,
        ),
    )
    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    plan = observed["plan"]
    assert isinstance(plan, QueryPlan)
    assert plan.fields == (
        "material_number",
        "material_name",
        "document_status",
    )
    assert plan.filters == ()
    assert plan.order == (("material_number", "asc"),)
    assert result.status is AdapterStatus.AVAILABLE
    assert result.controls.network_calls == 1


@pytest.mark.parametrize(
    "rows",
    [
        (
            {
                "record_ref": "material-live-0001",
                "values": {
                    "material_number": "MAT-0001",
                    "material_name": "Allowed",
                    "document_status": "C",
                    "access_token": "must-not-escape",
                },
            },
        ),
        tuple(
            {
                "record_ref": f"material-live-{index:04d}",
                "values": {
                    "material_number": f"MAT-{index:04d}",
                    "material_name": "Allowed",
                    "document_status": "C",
                },
            }
            for index in range(3)
        ),
    ],
)
def test_live_result_extra_fields_and_over_budget_rows_fail_closed(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
    rows: tuple[Mapping[str, Any], ...],
) -> None:
    def backend(*_args: object, **_kwargs: object) -> TransportResult:
        return TransportResult.available(
            rows=rows,
            metadata={"access_token": "must-not-escape"},
            network_calls=1,
        )

    adapter = KingdeeAdapter(
        policy=policy,
        transport=LiveTransport(
            destination=live_destination(),
            gates=enabled_gates(),
            backend=backend,
        ),
    )

    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert result.status is AdapterStatus.UNAVAILABLE
    assert result.rows == ()
    assert result.metadata == {}
    assert result.reason_code == "transport_result_rejected"
    assert "must-not-escape" not in json.dumps(result.to_dict())


def test_audit_is_structured_redacted_and_contains_no_row_bodies_or_secrets(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    audit = InMemoryAuditSink()
    adapter = KingdeeAdapter(
        policy=policy,
        transport=SyntheticTransport(),
        audit_sink=audit,
    )
    result = adapter.invoke("kingdee.material.get", material_request, auth=auth)

    assert result.status is AdapterStatus.AVAILABLE
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.request_id == material_request["request_id"]
    assert event.tool_name == "kingdee.material.get"
    assert event.returned_rows == 2
    assert not hasattr(event, "rows")
    encoded = json.dumps(event.to_dict(), sort_keys=True)
    assert "material_name" not in encoded
    assert "Synthetic material" not in encoded
    assert "token" not in encoded.lower()
    assert "secret" not in encoded.lower()
    assert "password" not in encoded.lower()
    assert "account-set-synthetic-gate5" not in encoded


def test_rejected_requests_are_audited_without_echoing_unvalidated_values(
    policy: FrozenKingdeePolicy,
    auth: AuthContext,
    material_request: Mapping[str, Any],
) -> None:
    audit = InMemoryAuditSink()
    adapter = KingdeeAdapter(
        policy=policy,
        transport=SyntheticTransport(),
        audit_sink=audit,
    )
    unsafe = dict(material_request)
    unsafe["token"] = "full-secret-value"
    unsafe["request_id"] = "token-full-secret-value"

    with pytest.raises(RequestRejected):
        adapter.invoke("kingdee.material.get", unsafe, auth=auth)

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.status == "denied"
    assert event.reason_code == "request_rejected"
    assert event.returned_rows == 0
    encoded = json.dumps(event.to_dict(), sort_keys=True)
    assert "full-secret-value" not in encoded
    assert "account-set-synthetic-gate5" not in encoded
