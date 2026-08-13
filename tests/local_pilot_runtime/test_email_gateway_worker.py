from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.email_gateway.repositories.mailboxes import MailboxConfigOutboxClaim
from services.local_pilot_runtime.email_publication_worker import (
    EmailPublicationRelayWorker,
    RelayStatus,
)
from services.local_pilot_runtime.mailbox_config_projection_worker import (
    GatewayConfigOutboxAdapter,
    MailboxConfigProjectionWorker,
)

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)


@dataclass(frozen=True)
class _Claim:
    site_id: str = "alpha.example"
    item_ref: str = "PUB-1"
    request_id: str = "request-1"
    payload: dict[str, Any] | None = None
    payload_digest: str = "sha256:" + "a" * 64
    attempt: int = 1
    max_attempts: int = 3
    generation: int = 1
    fence_token: str = "opaque-fence"


class _Outbox:
    def __init__(self, claim: _Claim | None = None) -> None:
        self.claim_value = claim
        self.events: list[tuple[str, object]] = []

    def claim(self, **kwargs: object) -> _Claim | None:
        self.events.append(("claim", kwargs))
        return self.claim_value

    def heartbeat(self, claim: _Claim, **kwargs: object) -> None:
        self.events.append(("heartbeat", claim.fence_token))

    def mark_delivered(self, claim: _Claim, *, receipt: dict[str, object], now: datetime) -> None:
        self.events.append(("delivered", receipt))

    def mark_failed(
        self, claim: _Claim, *, retry_at: datetime, error_code: str, now: datetime
    ) -> str:
        self.events.append(("failed", error_code))
        return "dead_letter" if claim.attempt == claim.max_attempts else "retry"


class _Transport:
    def __init__(self, response: tuple[int, dict[str, Any]] | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs: object) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def test_publication_relay_marks_delivered_only_after_exact_stable_receipt() -> None:
    outbox = _Outbox(_Claim(payload={"publication_id": "PUB-1"}))
    transport = _Transport(
        (
            200,
            {
                "schema_version": "1.0",
                "binding": {
                    "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "message_ref": "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "mailbox_config_revision": 1,
                    "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "payload_digest": "sha256:" + "a" * 64,
                    "participant_binding_digest": "sha256:" + "b" * 64,
                    "evidence_binding_digest": "sha256:" + "c" * 64,
                },
            },
        )
    )
    outbox.claim_value = _Claim(
        item_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload={"publication_id": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV"},
    )
    worker = EmailPublicationRelayWorker(
        outbox=outbox,
        transport=transport,
        bearer_token="secret",
        worker_id="publisher-1",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=30),
    )

    result = worker.run_once()

    assert result.status == RelayStatus.DELIVERED
    assert outbox.events[-1][0] == "delivered"
    delivered = outbox.events[-1][1]
    assert isinstance(delivered, dict)
    assert set(delivered) == {
        "gateway_receipt_ref",
        "publication_ref",
        "inbox_item_ref",
        "message_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_delivery_ref",
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
    }
    call = transport.calls[0]
    assert call["url"] == "http://email-gateway-api:8004/internal/v1/email-publications/accept"
    assert call["headers"]["X-Request-ID"] == "request-1"  # type: ignore[index]


def test_relay_retries_boundedly_without_ack_on_429_and_closes_in_main_boundary() -> None:
    outbox = _Outbox(_Claim(payload={"publication_id": "PUB-1"}))
    worker = EmailPublicationRelayWorker(
        outbox=outbox,
        transport=_Transport((429, {"error": {"code": "busy"}})),
        bearer_token="secret",
        worker_id="publisher-1",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=30),
    )

    result = worker.run_once()

    assert result.status == RelayStatus.RETRY
    assert all(event[0] != "delivered" for event in outbox.events)
    assert outbox.events[-1] == ("failed", "downstream_retryable")


def test_config_projection_uses_observer_url_different_auth_and_exact_receipt() -> None:
    claim = _Claim(item_ref="MCP-1", payload={"config_publication_ref": "MCP-1"})
    outbox = _Outbox(claim)
    transport = _Transport(
        (
            200,
            {
                "schema_version": "1.0",
                "receipt_ref": "OCP-1",
                "config_publication_ref": "MCP-1",
                "payload_digest": claim.payload_digest,
            },
        )
    )
    worker = MailboxConfigProjectionWorker(
        outbox=outbox,
        transport=transport,
        bearer_token="different-secret",
        worker_id="config-relay-1",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=30),
    )

    assert worker.run_once().status == RelayStatus.DELIVERED
    call = transport.calls[0]
    assert call["url"] == "http://observer-api:8003/internal/v1/email-connectors/apply-config"
    assert call["headers"]["X-GBOS-Local-Auth-Ref"] == "gateway-mailbox-projection-v1"  # type: ignore[index]


class _ConfigRepository:
    def __init__(self, claim: MailboxConfigOutboxClaim | None) -> None:
        self.claim_value = claim
        self.events: list[tuple[str, object]] = []

    def claim(self, scope: object, **kwargs: object) -> MailboxConfigOutboxClaim | None:
        self.events.append(("claim", scope))
        value, self.claim_value = self.claim_value, None
        return value

    def heartbeat(self, scope: object, ref: str, **kwargs: object) -> None:
        self.events.append(("heartbeat", ref))

    def mark_delivered(self, scope: object, ref: str, **kwargs: object) -> None:
        self.events.append(("delivered", kwargs["receipt_ref"]))

    def mark_failed(self, scope: object, ref: str, **kwargs: object) -> str:
        self.events.append(("failed", kwargs["error_code"]))
        return "retry"


def _config_claim(*, provider: str = "imap_smtp") -> MailboxConfigOutboxClaim:
    return MailboxConfigOutboxClaim(
        site_id="alpha.example",
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_config_revision=3,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        provider=provider,
        entry_role="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email-primary",
        inbound_enabled=True,
        outbound_enabled=False,
        mailbox_status="active",
        activation_not_before=NOW,
        processing_purpose="sales_follow_up",
        request_id="mailbox-config-request-1",
        idempotency_key="mailbox-config-idempotency-1",
        payload_digest="sha256:" + "b" * 64,
        status="leased",
        attempt=1,
        lease_owner="config-worker-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        lease_generation=2,
        fence_token="v1:1:2:" + "f" * 64,
    )


def test_gateway_config_adapter_uses_projection_digest_and_exact_publication_identity() -> None:
    repository = _ConfigRepository(_config_claim())
    adapter = GatewayConfigOutboxAdapter(repository, site_id="alpha.example")  # type: ignore[arg-type]

    claim = adapter.claim(
        worker_id="config-worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    assert claim is not None
    assert claim.item_ref == claim.request_id == "MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert claim.payload is not None
    assert claim.payload_digest == claim.payload["projection_digest"]
    assert claim.payload_digest != "sha256:" + "b" * 64
    adapter.heartbeat(claim, now=NOW, lease_duration=timedelta(seconds=30))
    adapter.mark_delivered(
        claim,
        receipt={"receipt_ref": "OCP-01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        now=NOW,
    )
    assert repository.events[-2:] == [
        ("heartbeat", claim.item_ref),
        ("delivered", "OCP-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
    ]


def test_fake_config_claim_is_dead_lettered_without_http_transport() -> None:
    repository = _ConfigRepository(_config_claim(provider="fake"))
    adapter = GatewayConfigOutboxAdapter(repository, site_id="alpha.example")  # type: ignore[arg-type]
    transport = _Transport(AssertionError("HTTP must not run for fake provider"))
    worker = MailboxConfigProjectionWorker(
        outbox=adapter,
        transport=transport,
        bearer_token="different-secret",
        worker_id="config-worker-1",
        clock=lambda: NOW,
        lease_duration=timedelta(seconds=30),
    )

    result = worker.run_once()

    assert result.status == RelayStatus.RETRY
    assert transport.calls == []
    assert repository.events[-1] == ("failed", "relay_payload_unavailable")
