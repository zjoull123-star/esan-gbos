from __future__ import annotations

from datetime import UTC, datetime

import pytest
from observer.connectors.whatsapp_cloud import (
    DurableDeliveryConflict,
    DurableDeliveryExpired,
    DurableDeliveryReplay,
)
from observer.local_pilot_api import LocalPilotAPIConfig
from observer.local_pilot_storage import DeliveryConflict, IngressExpired, NonceReplay
from observer.runtime import (
    KillSwitchEngaged,
    LocalPilotRuntimeGuard,
    compose_postgres_local_pilot_runtime,
    map_whatsapp_durable_accept,
)

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    ("storage_error", "public_error"),
    (
        (DeliveryConflict("different digest"), DurableDeliveryConflict),
        (NonceReplay("nonce"), DurableDeliveryReplay),
        (IngressExpired("late"), DurableDeliveryExpired),
    ),
)
def test_whatsapp_ingress_maps_storage_failures_to_safe_receiver_errors(
    storage_error: Exception,
    public_error: type[Exception],
) -> None:
    def accept(*_args: object, **_kwargs: object) -> str:
        raise storage_error

    mapped = map_whatsapp_durable_accept(accept)

    with pytest.raises(public_error):
        mapped(
            object(),
            nonce="nonce",
            nonce_expires_at=NOW,
            now=NOW,
        )


def test_runtime_kill_switch_is_fail_closed_and_visible_in_health() -> None:
    guard = LocalPilotRuntimeGuard(enabled=True, kill_switch=False)
    guard.require_running()
    assert guard.health()["status"] == "ok"

    guard.engage("operator_stop")

    with pytest.raises(KillSwitchEngaged):
        guard.require_running()
    assert guard.health() == {
        "status": "stopped",
        "runtime_enabled": True,
        "kill_switch": True,
        "safe_reason_code": "operator_stop",
        "external_send": False,
        "formal_business_commands": False,
    }


def test_runtime_composition_instantiates_postgres_adapters_without_starting_io() -> None:
    runtime = compose_postgres_local_pilot_runtime(
        connection=object(),
        storage=object(),
        api_config=LocalPilotAPIConfig(
            bind_host="127.0.0.1",
            network_mode="loopback",
            bearer_token="synthetic-local-token",
            auth_ref="observer-token-v1",
        ),
        cursor_secret=b"x" * 32,
        publisher=lambda _scope, _event_id, _idempotency_key: None,
        clock=lambda: NOW,
        outbox_worker_id="context-publisher-1",
        enabled=True,
        kill_switch=False,
    )

    assert runtime.guard.health()["status"] == "ok"
    assert runtime.app.title == "ESAN GBOS Observer Local Pilot"
    assert runtime.control._repository is runtime.control_repository
    assert runtime.reader._repository is runtime.communication_repository
