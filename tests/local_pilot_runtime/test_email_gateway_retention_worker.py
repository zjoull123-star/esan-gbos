from __future__ import annotations

import http.client
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.email_gateway.metrics import GatewayMetrics
from services.email_gateway.terminal_retention import (
    GatewayTombstoneCallbackReceipt,
    TerminalMaterialAuthority,
)

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
SITE_ID = "alpha.example"
AUTHORITY_REF = "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV"
OBSERVER_REQUEST_REF = "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _authority() -> TerminalMaterialAuthority:
    return TerminalMaterialAuthority(
        authority_receipt_ref=AUTHORITY_REF,
        site_id=SITE_ID,
        purpose="email_draft_material",
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=4,
        material_kind="draft",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_digest="sha256:" + "1" * 64,
        terminal_state="sent",
        terminal_at=NOW,
        not_before=NOW + timedelta(days=30),
        source_authority_receipt_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + "2" * 64,
    )


def test_observer_registration_transport_is_fixed_bounded_and_closed() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import (
        HttpObserverEmailMaterialRegistration,
    )

    calls: list[dict[str, object]] = []

    def transport(**kwargs: object) -> tuple[int, dict[str, object]]:
        calls.append(kwargs)
        return 200, {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "evidence_ref": _authority().evidence_ref,
            "request_ref": OBSERVER_REQUEST_REF,
            "not_before": _authority().not_before.isoformat(),
        }

    client = HttpObserverEmailMaterialRegistration(
        endpoint="http://observer-api:8003/internal/v1/retention/email-material/register",
        bearer_token="observer-registration-token",
        auth_ref="observer-retention-verifier-v1",
        transport=transport,
    )

    response = client.register(_authority().registration_wire())

    assert response["request_ref"] == OBSERVER_REQUEST_REF
    assert calls == [
        {
            "url": "http://observer-api:8003/internal/v1/retention/email-material/register",
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer observer-registration-token",
                "Content-Type": "application/json",
                "X-GBOS-Local-Auth-Ref": "observer-retention-verifier-v1",
                "X-Processing-Purpose": "audit_compliance",
                "X-Request-ID": AUTHORITY_REF,
                "X-Site-ID": SITE_ID,
            },
            "payload": _authority().registration_wire(),
            "timeout_seconds": 3.0,
        }
    ]

    closed = HttpObserverEmailMaterialRegistration(
        endpoint="http://observer-api:8003/internal/v1/retention/email-material/register",
        bearer_token="observer-registration-token",
        auth_ref="observer-retention-verifier-v1",
        transport=lambda **_kwargs: (200, {**response, "extra": True}),
    )
    with pytest.raises(ValueError, match="registration response"):
        closed.register(_authority().registration_wire())


class _RetentionRepository:
    def record_worker_heartbeat(self, *_args: object, **_kwargs: object) -> None:
        return None

    def discover_due_projections(self, *_args: object, **_kwargs: object) -> tuple[()]:
        return ()

    def retention_health(self, *_args: object, **_kwargs: object) -> tuple[int, int]:
        return 0, 0

    def heartbeat_snapshot(self, *_args: object, **_kwargs: object) -> dict[str, datetime]:
        return {}


class _RegistrationRelay:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self, _scope: object) -> bool:
        self.calls += 1
        return True


def test_retention_cycle_runs_registration_relay_at_most_batch_size() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import RetentionCycle

    relay = _RegistrationRelay()
    cycle = RetentionCycle(
        repository=_RetentionRepository(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        metrics=GatewayMetrics(required_workers=frozenset()),
        registration_relay=relay,
        site_id=SITE_ID,
        worker_id="gateway-retention-1",
        batch_size=3,
        execute=lambda: False,
        clock=lambda: NOW,
    )

    assert cycle() == 0
    assert relay.calls == 3


class _TerminalService:
    def __init__(self) -> None:
        self.resolve_calls: list[tuple[object, str]] = []
        self.callback_calls: list[tuple[object, object]] = []

    def resolve_terminal(self, scope: object, authority_receipt_ref: str):
        self.resolve_calls.append((scope, authority_receipt_ref))
        return _authority()

    def accept_tombstone_callback(self, scope: object, *, payload: object):
        self.callback_calls.append((scope, payload))
        return GatewayTombstoneCallbackReceipt(
            callback_receipt_ref="GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            site_id=SITE_ID,
            authority_receipt_ref=AUTHORITY_REF,
            tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )


def _request(
    port: int,
    path: str,
    payload: dict[str, object],
    *,
    token: str = "gateway-retention-token",
    request_id: str,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(
        "POST",
        path,
        body=json.dumps(payload, separators=(",", ":")),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GBOS-Local-Auth-Ref": "email-gateway-retention-v1",
            "X-Processing-Purpose": "email_draft_material",
            "X-Request-ID": request_id,
            "X-Site-ID": SITE_ID,
        },
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    return response.status, body


def test_authenticated_retention_server_resolves_closed_authority_and_accepts_callback() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import MetricsServer

    service = _TerminalService()
    server = MetricsServer(
        GatewayMetrics(required_workers=frozenset()),
        port=0,
        clock=lambda: NOW,
        terminal_service=service,  # type: ignore[arg-type]
        site_id=SITE_ID,
        bearer_token="gateway-retention-token",
        auth_ref="email-gateway-retention-v1",
    )
    server.start()
    try:
        port = server.port
        request_id = "REQ-01ARZ3NDEKTSV4RRFFQ69G5FAV"
        status, body = _request(
            port,
            "/internal/v1/retention/email-material/authority/resolve",
            {
                "schema_version": "1.0",
                "site_id": SITE_ID,
                "authority_receipt_ref": AUTHORITY_REF,
                "request_id": request_id,
            },
            request_id=request_id,
        )
        assert status == 200
        assert body == {
            "schema_version": "1.0",
            "authority_receipt_ref": AUTHORITY_REF,
            "site_id": SITE_ID,
            "purpose": "email_draft_material",
            "evidence_ref": _authority().evidence_ref,
            "terminal_state": "sent",
            "terminal_at": "2026-08-14T08:00:00Z",
            "draft_ref": _authority().draft_ref,
            "draft_revision": 4,
        }

        callback = {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "purpose": "email_draft_material",
            "authority_receipt_ref": AUTHORITY_REF,
            "evidence_ref": _authority().evidence_ref,
            "observer_request_ref": OBSERVER_REQUEST_REF,
            "tombstone_receipt_ref": "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "deleted_at": "2026-09-13T08:00:00Z",
            "evidence_digest": "sha256:" + "1" * 64,
            "callback_payload_digest": "sha256:" + "3" * 64,
        }
        status, body = _request(
            port,
            "/internal/v1/retention/email-material/tombstone-callback",
            callback,
            request_id=OBSERVER_REQUEST_REF,
        )
        assert status == 200
        assert body == {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "authority_receipt_ref": AUTHORITY_REF,
            "tombstone_receipt_ref": "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "callback_receipt_ref": "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "accepted": True,
        }
        assert len(service.resolve_calls) == 1
        assert len(service.callback_calls) == 1
    finally:
        server.close()


def test_retention_server_rejects_unauthorized_or_unbound_payload_before_service() -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import MetricsServer

    service = _TerminalService()
    server = MetricsServer(
        GatewayMetrics(required_workers=frozenset()),
        port=0,
        clock=lambda: NOW,
        terminal_service=service,  # type: ignore[arg-type]
        site_id=SITE_ID,
        bearer_token="gateway-retention-token",
        auth_ref="email-gateway-retention-v1",
    )
    server.start()
    try:
        payload = {
            "schema_version": "1.0",
            "site_id": SITE_ID,
            "authority_receipt_ref": AUTHORITY_REF,
            "request_id": "REQ-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        }
        status, body = _request(
            server.port,
            "/internal/v1/retention/email-material/authority/resolve",
            payload,
            token="wrong-token",
            request_id=str(payload["request_id"]),
        )
        assert (status, body) == (401, {"error": "unauthorized"})

        status, body = _request(
            server.port,
            "/internal/v1/retention/email-material/authority/resolve?site=other",
            payload,
            request_id=str(payload["request_id"]),
        )
        assert (status, body) == (404, {"error": "not_found"})
        assert service.resolve_calls == []
        assert service.callback_calls == []
    finally:
        server.close()


def _config() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "site_id": SITE_ID,
        "external_send": False,
        "postgres": {
            "host": "postgres",
            "port": 5432,
            "database": "gbos_local_pilot",
            "user": "gbos_email_gateway_retention_worker",
            "password_file": "/run/secrets/postgres_email_gateway_retention_worker_password",
            "connect_timeout_seconds": 5,
        },
        "observer_verifier": {
            "endpoint": "http://observer-api:8003/internal/v1/retention/tombstones/verify",
            "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
            "auth_ref": "observer-retention-verifier-v1",
        },
        "gateway_retention_api": {
            "bearer_file": "/run/secrets/email_gateway_retention_bearer",
            "auth_ref": "email-gateway-retention-v1",
        },
        "observer_registration": {
            "endpoint": ("http://observer-api:8003/internal/v1/retention/email-material/register"),
            "bearer_file": "/run/secrets/observer_email_draft_material_bearer",
            "auth_ref": "observer-retention-verifier-v1",
        },
        "worker_id": "gateway-retention-1",
        "batch_size": 10,
        "interval_seconds": 60,
        "metrics_port": 9102,
        "required_workers": ["retention"],
    }


def test_runtime_config_accepts_only_closed_terminal_retention_dependencies(
    tmp_path: Path,
) -> None:
    from services.local_pilot_runtime.email_gateway_retention_worker import _load_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()))
    assert _load_config(path) == _config()

    drift = _config()
    drift["observer_registration"] = {
        **drift["observer_registration"],  # type: ignore[dict-item]
        "endpoint": "http://observer-api:8003/wrong",
    }
    path.write_text(json.dumps(drift))
    with pytest.raises(ValueError, match="config rejected"):
        _load_config(path)


def test_main_preflights_all_secret_files_before_database_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.local_pilot_runtime.email_gateway_retention_worker as worker

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mode": "local_pilot",
                "site_id": SITE_ID,
                "retention_days": 30,
                "production_go": False,
                "local_pilot_go": True,
            }
        )
    )
    secret_reads: list[Path] = []

    class _Secret:
        def reveal(self) -> str:
            return "bounded-secret"

    def load(path: Path):
        secret_reads.append(path)
        if len(secret_reads) == 3:
            raise ValueError("missing secret")
        return _Secret()

    database_calls: list[dict[str, object]] = []
    monkeypatch.setattr(worker, "load_secret_file", load)
    result = worker.main(
        environ={
            "GBOS_EMAIL_GATEWAY_RETENTION_CONFIG": str(config_path),
            "GBOS_LOCAL_PILOT_MANIFEST": str(manifest_path),
        },
        connector=lambda **kwargs: database_calls.append(kwargs),
    )

    assert result == 78
    assert secret_reads == [
        Path("/run/secrets/postgres_email_gateway_retention_worker_password"),
        Path("/run/secrets/email_gateway_retention_bearer"),
        Path("/run/secrets/observer_email_draft_material_bearer"),
    ]
    assert database_calls == []
