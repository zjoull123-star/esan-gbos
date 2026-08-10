from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from observer.control_service import ConnectorControlResult, ConnectorStatus
from observer.identity_resolution_work import IdentityResolutionWorkSnapshot
from observer.local_pilot_api import LocalPilotAPIConfig, create_local_pilot_app
from observer.models import ConnectorKey, TenantScope
from observer.read_service import (
    CommunicationAccess,
    CommunicationDetail,
    CommunicationPage,
    CommunicationSummary,
)
from observer.runtime import LocalPilotRuntimeGuard

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
PRIVATE_IDENTITY_REF = "extid:v1:email:LV6GAKT7pm5calE6bndCH0B5zbhyjtErgQGWWEsLveI"
STATUS = ConnectorStatus(
    instance_id="sales-inbox",
    channel="email",
    status="enabled",
    checkpoint_version=4,
    backlog=0,
    last_success_at=NOW - timedelta(minutes=1),
    safe_error_code=None,
    freshness="fresh",
    revision=7,
)


class FakeControlService:
    def __init__(self) -> None:
        self.last_key: ConnectorKey | None = None

    def resolve_instance(self, *_args: object, **_kwargs: object) -> ConnectorKey:
        return ConnectorKey("email", "sales-inbox")

    def list_status(self, *_args: object, **_kwargs: object) -> tuple[ConnectorStatus, ...]:
        return (STATUS,)

    def pause(self, _scope: object, key: ConnectorKey, **_kwargs: object) -> ConnectorControlResult:
        self.last_key = key
        return ConnectorControlResult(
            status=ConnectorStatus(**{**STATUS.as_dict(), "status": "paused", "revision": 8}),
            replayed_count=0,
            replayed=False,
        )

    resume = pause
    replay = pause


class FakeReadService:
    def __init__(self) -> None:
        self.access: CommunicationAccess | None = None

    def list_communications(
        self,
        _scope: object,
        access: CommunicationAccess,
        **_kwargs: object,
    ) -> CommunicationPage:
        self.access = access
        return CommunicationPage(communications=(), next_cursor=None)

    def get_communication(
        self,
        _scope: object,
        access: CommunicationAccess,
        **_kwargs: object,
    ) -> CommunicationDetail:
        self.access = access
        return CommunicationDetail(
            summary=CommunicationSummary(
                observation_id="event-001",
                channel="email",
                occurred_at=NOW,
                summary_zh="客户询问交期",
                original_language="zh-CN",
                classification="Restricted",
                review_status="unreviewed",
                team_ref="team-sales",
                party_ref="party-001",
                evidence_count=1,
            ),
            evidence=({"ref": "evidence-001", "locator": "message"},),
            fact_proposals=(),
            association_suggestions=(),
            model={"name": "deepseek-v4-flash", "version": "2026-08-08"},
            original_text=None,
        )


class FakeIdentityResolutionMetrics:
    def __init__(
        self,
        snapshot: IdentityResolutionWorkSnapshot | Exception,
    ) -> None:
        self.value = snapshot
        self.calls: list[tuple[TenantScope, datetime, timedelta]] = []

    def snapshot(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        readiness_window: timedelta,
    ) -> IdentityResolutionWorkSnapshot:
        self.calls.append((scope, now, readiness_window))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeIdentityAuthorityDenials:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_authority_denial(self, scope: TenantScope, **values: Any) -> Any:
        self.calls.append({"scope": scope, **values})
        return SimpleNamespace(
            mapping_ref=values["mapping_ref"],
            deny_through_revision=values["deny_through_revision"],
        )


def _metrics_snapshot(
    *,
    ready: bool = True,
    heartbeat: datetime | None = NOW - timedelta(seconds=4),
    oldest_age: int | None = 17,
) -> IdentityResolutionWorkSnapshot:
    return IdentityResolutionWorkSnapshot(
        ready=ready,
        worker_last_heartbeat_at=heartbeat,
        backlog_count=3,
        oldest_backlog_age_seconds=oldest_age,
        unresolved_count=2,
        conflict_count=1,
        request_outcomes={
            "confirmed": 5,
            "unresolved": 7,
            "revoked": 2,
            "conflict": 1,
            "error": 4,
        },
        latency_buckets={
            "le_100_ms": 2,
            "le_500_ms": 3,
            "le_2000_ms": 4,
            "gt_2000_ms": 1,
        },
    )


def _config(
    *,
    bind_host: str = "127.0.0.1",
    network_mode: str = "loopback",
    max_request_bytes: int = 262_144,
) -> LocalPilotAPIConfig:
    return LocalPilotAPIConfig(
        bind_host=bind_host,
        network_mode=network_mode,
        bearer_token="synthetic-local-token",
        auth_ref="observer-token-v1",
        max_request_bytes=max_request_bytes,
    )


def _headers(
    *,
    purpose: str,
    request_id: str = "req-001",
    **overrides: str,
) -> dict[str, str]:
    values = {
        "Authorization": "Bearer synthetic-local-token",
        "X-GBOS-Local-Auth-Ref": "observer-token-v1",
        "X-Site-ID": "alpha.example",
        "X-Processing-Purpose": purpose,
        "X-Request-ID": request_id,
    }
    values.update(overrides)
    return values


def _app(
    *,
    config: LocalPilotAPIConfig | None = None,
    metrics: FakeIdentityResolutionMetrics | None = None,
    clock: Callable[[], datetime] | None = None,
    enabled: bool = True,
    denials: FakeIdentityAuthorityDenials | None = None,
) -> tuple[FastAPI, FakeControlService, FakeReadService]:
    control = FakeControlService()
    reader = FakeReadService()
    app = create_local_pilot_app(
        config=config or _config(),
        control=control,
        reader=reader,
        guard=LocalPilotRuntimeGuard(enabled=enabled, kill_switch=not enabled),
        clock=clock or (lambda: NOW),
        identity_resolution_metrics=metrics,
        identity_authority_denials=denials,
    )
    return app, control, reader


def test_identity_authority_denial_is_authenticated_idempotent_and_redacted() -> None:
    denials = FakeIdentityAuthorityDenials()
    app, _control, _reader = _app(denials=denials)
    client = TestClient(app)
    payload = {
        "identity_provider": "email",
        "external_subject_ref": PRIVATE_IDENTITY_REF,
        "mapping_ref": "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "team_ref": "TEM-SALES",
        "deny_through_revision": 4,
        "reason": "revoked",
        "idempotency_key": "identity-authority-deny-0001",
    }

    unauthenticated = client.post(
        "/internal/v1/identity-authority/deny",
        json=payload,
    )
    mismatch = client.post(
        "/internal/v1/identity-authority/deny",
        headers=_headers(
            purpose="identity_authority",
            **{"Idempotency-Key": "identity-authority-deny-other"},
        ),
        json=payload,
    )
    accepted = client.post(
        "/internal/v1/identity-authority/deny",
        headers=_headers(
            purpose="identity_authority",
            **{"Idempotency-Key": payload["idempotency_key"]},
        ),
        json=payload,
    )

    assert unauthenticated.status_code == 401
    assert mismatch.status_code == 409
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["data"] == {
        "denial": {
            "mapping_ref": payload["mapping_ref"],
            "deny_through_revision": 4,
            "status": "denied",
        }
    }
    assert "opaque-private-sentinel" not in accepted.text
    assert denials.calls == [
        {
            "scope": TenantScope("alpha.example", "observation_processing"),
            "identity_provider": "email",
            "identity_ref": payload["external_subject_ref"],
            "mapping_ref": payload["mapping_ref"],
            "team_ref": "TEM-SALES",
            "deny_through_revision": 4,
            "reason": "revoked",
            "denied_at": NOW,
            "idempotency_key": payload["idempotency_key"],
        }
    ]


def test_identity_resolution_metrics_are_authenticated_db_snapshot_prometheus_text() -> None:
    metrics = FakeIdentityResolutionMetrics(_metrics_snapshot())
    app, _control, _reader = _app(metrics=metrics)
    client = TestClient(app)

    unauthenticated = client.get("/internal/v1/metrics/identity-resolution")
    response = client.get(
        "/internal/v1/metrics/identity-resolution",
        headers=_headers(purpose="identity_resolution_metrics", request_id="metrics-001"),
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    assert metrics.calls == [
        (
            TenantScope("alpha.example", "observation_processing"),
            NOW,
            timedelta(seconds=30),
        )
    ]
    assert response.text.splitlines() == [
        "gbos_identity_resolver_ready 1",
        "gbos_identity_resolver_heartbeat_age_seconds 4",
        "gbos_identity_resolver_backlog 3",
        "gbos_identity_resolver_oldest_work_age_seconds 17",
        "gbos_identity_resolver_unresolved 2",
        "gbos_identity_resolver_conflicts 1",
        'gbos_identity_resolver_requests_total{outcome="confirmed"} 5',
        'gbos_identity_resolver_requests_total{outcome="unresolved"} 7',
        'gbos_identity_resolver_requests_total{outcome="revoked"} 2',
        'gbos_identity_resolver_requests_total{outcome="conflict"} 1',
        'gbos_identity_resolver_requests_total{outcome="error"} 4',
        'gbos_identity_resolver_request_duration_seconds_bucket{le="0.1"} 2',
        'gbos_identity_resolver_request_duration_seconds_bucket{le="0.5"} 5',
        'gbos_identity_resolver_request_duration_seconds_bucket{le="2"} 9',
        'gbos_identity_resolver_request_duration_seconds_bucket{le="+Inf"} 10',
        "gbos_identity_resolver_request_duration_seconds_count 10",
    ]


def test_identity_metrics_use_only_fixed_low_cardinality_labels_and_no_scope_values() -> None:
    metrics = FakeIdentityResolutionMetrics(_metrics_snapshot())
    app, _control, _reader = _app(metrics=metrics)

    response = TestClient(app).get(
        "/internal/v1/metrics/identity-resolution",
        headers=_headers(purpose="identity_resolution_metrics"),
    )

    assert response.status_code == 200
    labels = re.findall(r"\{([^}]*)\}", response.text)
    assert all(label.startswith('outcome="') or label.startswith('le="') for label in labels)
    for forbidden in (
        "alpha.example",
        "team-sales",
        PRIVATE_IDENTITY_REF,
        "member@example.invalid",
        "account-owner-private",
        "provider=",
        "site=",
        "team=",
        "target=",
        "account=",
    ):
        assert forbidden not in response.text


@pytest.mark.parametrize(
    "snapshot",
    (
        _metrics_snapshot(ready=True, heartbeat=NOW - timedelta(seconds=31)),
        _metrics_snapshot(ready=True, heartbeat=None, oldest_age=None),
    ),
)
def test_stale_or_absent_identity_heartbeat_is_not_ready_and_missing_ages_are_safe(
    snapshot: IdentityResolutionWorkSnapshot,
) -> None:
    app, _control, _reader = _app(metrics=FakeIdentityResolutionMetrics(snapshot))

    response = TestClient(app).get(
        "/internal/v1/metrics/identity-resolution",
        headers=_headers(purpose="identity_resolution_metrics"),
    )

    assert response.status_code == 200
    assert "gbos_identity_resolver_ready 0\n" in response.text
    if snapshot.worker_last_heartbeat_at is None:
        assert "gbos_identity_resolver_heartbeat_age_seconds NaN\n" in response.text
        assert "gbos_identity_resolver_oldest_work_age_seconds NaN\n" in response.text


def test_identity_metrics_fail_closed_for_missing_dependency_purpose_clock_or_repo_error() -> None:
    missing, _control, _reader = _app()
    broken, _control, _reader = _app(
        metrics=FakeIdentityResolutionMetrics(RuntimeError("private-db-error-sentinel"))
    )
    naive, _control, _reader = _app(
        metrics=FakeIdentityResolutionMetrics(_metrics_snapshot()),
        clock=lambda: datetime(2026, 8, 8, 9),
    )
    disabled, _control, _reader = _app(
        metrics=FakeIdentityResolutionMetrics(_metrics_snapshot()),
        enabled=False,
    )
    headers = _headers(purpose="identity_resolution_metrics")

    missing_response = TestClient(missing).get(
        "/internal/v1/metrics/identity-resolution", headers=headers
    )
    broken_response = TestClient(broken).get(
        "/internal/v1/metrics/identity-resolution", headers=headers
    )
    naive_response = TestClient(naive).get(
        "/internal/v1/metrics/identity-resolution", headers=headers
    )
    disabled_response = TestClient(disabled).get(
        "/internal/v1/metrics/identity-resolution", headers=headers
    )
    wrong_purpose = TestClient(broken).get(
        "/internal/v1/metrics/identity-resolution",
        headers=_headers(purpose="communication_projection"),
    )

    assert missing_response.status_code == 503
    assert broken_response.status_code == 503
    assert naive_response.status_code == 503
    assert disabled_response.status_code == 503
    assert wrong_purpose.status_code == 403
    assert "private-db-error-sentinel" not in (
        missing_response.text + broken_response.text + naive_response.text
    )


def test_fastapi_bff_surface_matches_frappe_v4_and_is_no_store() -> None:
    app, _control, _reader = _app()
    client = TestClient(app)

    health = client.get("/health")
    response = client.post(
        "/internal/v1/bff/connectors/list",
        headers=_headers(purpose="connector_status"),
        json={},
    )

    assert health.json()["status"] == "ok"
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["site_id"] == "alpha.example"
    assert response.json()["data"]["connectors"][0]["instance_id"] == "sales-inbox"


@pytest.mark.parametrize(
    "overrides",
    (
        {"Authorization": "Bearer wrong"},
        {"X-GBOS-Local-Auth-Ref": "wrong-ref"},
        {"X-Site-ID": ""},
        {"X-Processing-Purpose": "communication_projection"},
        {"X-Request-ID": ""},
    ),
)
def test_bff_surface_rejects_missing_or_mismatched_governed_headers(
    overrides: dict[str, str],
) -> None:
    app, _control, _reader = _app()
    response = TestClient(app).post(
        "/internal/v1/bff/connectors/list",
        headers=_headers(purpose="connector_status", **overrides),
        json={},
    )

    assert response.status_code in {401, 403, 422}


def test_bff_command_resolves_unique_instance_and_matches_idempotency_header() -> None:
    app, control, _reader = _app()
    client = TestClient(app)
    payload: dict[str, Any] = {
        "instance_id": "sales-inbox",
        "expected_revision": 7,
        "idempotency_key": "pause-0001",
    }

    missing = client.post(
        "/internal/v1/bff/connectors/pause",
        headers=_headers(purpose="connector_control"),
        json=payload,
    )
    mismatch = client.post(
        "/internal/v1/bff/connectors/pause",
        headers=_headers(
            purpose="connector_control",
            **{"Idempotency-Key": "pause-other"},
        ),
        json=payload,
    )
    valid = client.post(
        "/internal/v1/bff/connectors/pause",
        headers=_headers(
            purpose="connector_control",
            **{"Idempotency-Key": "pause-0001"},
        ),
        json=payload,
    )

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert valid.status_code == 200
    assert valid.json()["data"]["connector"]["status"] == "paused"
    assert control.last_key == ConnectorKey("email", "sales-inbox")


def test_bff_communication_scope_comes_from_authenticated_body_not_headers() -> None:
    app, _control, reader = _app()
    response = TestClient(app).post(
        "/internal/v1/bff/communications/list",
        headers=_headers(purpose="communication_projection"),
        json={
            "actor_ref": "sales@example.com",
            "allowed_team_refs": ["team-sales"],
            "scope": "team_and_self",
            "include_raw": False,
            "page_size": 25,
        },
    )

    assert response.status_code == 200
    assert reader.access is not None
    assert reader.access.actor_ref == "sales@example.com"
    assert reader.access.team_refs == frozenset({"team-sales"})
    assert reader.access.allow_all_teams is False


def test_bff_communication_get_matches_closed_frappe_detail_shape() -> None:
    app, _control, _reader = _app()
    response = TestClient(app).post(
        "/internal/v1/bff/communications/get",
        headers=_headers(purpose="communication_projection"),
        json={
            "actor_ref": "admin@example.com",
            "allowed_team_refs": ["*"],
            "scope": "all_business_projection",
            "include_raw": False,
            "observation_id": "event-001",
        },
    )

    detail = response.json()["data"]["communication"]
    assert response.status_code == 200
    assert set(detail) == {
        "observation_id",
        "channel",
        "occurred_at",
        "summary_zh",
        "original_language",
        "classification",
        "review_status",
        "team_ref",
        "party_ref",
        "evidence_count",
        "evidence",
        "fact_proposals",
        "association_suggestions",
        "participant_identities",
        "connector_account_user_ref",
        "model",
        "raw_access_allowed",
    }
    assert detail["raw_access_allowed"] is False
    assert detail["participant_identities"] == []
    assert detail["connector_account_user_ref"] is None
    assert "original_text" not in detail


def test_bff_replay_accepts_only_the_frozen_eligible_delivery_scope() -> None:
    app, _control, _reader = _app()
    client = TestClient(app)
    headers = _headers(
        purpose="connector_control",
        **{"Idempotency-Key": "replay-0001"},
    )
    payload: dict[str, Any] = {
        "instance_id": "sales-inbox",
        "expected_revision": 7,
        "idempotency_key": "replay-0001",
        "delivery_scope": "eligible_failed_deliveries",
        "limit": 100,
        "requires": [
            "within_connector_replay_window",
            "not_retention_expired",
            "same_site_and_instance",
        ],
    }

    accepted = client.post(
        "/internal/v1/bff/connectors/replay",
        headers=headers,
        json=payload,
    )
    rejected = client.post(
        "/internal/v1/bff/connectors/replay",
        headers=headers,
        json={**payload, "requires": list(reversed(payload["requires"]))},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_bff_payload_is_bounded_and_extra_fields_are_rejected() -> None:
    app, _control, _reader = _app(config=_config(max_request_bytes=256))
    client = TestClient(app)
    headers = _headers(purpose="connector_status")

    extra = client.post(
        "/internal/v1/bff/connectors/list",
        headers=headers,
        json={"unexpected": True},
    )
    oversized = client.post(
        "/internal/v1/bff/connectors/list",
        headers=headers,
        content=json.dumps({"channel": "x" * 300}),
    )

    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "invalid_query"
    assert oversized.status_code == 413


def test_bind_mode_is_explicit_for_loopback_internal_network_or_unix_socket() -> None:
    with pytest.raises(ValueError, match="loopback"):
        _config(bind_host="0.0.0.0")

    internal = _config(bind_host="0.0.0.0", network_mode="internal_network")
    unix = _config(bind_host="/run/gbos/observer.sock", network_mode="unix_socket")

    assert internal.network_mode == "internal_network"
    assert unix.network_mode == "unix_socket"
