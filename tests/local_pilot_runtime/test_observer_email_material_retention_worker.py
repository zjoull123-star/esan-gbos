from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.local_pilot_runtime import observer_email_material_retention_worker as worker
from services.observer.observer.email_material_retention_callback import (
    EmailMaterialRetentionCallback,
)
from services.observer.observer.models import TenantScope

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)


def _secret(path: Path, value: str) -> None:
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "runtime-observer-email-material-retention.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "site_id": "alpha.example",
                "external_send": False,
                "postgres": {
                    "host": "postgres",
                    "port": 5432,
                    "database": "gbos_local_pilot",
                    "user": "gbos_observer_app",
                    "password_file": "/run/secrets/postgres_observer_password",
                    "connect_timeout_seconds": 5,
                },
                "gateway_api": {
                    "authority_endpoint": "http://email-gateway-retention-worker:9102/internal/v1/retention/email-material/authority/resolve",
                    "callback_endpoint": "http://email-gateway-retention-worker:9102/internal/v1/retention/email-material/tombstone-callback",
                    "bearer_file": "/run/secrets/email_gateway_retention_bearer",
                    "auth_ref": "email-gateway-retention-v1",
                },
                "worker": {
                    "worker_id": "observer-email-material-retention-worker",
                    "batch_size": 100,
                    "interval_seconds": 3600,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _callback() -> EmailMaterialRetentionCallback:
    return EmailMaterialRetentionCallback(
        callback_ref="EMC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id="alpha.example",
        purpose="email_draft_material",
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        deleted_at=NOW + timedelta(days=30),
        evidence_digest="sha256:" + "1" * 64,
        callback_payload_digest="sha256:" + "2" * 64,
    )


def test_load_config_accepts_only_frozen_observer_retention_shape(tmp_path: Path) -> None:
    path = _config(tmp_path)

    config = worker.load_observer_email_material_retention_config(path)

    assert config.site_id == "alpha.example"
    assert config.postgres.user == "gbos_observer_app"
    assert config.gateway_api.auth_ref == "email-gateway-retention-v1"
    assert config.worker.batch_size == 100

    value = json.loads(path.read_text(encoding="utf-8"))
    value["unexpected"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="config rejected"):
        worker.load_observer_email_material_retention_config(path)


def test_gateway_callback_transport_posts_exact_callback_and_receipt_contract() -> None:
    callback = _callback()
    calls: list[dict[str, object]] = []

    def transport(**kwargs: object) -> tuple[int, dict[str, object]]:
        calls.append(kwargs)
        payload = kwargs["payload"]
        assert payload == callback.to_wire()
        return 200, {
            "schema_version": "1.0",
            "site_id": callback.site_id,
            "authority_receipt_ref": callback.authority_receipt_ref,
            "tombstone_receipt_ref": callback.tombstone_receipt_ref,
            "callback_receipt_ref": "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "accepted": True,
        }

    client = worker.HttpGatewayTombstoneCallback(
        endpoint="http://email-gateway-retention-worker:9102/internal/v1/retention/email-material/tombstone-callback",
        bearer_token="retention-token",
        auth_ref="email-gateway-retention-v1",
        transport=transport,
    )

    receipt = client.deliver(callback.to_wire())

    assert receipt == {
        "schema_version": "1.0",
        "site_id": callback.site_id,
        "authority_receipt_ref": callback.authority_receipt_ref,
        "tombstone_receipt_ref": callback.tombstone_receipt_ref,
        "callback_receipt_ref": "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "accepted": True,
    }
    assert calls[0]["timeout_seconds"] == 3.0
    assert calls[0]["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer retention-token",
        "Content-Type": "application/json",
        "X-GBOS-Local-Auth-Ref": "email-gateway-retention-v1",
        "X-Processing-Purpose": "email_draft_material",
        "X-Request-ID": callback.observer_request_ref,
        "X-Site-ID": callback.site_id,
    }


def test_gateway_callback_transport_rejects_nonexact_receipt() -> None:
    callback = _callback()
    response = {
        "schema_version": "1.0",
        "site_id": callback.site_id,
        "authority_receipt_ref": callback.authority_receipt_ref,
        "tombstone_receipt_ref": callback.tombstone_receipt_ref,
        "callback_receipt_ref": "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "accepted": True,
        "invented": "field",
    }
    client = worker.HttpGatewayTombstoneCallback(
        endpoint="http://email-gateway-retention-worker:9102/internal/v1/retention/email-material/tombstone-callback",
        bearer_token="retention-token",
        auth_ref="email-gateway-retention-v1",
        transport=lambda **_kwargs: (200, response),
    )

    with pytest.raises(ValueError, match="response rejected"):
        client.deliver(callback.to_wire())


class _DeletionRunner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run_once(self, scope: TenantScope, *, batch_size: int) -> tuple[object, ...]:
        self.events.append(f"delete:{scope.site_id}:{batch_size}")
        return (object(), object())


class _CallbackRelay:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def run_once(self, scope: TenantScope) -> bool:
        self.calls += 1
        self.events.append(f"callback:{scope.site_id}:{self.calls}")
        return True


def test_one_iteration_deletes_then_bounds_callback_delivery_to_100() -> None:
    events: list[str] = []
    relay = _CallbackRelay(events)
    cycle = worker.ObserverEmailMaterialRetentionCycle(
        deletion_runner=_DeletionRunner(events),
        callback_relay=relay,
        site_id="alpha.example",
        batch_size=100,
        max_callbacks=100,
    )

    deleted, callbacks = cycle.run_once()

    assert (deleted, callbacks) == (2, 100)
    assert events[0] == "delete:alpha.example:100"
    assert relay.calls == 100


def test_cycle_allowed_is_default_off_and_honors_every_kill_switch(tmp_path: Path) -> None:
    enabled = {
        "GBOS_LOCAL_RUNTIME_ENABLED": "true",
        "GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_ENABLED": "true",
        "GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_KILL_SWITCH": "false",
        "GBOS_EMAIL_GATEWAY_KILL_SWITCH": "false",
        "GBOS_GLOBAL_KILL_SWITCH": "false",
    }
    stop_file = tmp_path / "EMERGENCY_STOP"

    assert worker.cycle_allowed(enabled, stop_file=stop_file)
    assert not worker.cycle_allowed({}, stop_file=stop_file)
    for name in (
        "GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_KILL_SWITCH",
        "GBOS_EMAIL_GATEWAY_KILL_SWITCH",
        "GBOS_GLOBAL_KILL_SWITCH",
    ):
        assert not worker.cycle_allowed({**enabled, name: "true"}, stop_file=stop_file)
    stop_file.touch()
    assert not worker.cycle_allowed(enabled, stop_file=stop_file)


def test_main_missing_dedicated_bearer_fails_before_postgres(tmp_path: Path) -> None:
    connect_calls: list[dict[str, object]] = []

    result = worker.main(
        config_path=_config(tmp_path),
        bearer_file=tmp_path / "missing-retention-bearer",
        environ={
            "GBOS_LOCAL_RUNTIME_ENABLED": "true",
            "GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_ENABLED": "true",
            "GBOS_OBSERVER_EMAIL_MATERIAL_RETENTION_KILL_SWITCH": "false",
            "GBOS_EMAIL_GATEWAY_KILL_SWITCH": "false",
            "GBOS_GLOBAL_KILL_SWITCH": "false",
        },
        connector=lambda **kwargs: connect_calls.append(kwargs),
    )

    assert result == 78
    assert connect_calls == []
