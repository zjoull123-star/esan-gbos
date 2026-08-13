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
from observer.email_connector_config import InMemoryEmailConnectorConfigRepository
from observer.email_participant_authority import canonical_binding_digest
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
        self.list_calls: list[tuple[TenantScope, str | None]] = []

    def resolve_instance(self, *_args: object, **_kwargs: object) -> ConnectorKey:
        return ConnectorKey("email", "sales-inbox")

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None = None,
    ) -> tuple[ConnectorStatus, ...]:
        self.list_calls.append((scope, channel))
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


class FakeEvidenceReveal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reveal(self, scope: TenantScope, **values: Any) -> dict[str, object]:
        self.calls.append({"scope": scope, **values})
        return {"content": "restricted body", "media_type": "text/plain; charset=utf-8"}


class FakeEmailDraftMaterial:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def save(self, scope: TenantScope, **values: Any) -> dict[str, object]:
        self.calls.append(("save", {"scope": scope, **values}))
        return {"evidence_ref": "EVR-DRAFT-01", "digest": values["content_digest"], "revision": 1}

    def finalize(self, scope: TenantScope, **values: Any) -> dict[str, object]:
        self.calls.append(("finalize", {"scope": scope, **values}))
        return {
            "evidence_ref": "EVR-MIME-01",
            "digest": "sha256:" + "b" * 64,
            "role_binding": "sha256:" + "c" * 64,
        }


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
        mailbox_projection_bearer_token="mailbox-projection-token",
        mailbox_projection_auth_ref="gateway-mailbox-projection-v1",
        draft_material_bearer_token="observer-draft-material-token",
        draft_material_auth_ref="observer-email-draft-material-v1",
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
    connector_configs: InMemoryEmailConnectorConfigRepository | None = None,
    evidence_reveal: FakeEvidenceReveal | None = None,
    draft_material: FakeEmailDraftMaterial | None = None,
    mailbox_identity: object | None = None,
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
        email_connector_configs=connector_configs,
        evidence_reveal=evidence_reveal,
        email_draft_material=draft_material,
        email_mailbox_identity=mailbox_identity,
    )
    return app, control, reader


class FakeEmailMailboxIdentity:
    def __init__(self) -> None:
        self.calls: list[tuple[TenantScope, str]] = []

    def derive(
        self,
        scope: TenantScope,
        *,
        canonical_mailbox_address: str,
    ) -> object:
        self.calls.append((scope, canonical_mailbox_address))

        class Result:
            def to_wire(self) -> dict[str, object]:
                return {
                    "opaque_address_ref": "extid:v1:email:" + "A" * 43,
                    "normalization_version": "email-v1",
                }

        return Result()


def _connector_projection() -> dict[str, object]:
    from services.email_gateway.models import MailboxConnectorProjection

    return MailboxConnectorProjection(
        site_id="alpha.example",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        provider_kind="imap_smtp",
        entry_role="primary",
        business_purpose="sales_follow_up",
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email-primary",
        inbound_enabled=True,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_config_revision=1,
        activation_not_before=NOW,
        projection_revision=1,
    ).to_wire()


def _connector_projection_headers(payload: dict[str, object]) -> dict[str, str]:
    return _headers(
        purpose="observation_processing",
        request_id="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        Authorization="Bearer mailbox-projection-token",
        **{
            "X-GBOS-Local-Auth-Ref": "gateway-mailbox-projection-v1",
            "X-Payload-Digest": str(payload["projection_digest"]),
        },
    )


def test_mailbox_projection_uses_distinct_auth_and_exact_replay_receipt() -> None:
    configs = InMemoryEmailConnectorConfigRepository()
    app, _control, _reader = _app(connector_configs=configs)
    client = TestClient(app)
    payload = _connector_projection()

    ordinary_auth = client.post(
        "/internal/v1/email-connectors/apply-config",
        headers=_headers(
            purpose="observation_processing",
            request_id="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            **{"X-Payload-Digest": str(payload["projection_digest"])},
        ),
        json=payload,
    )
    first = client.post(
        "/internal/v1/email-connectors/apply-config",
        headers=_connector_projection_headers(payload),
        json=payload,
    )
    replay = client.post(
        "/internal/v1/email-connectors/apply-config",
        headers=_connector_projection_headers(payload),
        json=payload,
    )

    assert ordinary_auth.status_code == 401
    assert first.status_code == replay.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == replay.json()
    assert first.json() == {
        "schema_version": "1.0",
        "receipt_ref": first.json()["receipt_ref"],
        "config_publication_ref": "MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": payload["projection_digest"],
    }
    assert len(configs.projections) == 1


def test_mailbox_projection_accepts_exact_v2_identity_ref_but_not_explicit_null() -> None:
    from services.email_gateway.models import canonical_digest

    legacy = _connector_projection()
    body = {key: value for key, value in legacy.items() if key != "projection_digest"}
    body["mailbox_address_identity_ref"] = "extid:v1:email:" + "M" * 43
    payload = {**body, "projection_digest": canonical_digest(body)}
    configs = InMemoryEmailConnectorConfigRepository()
    app, _control, _reader = _app(connector_configs=configs)
    client = TestClient(app)

    accepted = client.post(
        "/internal/v1/email-connectors/apply-config",
        headers=_connector_projection_headers(payload),
        json=payload,
    )
    explicit_null = {**payload, "mailbox_address_identity_ref": None}
    rejected = client.post(
        "/internal/v1/email-connectors/apply-config",
        headers=_connector_projection_headers(payload),
        json=explicit_null,
    )

    assert accepted.status_code == 200
    assert configs.projections[0].mailbox_address_identity_ref == ("extid:v1:email:" + "M" * 43)
    assert rejected.status_code == 422


def test_mailbox_projection_rejects_missing_repo_fake_extra_and_digest_drift() -> None:
    payload = _connector_projection()
    missing, _control, _reader = _app()
    configured, _control, _reader = _app(connector_configs=InMemoryEmailConnectorConfigRepository())
    headers = _connector_projection_headers(payload)

    missing_response = TestClient(missing).post(
        "/internal/v1/email-connectors/apply-config", headers=headers, json=payload
    )
    fake = TestClient(configured).post(
        "/internal/v1/email-connectors/apply-config",
        headers=headers,
        json={**payload, "provider_kind": "fake"},
    )
    extra = TestClient(configured).post(
        "/internal/v1/email-connectors/apply-config",
        headers=headers,
        json={**payload, "unexpected": "private@example.invalid"},
    )
    drift = TestClient(configured).post(
        "/internal/v1/email-connectors/apply-config",
        headers={**headers, "X-Payload-Digest": "sha256:" + "0" * 64},
        json=payload,
    )

    assert missing_response.status_code == 503
    assert fake.status_code == extra.status_code == drift.status_code == 422
    assert "private@example.invalid" not in extra.text


def test_email_connector_health_uses_projection_auth_and_returns_only_safe_live_state() -> None:
    app, control, _reader = _app()
    client = TestClient(app)
    headers = _headers(
        purpose="email_connector_health_read",
        request_id="email-health-001",
        Authorization="Bearer mailbox-projection-token",
        **{"X-GBOS-Local-Auth-Ref": "gateway-mailbox-projection-v1"},
    )

    ordinary = client.post(
        "/internal/v1/email-connectors/health",
        headers=_headers(purpose="email_connector_health_read"),
        json={},
    )
    wrong_purpose = client.post(
        "/internal/v1/email-connectors/health",
        headers={**headers, "X-Processing-Purpose": "observation_processing"},
        json={},
    )
    response = client.post(
        "/internal/v1/email-connectors/health",
        headers=headers,
        json={},
    )

    assert ordinary.status_code == 401
    assert wrong_purpose.status_code == 403
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "site_id": "alpha.example",
        "data": {
            "connectors": [
                {
                    "observer_connector_instance_ref": "sales-inbox",
                    "status": "enabled",
                    "freshness": "fresh",
                    "backlog": 0,
                    "last_success_at": "2026-08-08T08:59:00Z",
                    "safe_error_code": None,
                }
            ]
        },
        "meta": {
            "request_id": "email-health-001",
            "schema_version": "1.0",
        },
    }
    assert control.list_calls == [(TenantScope("alpha.example", "observation_processing"), "email")]


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


def test_evidence_reveal_is_a_second_authorized_no_store_observer_call() -> None:
    reveal = FakeEvidenceReveal()
    app, _control, _reader = _app(evidence_reveal=reveal)
    response = TestClient(app).post(
        "/internal/v1/bff/evidence/reveal",
        headers={
            **_headers(purpose="email_evidence_reveal"),
            "Authorization": "Bearer mailbox-projection-token",
            "X-GBOS-Local-Auth-Ref": "gateway-mailbox-projection-v1",
        },
        json={
            "authorization": {
                "receipt_ref": "EAR-01",
                "site_id": "alpha.example",
                "purpose": "email_evidence_reveal",
                "inbox_item_ref": "INB-01",
                "evidence_ref": "EVR-01",
                "actor_ref": "reviewer-01",
                "team_ref": "team-sales",
                "issued_at": "2026-08-08T08:59:00Z",
                "expires_at": "2026-08-08T09:01:00Z",
            }
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "content": "restricted body",
        "media_type": "text/plain; charset=utf-8",
    }
    assert len(reveal.calls) == 1


def test_email_draft_material_uses_separate_bearer_and_closed_save_finalize_shapes() -> None:
    material = FakeEmailDraftMaterial()
    app, _control, _reader = _app(draft_material=material)
    client = TestClient(app)
    headers = {
        **_headers(purpose="email_draft_material"),
        "Authorization": "Bearer observer-draft-material-token",
        "X-GBOS-Local-Auth-Ref": "observer-email-draft-material-v1",
        "Idempotency-Key": "draft-save-01",
    }
    authorization = {
        "receipt_ref": "DAR-01",
        "site_id": "alpha.example",
        "purpose": "email_draft_material",
        "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_ref": "DRF-01",
        "draft_revision": 1,
        "actor_ref": "sales-01",
        "team_ref": "team-sales",
        "request_digest": "sha256:" + "a" * 64,
        "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "message_ref": "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 1,
        "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": "sha256:" + "b" * 64,
        "participant_binding_digest": "sha256:" + "c" * 64,
        "evidence_binding_digest": "sha256:" + "d" * 64,
        "participant_roles_digest": canonical_binding_digest(
            {"sender": "mailbox_owner", "recipients": ["original_sender"]}
        ),
        "issued_at": "2026-08-08T08:59:00Z",
        "expires_at": "2026-08-08T09:01:00Z",
    }
    saved = client.post(
        "/internal/v1/bff/email-draft-material/save",
        headers=headers,
        json={
            "authorization": authorization,
            "content": "bounded draft",
            "content_digest": "sha256:" + "a" * 64,
            "idempotency_key": "draft-save-01",
        },
    )
    finalized = client.post(
        "/internal/v1/bff/email-draft-material/finalize",
        headers={**headers, "Idempotency-Key": "draft-finalize-01"},
        json={
            "authorization": authorization,
            "draft_evidence_ref": "EVR-DRAFT-01",
            "draft_digest": "sha256:" + "a" * 64,
            "draft_revision": 1,
            "participant_roles": {
                "sender": "mailbox_owner",
                "recipients": ["original_sender"],
            },
            "idempotency_key": "draft-finalize-01",
        },
    )
    raw_address = client.post(
        "/internal/v1/bff/email-draft-material/finalize",
        headers={**headers, "Idempotency-Key": "draft-finalize-02"},
        json={
            "authorization": authorization,
            "draft_evidence_ref": "EVR-DRAFT-01",
            "draft_digest": "sha256:" + "a" * 64,
            "draft_revision": 1,
            "participant_roles": {"from": "sales@example.invalid"},
            "idempotency_key": "draft-finalize-02",
        },
    )

    assert saved.status_code == finalized.status_code == 200
    assert raw_address.status_code == 422
    assert [name for name, _call in material.calls] == ["save", "finalize"]


def test_email_mailbox_identity_uses_draft_material_auth_and_exact_closed_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from observer.email_mailbox_identity import EmailMailboxIdentityService
    from observer.identity_tokens import HmacSha256IdentityTokenResolver

    identity = EmailMailboxIdentityService(
        identity_resolver=HmacSha256IdentityTokenResolver(b"m" * 32)
    )
    app, _control, _reader = _app(mailbox_identity=identity)
    client = TestClient(app)
    headers = {
        **_headers(purpose="email_mailbox_identity", request_id="mailbox-id-req-01"),
        "Authorization": "Bearer observer-draft-material-token",
        "X-GBOS-Local-Auth-Ref": "observer-email-draft-material-v1",
        "Idempotency-Key": "mailbox-identity-01",
    }

    response = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers=headers,
        json={
            "canonical_mailbox_address": "Sales.Primary@Example.COM",
            "idempotency_key": "mailbox-identity-01",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    response_payload = response.json()
    assert response_payload["site_id"] == "alpha.example"
    assert response_payload["data"] == {
        "opaque_address_ref": response_payload["data"]["opaque_address_ref"],
        "normalization_version": "email-v1",
    }
    assert response_payload["data"]["opaque_address_ref"].startswith("extid:v1:email:")
    assert response_payload["meta"] == {
        "request_id": "mailbox-id-req-01",
        "schema_version": "1.0",
    }

    wrong_purpose = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers={**headers, "X-Processing-Purpose": "email_draft_material"},
        json={
            "canonical_mailbox_address": "owner@example.com",
            "idempotency_key": "mailbox-identity-01",
        },
    )
    extra = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers=headers,
        json={
            "canonical_mailbox_address": "owner@example.com",
            "idempotency_key": "mailbox-identity-01",
            "raw_address": "must-not-be-accepted@example.com",
        },
    )
    mismatch = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers={**headers, "Idempotency-Key": "different-mailbox-id"},
        json={
            "canonical_mailbox_address": "owner@example.com",
            "idempotency_key": "mailbox-identity-01",
        },
    )
    invalid_address = "private invalid mailbox@example.invalid"
    invalid = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers=headers,
        json={
            "canonical_mailbox_address": invalid_address,
            "idempotency_key": "mailbox-identity-01",
        },
    )
    oversized_address = "oversized-private-" + "x" * 300 + "@example.invalid"
    oversized = client.post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers=headers,
        json={
            "canonical_mailbox_address": oversized_address,
            "idempotency_key": "mailbox-identity-01",
        },
    )

    assert wrong_purpose.status_code == 403
    assert extra.status_code == 422
    assert mismatch.status_code == 409
    assert invalid.status_code == oversized.status_code == 422
    exposed_values = (
        "must-not-be-accepted@example.com",
        invalid_address,
        oversized_address,
    )
    rendered_responses = "".join(
        repr(dict(response.headers)) + response.text for response in (extra, invalid, oversized)
    )
    rendered_logs = caplog.text
    assert all(value not in rendered_responses for value in exposed_values)
    assert all(value not in rendered_logs for value in exposed_values)


def test_email_mailbox_identity_fails_closed_when_service_or_auth_is_missing() -> None:
    config = LocalPilotAPIConfig(
        bind_host="127.0.0.1",
        network_mode="loopback",
        bearer_token="synthetic-local-token",
        auth_ref="observer-token-v1",
    )
    app, _control, _reader = _app(config=config)
    payload = {
        "canonical_mailbox_address": "owner@example.com",
        "idempotency_key": "mailbox-identity-01",
    }

    missing_auth = TestClient(app).post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers={
            **_headers(purpose="email_mailbox_identity"),
            "Idempotency-Key": "mailbox-identity-01",
        },
        json=payload,
    )
    assert missing_auth.status_code == 401

    configured_app, _control, _reader = _app()
    unavailable = TestClient(configured_app).post(
        "/internal/v1/bff/email-mailbox-identity/derive",
        headers={
            **_headers(purpose="email_mailbox_identity"),
            "Authorization": "Bearer observer-draft-material-token",
            "X-GBOS-Local-Auth-Ref": "observer-email-draft-material-v1",
            "Idempotency-Key": "mailbox-identity-01",
        },
        json=payload,
    )
    assert unavailable.status_code == 503
