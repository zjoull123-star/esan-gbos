from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.local_pilot_runtime.webhook import (
    WhatsAppWebhookConfig,
    create_whatsapp_webhook_app,
    main,
)
from services.observer.observer.connectors.whatsapp_cloud import (
    WhatsAppCloudDeliveryAuthenticator,
    WhatsAppCloudDurableReceiver,
)
from services.observer.observer.models import RawDelivery
from services.observer.observer.runtime import LocalPilotRuntimeGuard

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
BODY = b'{"object":"whatsapp_business_account","entry":[]}'
APP_SECRET = "private-app-secret"
VERIFY_TOKEN = "private-verify-token"
PATH = "/webhooks/whatsapp"


def _signature(body: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class DurableAccept:
    def __init__(self, *, disposition: str = "accepted") -> None:
        self.disposition = disposition
        self.calls: list[tuple[RawDelivery, str, datetime, datetime]] = []

    def __call__(
        self,
        delivery: RawDelivery,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        self.calls.append((delivery, nonce, nonce_expires_at, now))
        return self.disposition


def _client(
    accept: DurableAccept,
    *,
    enabled: bool = True,
    kill_switch: bool = False,
    max_body_bytes: int = 1_024,
) -> TestClient:
    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(
            app_secret=APP_SECRET,
            max_body_bytes=max_body_bytes,
        ),
        authenticated_accept=accept,
        clock=lambda: NOW,
    )
    app = create_whatsapp_webhook_app(
        config=WhatsAppWebhookConfig(
            path=PATH,
            verify_token=VERIFY_TOKEN,
            max_body_bytes=max_body_bytes,
        ),
        receiver=receiver,
        guard=LocalPilotRuntimeGuard(
            enabled=enabled,
            kill_switch=kill_switch,
        ),
        clock=lambda: NOW,
    )
    return TestClient(app)


def test_get_challenge_requires_injected_runtime_and_returns_exact_plain_value() -> None:
    client = _client(DurableAccept())

    response = client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123456",
        },
    )

    assert response.status_code == 200
    assert response.content == b"123456"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert VERIFY_TOKEN not in repr(client.app)


def test_post_authenticates_exact_raw_body_then_acknowledges_accept_and_duplicate() -> None:
    accepted = DurableAccept()
    accepted_response = _client(accepted).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )

    duplicate = DurableAccept(disposition="duplicate")
    duplicate_response = _client(duplicate).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )

    assert accepted_response.status_code == 200
    assert accepted_response.json() == {"status": "accepted"}
    assert duplicate_response.status_code == 200
    assert duplicate_response.json() == {"status": "duplicate"}
    delivery = accepted.calls[0][0]
    assert delivery.exact_bytes == BODY
    assert delivery.delivery_id == ("whatsapp-webhook:" + hashlib.sha256(BODY).hexdigest())
    assert len(accepted.calls) == len(duplicate.calls) == 1


def test_post_rejects_oversize_or_bad_signature_before_durable_accept() -> None:
    oversized_accept = DurableAccept()
    oversized = _client(oversized_accept, max_body_bytes=len(BODY) - 1).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )
    unsigned_accept = DurableAccept()
    unsigned = _client(unsigned_accept).post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )

    assert oversized.status_code == 413
    assert oversized.json() == {"error": {"code": "payload_too_large"}}
    assert unsigned.status_code == 401
    assert unsigned.json() == {"error": {"code": "authentication_failed"}}
    assert oversized_accept.calls == []
    assert unsigned_accept.calls == []


def test_default_missing_dependencies_kill_switch_and_unknown_paths_fail_closed() -> None:
    default_client = TestClient(create_whatsapp_webhook_app())
    stopped_accept = DurableAccept()
    stopped_client = _client(stopped_accept, kill_switch=True)

    missing = default_client.get(
        PATH,
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123456",
        },
    )
    stopped = stopped_client.post(
        PATH,
        content=BODY,
        headers={"X-Hub-Signature-256": _signature(BODY)},
    )
    unknown = stopped_client.post("/not-allowed", content=BODY)

    assert missing.status_code == 503
    assert missing.json() == {"error": {"code": "runtime_disabled"}}
    assert stopped.status_code == 503
    assert stopped.json() == {"error": {"code": "runtime_disabled"}}
    assert unknown.status_code == 404
    assert stopped_accept.calls == []
    assert main() == 78
