from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from observer.control_service import ConnectorControlResult, ConnectorStatus
from observer.local_pilot_api import LocalPilotAPIConfig, create_local_pilot_app
from observer.models import ConnectorKey
from observer.read_service import (
    CommunicationDetail,
    CommunicationPage,
    CommunicationSummary,
)
from observer.runtime import LocalPilotRuntimeGuard

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
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
        self.access: object | None = None

    def list_communications(
        self,
        _scope: object,
        access: object,
        **_kwargs: object,
    ) -> CommunicationPage:
        self.access = access
        return CommunicationPage(communications=(), next_cursor=None)

    def get_communication(
        self,
        _scope: object,
        access: object,
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
) -> tuple[object, FakeControlService, FakeReadService]:
    control = FakeControlService()
    reader = FakeReadService()
    app = create_local_pilot_app(
        config=config or _config(),
        control=control,
        reader=reader,
        guard=LocalPilotRuntimeGuard(enabled=True, kill_switch=False),
        clock=lambda: NOW,
    )
    return app, control, reader


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
    payload = {
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
        "model",
        "raw_access_allowed",
    }
    assert detail["raw_access_allowed"] is False
    assert "original_text" not in detail


def test_bff_replay_accepts_only_the_frozen_eligible_delivery_scope() -> None:
    app, _control, _reader = _app()
    client = TestClient(app)
    headers = _headers(
        purpose="connector_control",
        **{"Idempotency-Key": "replay-0001"},
    )
    payload = {
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
