from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.local_pilot_runtime.email_command_publication_worker import (
    CommandPublicationRelayWorker,
    FrappeCommandPublicationClient,
    GatewayCommandIngestClient,
    PublicationRelayStatus,
    main,
)
from tests.email_gateway.fakes.provider import closed_command

NOW = datetime(2026, 8, 13, 13, 5, tzinfo=UTC)


class _Transport:
    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs: object) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _claim() -> dict[str, Any]:
    command = closed_command()
    return {
        "publication": {
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "attempt": 1,
            "generation": 1,
            "fence_token": "FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "lease_expires_at": "2026-08-13T13:05:30Z",
            "command": command,
            "payload_digest": "sha256:" + command["payload_sha256"],
        }
    }


def test_relay_uses_one_frappe_credential_and_separate_gateway_bearer_then_acks() -> None:
    command = closed_command()
    transport = _Transport(
        (200, _claim()),
        (200, {"lease": {"lease_expires_at": "2026-08-13T13:05:30Z"}}),
        (
            200,
            {
                "command_receipt_ref": "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "send_outbox_ref": "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                "payload_digest": command["payload_sha256"],
            },
        ),
        (200, {"acknowledgement": {"publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"}}),
    )
    frappe = FrappeCommandPublicationClient(
        transport=transport,
        api_key="frappe-publication-key",
        api_secret="frappe-publication-secret",
    )
    gateway = GatewayCommandIngestClient(
        transport=transport,
        bearer_token="gateway-command-ingest-secret",
    )
    worker = CommandPublicationRelayWorker(
        frappe=frappe,
        gateway=gateway,
        site_id="gbos.localhost",
        worker_id="email-command-relay-01",
        clock=lambda: NOW,
        lease_seconds=30,
    )

    result = worker.run_once()

    assert result.status == PublicationRelayStatus.DELIVERED
    assert [str(call["url"]).rsplit(".", 1)[-1] for call in transport.calls[:2]] == [
        "claim",
        "heartbeat",
    ]
    gateway_call = transport.calls[2]
    assert gateway_call["url"] == (
        "http://email-gateway-api:8004/internal/v1/email-commands/accept"
    )
    assert gateway_call["headers"]["Authorization"] == (  # type: ignore[index]
        "Bearer gateway-command-ingest-secret"
    )
    assert "frappe-publication-secret" not in repr(gateway_call)
    assert transport.calls[3]["url"] == (
        "http://frappe-backend:8000/api/method/"
        "esan_gbos.api.internal.email_command_publication.acknowledge"
    )


def test_gateway_failure_releases_frappe_claim_with_fixed_safe_code_and_never_acks() -> None:
    transport = _Transport(
        (200, _claim()),
        (200, {"lease": {"lease_expires_at": "2026-08-13T13:05:30Z"}}),
        (503, {"error": {"code": "unavailable"}}),
        (200, {"release": {"status": "Retry"}}),
    )
    worker = CommandPublicationRelayWorker(
        frappe=FrappeCommandPublicationClient(
            transport=transport,
            api_key="frappe-publication-key",
            api_secret="frappe-publication-secret",
        ),
        gateway=GatewayCommandIngestClient(
            transport=transport,
            bearer_token="gateway-command-ingest-secret",
        ),
        site_id="gbos.localhost",
        worker_id="email-command-relay-01",
        clock=lambda: NOW,
        lease_seconds=30,
    )

    assert worker.run_once().status == PublicationRelayStatus.RETRY
    assert transport.calls[-1]["payload"]["safe_code"] == "gateway_unavailable"  # type: ignore[index]
    assert all("acknowledge" not in str(call["url"]) for call in transport.calls)


def test_main_defaults_closed_before_http_factory(tmp_path: Path) -> None:
    calls: list[str] = []
    assert (
        main(
            manifest_path=tmp_path / "missing.json",
            environ={},
            transport_factory=lambda: calls.append("http"),  # type: ignore[arg-type,return-value]
        )
        == 78
    )
    assert calls == []
