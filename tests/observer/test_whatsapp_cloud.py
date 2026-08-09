from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 8, 7, 10, 30, tzinfo=UTC)
APP_SECRET = "app-secret-never-log"


def _signature(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _messages_body(messages: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-001",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550001111",
                                    "phone_number_id": "phone-number-001",
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Raw Profile"},
                                        "wa_id": "15550002222",
                                    }
                                ],
                                "messages": messages,
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def test_get_verification_returns_an_exact_plain_text_challenge() -> None:
    from observer.connectors.whatsapp_cloud import verify_webhook_challenge

    response = verify_webhook_challenge(
        mode="subscribe",
        supplied_token="expected-token",
        challenge="004219",
        expected_token="expected-token",
    )

    assert response.status_code == 200
    assert response.content_type == "text/plain; charset=utf-8"
    assert response.body == b"004219"


@pytest.mark.parametrize(
    ("mode", "token", "challenge"),
    [
        ("Subscribe", "expected-token", "42"),
        ("subscribe", "wrong-token", "42"),
        ("subscribe", "expected-token", " 42"),
        ("subscribe", "expected-token", "42\n"),
        ("subscribe", "expected-token", ""),
        ("subscribe", "expected-token", "x" * 513),
    ],
)
def test_get_verification_fails_closed_without_disclosing_tokens(
    mode: str,
    token: str,
    challenge: str,
) -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudRequestError,
        verify_webhook_challenge,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        verify_webhook_challenge(
            mode=mode,
            supplied_token=token,
            challenge=challenge,
            expected_token="expected-token",
        )

    assert raised.value.status_code == 403
    assert raised.value.reason_code == "verification_failed"
    assert "expected-token" not in str(raised.value)
    assert "expected-token" not in repr(raised.value)


def test_get_verification_uses_constant_time_token_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import observer.connectors.whatsapp_cloud as whatsapp_cloud

    calls: list[tuple[bytes, bytes]] = []
    real_compare = hmac.compare_digest

    def recording_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(whatsapp_cloud.hmac, "compare_digest", recording_compare)

    whatsapp_cloud.verify_webhook_challenge(
        mode="subscribe",
        supplied_token="expected-token",
        challenge="42",
        expected_token="expected-token",
    )

    assert calls == [(b"expected-token", b"expected-token")]


def test_get_verification_rejects_non_text_inputs_with_a_safe_error() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudRequestError,
        verify_webhook_challenge,
    )

    with pytest.raises(WhatsAppCloudRequestError, match="verification_failed"):
        verify_webhook_challenge(
            mode="subscribe",
            supplied_token=object(),
            challenge="42",
            expected_token="expected-token",
        )


class _AtomicAcceptor:
    def __init__(
        self,
        disposition: str = "accepted",
        *,
        failure: Exception | None = None,
    ) -> None:
        self.disposition = disposition
        self.failure = failure
        self.calls: list[tuple[object, str, datetime, datetime]] = []

    def __call__(
        self,
        delivery: object,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        self.calls.append((delivery, nonce, nonce_expires_at, now))
        if self.failure is not None:
            raise self.failure
        return self.disposition


def test_post_authenticates_exact_raw_bytes_and_returns_a_durable_delivery() -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudDeliveryAuthenticator

    body = b'{"exact":"bytes\\r\\n"}'
    authenticator = WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
    )

    delivery = authenticator.authenticate(
        exact_body=body,
        signature_header=_signature(body),
        delivery_id="delivery-001",
        received_at=NOW,
    )

    assert delivery.delivery_id == "delivery-001"
    assert delivery.exact_bytes is body
    assert delivery.media_type == "application/json"
    assert delivery.received_at == NOW


@pytest.mark.parametrize(
    "signature_header",
    [
        None,
        "",
        "md5=" + ("0" * 32),
        "sha256=xyz",
        "sha256=" + ("0" * 63),
        " sha256=" + ("0" * 64),
    ],
)
def test_post_rejects_missing_or_malformed_signatures(
    signature_header: str | None,
) -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudRequestError,
    )

    body = b"{}"
    authenticator = WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        authenticator.authenticate(
            exact_body=body,
            signature_header=signature_header,
            delivery_id="delivery-001",
            received_at=NOW,
        )

    assert raised.value.status_code == 401
    assert raised.value.reason_code == "authentication_failed"


def test_post_rejects_tampered_body_before_claiming_replay_identity() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudRequestError,
    )

    signed = b'{"message":"original"}'
    tampered = b'{"message":"tampered"}'
    authenticator = WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        authenticator.authenticate(
            exact_body=tampered,
            signature_header=_signature(signed),
            delivery_id="delivery-001",
            received_at=NOW,
        )

    assert raised.value.status_code == 401
    assert raised.value.reason_code == "authentication_failed"


def test_post_authenticates_before_any_json_parse() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    malformed_json = b"{not-json"
    authenticator = WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
    )

    delivery = authenticator.authenticate(
        exact_body=malformed_json,
        signature_header=_signature(malformed_json),
        delivery_id="delivery-001",
        received_at=NOW,
    )
    assert delivery.exact_bytes == malformed_json

    with pytest.raises(WhatsAppCloudQuarantineError, match="invalid_json"):
        WhatsAppCloudWebhookDecoder().decode(delivery.exact_bytes)


def test_post_rejects_oversize_body_before_signature_or_replay_work() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudRequestError,
    )

    authenticator = WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
        max_body_bytes=8,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        authenticator.authenticate(
            exact_body=b"123456789",
            signature_header=None,
            delivery_id="delivery-001",
            received_at=NOW,
        )

    assert raised.value.status_code == 413
    assert raised.value.reason_code == "payload_too_large"


@pytest.mark.parametrize(
    ("failure_name", "status_code", "reason_code"),
    [
        ("replay", 409, "replay_rejected"),
        ("expired", 408, "delivery_expired"),
    ],
)
def test_post_atomic_accept_rejects_replayed_or_expired_delivery(
    failure_name: str,
    status_code: int,
    reason_code: str,
) -> None:
    from observer.connectors.whatsapp_cloud import (
        DurableDeliveryExpired,
        DurableDeliveryReplay,
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudDurableReceiver,
        WhatsAppCloudRequestError,
    )

    body = b"{}"
    failure = DurableDeliveryReplay() if failure_name == "replay" else DurableDeliveryExpired()
    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(app_secret=APP_SECRET),
        authenticated_accept=_AtomicAcceptor(failure=failure),
        clock=lambda: NOW,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        receiver.receive(
            exact_body=body,
            signature_header=_signature(body),
            delivery_id="delivery-001",
            received_at=NOW,
        )

    assert raised.value.status_code == status_code
    assert raised.value.reason_code == reason_code


def test_post_maps_atomic_ingress_failures_to_a_safe_error() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudDurableReceiver,
        WhatsAppCloudRequestError,
    )

    body = b"{}"
    acceptor = _AtomicAcceptor(failure=RuntimeError(APP_SECRET))

    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(app_secret=APP_SECRET),
        authenticated_accept=acceptor,
        clock=lambda: NOW,
    )

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        receiver.receive(
            exact_body=body,
            signature_header=_signature(body),
            delivery_id="delivery-001",
            received_at=NOW,
        )

    assert raised.value.status_code == 503
    assert raised.value.reason_code == "durable_accept_failed"
    assert APP_SECRET not in str(raised.value)
    assert APP_SECRET not in repr(raised.value)
    assert len(acceptor.calls) == 1


def test_receive_uses_one_atomic_nonce_delivery_and_job_boundary() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudDurableReceiver,
    )

    acceptor = _AtomicAcceptor()

    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(app_secret=APP_SECRET),
        authenticated_accept=acceptor,
        clock=lambda: NOW,
        replay_window_seconds=300,
    )
    body = b"{not-json"

    result = receiver.receive(
        exact_body=body,
        signature_header=_signature(body),
        delivery_id="delivery-ordered",
        received_at=NOW,
    )

    assert len(acceptor.calls) == 1
    delivery, nonce, nonce_expires_at, now = acceptor.calls[0]
    assert delivery.__class__.__name__ == "RawDelivery"
    assert nonce.startswith("whatsapp:")
    assert "delivery-ordered" not in nonce
    assert nonce_expires_at == NOW + timedelta(seconds=300)
    assert now == NOW
    assert result.status_code == 200
    assert result.disposition == "accepted"
    assert result.work_created is True


def test_atomic_failure_allows_provider_retry_to_succeed() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudDurableReceiver,
        WhatsAppCloudRequestError,
    )

    attempts = 0

    def authenticated_accept(
        _delivery: object,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        del nonce, nonce_expires_at, now
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError(APP_SECRET)
        return "accepted"

    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(app_secret=APP_SECRET),
        authenticated_accept=authenticated_accept,
        clock=lambda: NOW,
    )
    body = b"{}"

    with pytest.raises(WhatsAppCloudRequestError) as raised:
        receiver.receive(
            exact_body=body,
            signature_header=_signature(body),
            delivery_id="delivery-retry",
            received_at=NOW,
        )
    assert raised.value.status_code == 503
    assert raised.value.reason_code == "durable_accept_failed"
    assert APP_SECRET not in repr(raised.value)

    result = receiver.receive(
        exact_body=body,
        signature_header=_signature(body),
        delivery_id="delivery-retry",
        received_at=NOW,
    )
    assert result.disposition == "accepted"
    assert result.work_created is True
    assert attempts == 2


def test_duplicate_delivery_is_idempotently_acked_without_a_second_work_item() -> None:
    from observer.connectors.whatsapp_cloud import (
        DurableDeliveryConflict,
        WhatsAppCloudDeliveryAuthenticator,
        WhatsAppCloudDurableReceiver,
        WhatsAppCloudRequestError,
    )

    stored: dict[str, tuple[str, str]] = {}
    work_items: list[str] = []

    def authenticated_accept(
        delivery: object,
        *,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
    ) -> str:
        del nonce_expires_at, now
        delivery_id = delivery.delivery_id  # type: ignore[attr-defined]
        digest = hashlib.sha256(delivery.exact_bytes).hexdigest()  # type: ignore[attr-defined]
        previous = stored.get(delivery_id)
        if previous is not None:
            if previous != (digest, nonce):
                raise DurableDeliveryConflict
            return "duplicate"
        stored[delivery_id] = (digest, nonce)
        work_items.append(delivery_id)
        return "accepted"

    receiver = WhatsAppCloudDurableReceiver(
        authenticator=WhatsAppCloudDeliveryAuthenticator(app_secret=APP_SECRET),
        authenticated_accept=authenticated_accept,
        clock=lambda: NOW,
    )
    body = b'{"message":"first"}'

    first = receiver.receive(
        exact_body=body,
        signature_header=_signature(body),
        delivery_id="delivery-idempotent",
        received_at=NOW,
    )
    duplicate = receiver.receive(
        exact_body=body,
        signature_header=_signature(body),
        delivery_id="delivery-idempotent",
        received_at=NOW,
    )

    assert first.disposition == "accepted"
    assert first.work_created is True
    assert duplicate.status_code == 200
    assert duplicate.disposition == "duplicate"
    assert duplicate.work_created is False
    assert work_items == ["delivery-idempotent"]

    conflicting_body = b'{"message":"different"}'
    with pytest.raises(WhatsAppCloudRequestError) as raised:
        receiver.receive(
            exact_body=conflicting_body,
            signature_header=_signature(conflicting_body),
            delivery_id="delivery-idempotent",
            received_at=NOW,
        )
    assert raised.value.status_code == 409
    assert raised.value.reason_code == "delivery_conflict"
    assert work_items == ["delivery-idempotent"]


def test_decoder_splits_multiple_messages_and_deduplicates_wamids_stably() -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudWebhookDecoder

    first: dict[str, object] = {
        "from": "15550002222",
        "id": "wamid.first",
        "timestamp": "1786071000",
        "type": "text",
        "text": {"body": "first"},
    }
    duplicate_first = dict(first, text={"body": "must not replace first"})
    second: dict[str, object] = {
        "from": "15550003333",
        "id": "wamid.second",
        "timestamp": "1786071001",
        "type": "text",
        "text": {"body": "second"},
    }

    decoded = WhatsAppCloudWebhookDecoder().decode_delivery(
        _messages_body([first, duplicate_first, second])
    )

    assert [item.provider_event_id for item in decoded.items] == [
        "wamid.first",
        "wamid.second",
    ]
    assert decoded.items[0].source_cursor == "wamid.first"
    assert decoded.items[0].payload["message"] == first
    assert decoded.items[0].payload["raw_contacts"] == (
        {"profile": {"name": "Raw Profile"}, "wa_id": "15550002222"},
    )
    assert "participant" not in decoded.items[0].payload
    assert "subject" not in decoded.items[0].payload


def test_status_only_webhook_is_runtime_metadata_and_never_a_connector_item() -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudWebhookDecoder

    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-001",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": "phone-number-001"},
                                "statuses": [
                                    {
                                        "id": "wamid.first",
                                        "status": "read",
                                        "timestamp": "1786071002",
                                        "recipient_id": "15550002222",
                                    },
                                    {
                                        "id": "wamid.second",
                                        "status": "failed",
                                        "timestamp": "1786071003",
                                        "recipient_id": "15550003333",
                                        "errors": [{"code": 131000, "title": "provider error"}],
                                    },
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()

    decoded = WhatsAppCloudWebhookDecoder().decode_delivery(body)

    assert decoded.items == ()
    assert [status["status"] for status in decoded.runtime_metadata.statuses] == [
        "read",
        "failed",
    ]
    assert not hasattr(decoded.runtime_metadata, "facts")


def test_media_messages_emit_read_only_independently_retryable_download_tasks() -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudWebhookDecoder

    body = _messages_body(
        [
            {
                "from": "15550002222",
                "id": "wamid.image",
                "timestamp": "1786071000",
                "type": "image",
                "image": {
                    "id": "media-001",
                    "mime_type": "image/jpeg",
                    "sha256": "a" * 64,
                    "caption": "sample",
                },
            }
        ]
    )

    decoded = WhatsAppCloudWebhookDecoder().decode_delivery(body)
    task = decoded.media_download_tasks[0]

    assert task.media_id == "media-001"
    assert task.media_type == "image/jpeg"
    assert task.sha256 == "a" * 64
    assert task.provider_event_id == "wamid.image"
    assert task.retry_key == "whatsapp-media:media-001"
    assert task == decoded.items[0].payload["media_download_task"]
    assert not hasattr(task, "url")
    assert not hasattr(task, "token")
    assert not hasattr(task, "__dict__")


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe\x00\x01",
        json.dumps({"object": "unknown", "entry": []}).encode(),
        json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "waba-001", "changes": [{"field": "unknown", "value": {}}]}],
            }
        ).encode(),
        json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [],
                "unexpected": {},
            }
        ).encode(),
    ],
)
def test_decoder_quarantines_binary_and_unknown_objects(body: bytes) -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    with pytest.raises(WhatsAppCloudQuarantineError):
        WhatsAppCloudWebhookDecoder().decode_delivery(body)


def test_decoder_rejects_duplicate_json_keys_and_unsupported_message_shapes() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    duplicate_root_key = b'{"object":"unknown","object":"whatsapp_business_account","entry":[]}'
    with pytest.raises(WhatsAppCloudQuarantineError, match="duplicate_json_key"):
        WhatsAppCloudWebhookDecoder().decode_delivery(duplicate_root_key)

    unsupported = _messages_body(
        [
            {
                "from": "15550002222",
                "id": "wamid.interactive",
                "timestamp": "1786071000",
                "type": "interactive",
                "interactive": {"unknown": {}},
            }
        ]
    )
    with pytest.raises(WhatsAppCloudQuarantineError, match="invalid_interactive_message"):
        WhatsAppCloudWebhookDecoder().decode_delivery(unsupported)


@pytest.mark.parametrize(
    ("message_type", "content"),
    [
        ("button", {"payload": "quote-request", "text": "请报价"}),
        (
            "interactive",
            {
                "type": "button_reply",
                "button_reply": {"id": "confirm-sample", "title": "确认样品"},
            },
        ),
        (
            "interactive",
            {
                "type": "list_reply",
                "list_reply": {
                    "id": "size-100ml",
                    "title": "100 ml",
                    "description": "透明玻璃瓶",
                },
            },
        ),
        (
            "location",
            {
                "latitude": 25.2048,
                "longitude": 55.2708,
                "name": "Dubai",
                "address": "Restricted address sentinel",
            },
        ),
        (
            "contacts",
            [
                {
                    "name": {"formatted_name": "Contact Sentinel"},
                    "phones": [{"phone": "+971500000000", "type": "WORK"}],
                }
            ],
        ),
        ("reaction", {"message_id": "wamid.original", "emoji": "👍"}),
        (
            "order",
            {
                "catalog_id": "catalog-001",
                "text": "Order note sentinel",
                "product_items": [
                    {
                        "product_retailer_id": "sku-001",
                        "quantity": "2",
                        "item_price": "1.00",
                        "currency": "USD",
                    }
                ],
            },
        ),
        (
            "system",
            {
                "body": "Customer changed number",
                "type": "customer_changed_number",
                "wa_id": "15550004444",
            },
        ),
        ("unknown", None),
    ],
)
def test_decoder_accepts_current_non_media_inbound_message_families(
    message_type: str,
    content: object,
) -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudWebhookDecoder

    message: dict[str, object] = {
        "from": "15550002222",
        "id": f"wamid.{message_type}",
        "timestamp": "1786071000",
        "type": message_type,
        message_type: content,
    }
    if message_type == "unknown":
        message.pop("unknown")
        message["errors"] = [
            {
                "code": 131051,
                "title": "Unsupported message type",
                "details": "Preserve for human review",
            }
        ]

    decoded = WhatsAppCloudWebhookDecoder().decode_delivery(_messages_body([message]))

    assert [item.provider_event_id for item in decoded.items] == [f"wamid.{message_type}"]
    stored_message = decoded.items[0].payload["message"]
    assert stored_message["type"] == message_type
    assert ("errors" if message_type == "unknown" else message_type) in stored_message
    assert decoded.media_download_tasks == ()


def test_decoded_delivery_repr_never_contains_message_contact_or_runtime_payloads() -> None:
    from observer.connectors.whatsapp_cloud import WhatsAppCloudWebhookDecoder

    body_sentinel = "BODY-SENTINEL-DO-NOT-LOG"
    contact_sentinel = "CONTACT-SENTINEL-DO-NOT-LOG"
    decoded = WhatsAppCloudWebhookDecoder().decode_delivery(
        _messages_body(
            [
                {
                    "from": "15550002222",
                    "id": "wamid.redacted",
                    "timestamp": "1786071000",
                    "type": "text",
                    "text": {"body": body_sentinel},
                }
            ]
        ).replace(b"Raw Profile", contact_sentinel.encode())
    )

    rendered = repr((decoded, decoded.items[0], decoded.runtime_metadata))

    assert body_sentinel not in rendered
    assert contact_sentinel not in rendered
    assert "15550002222" not in rendered
    assert "payload=<redacted>" in rendered


def test_decoder_rejects_non_finite_json_numbers() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    body = (
        b'{"object":"whatsapp_business_account","entry":[{"id":"waba-001",'
        b'"changes":[{"field":"messages","value":{"messaging_product":"whatsapp",'
        b'"metadata":{"phone_number_id":"phone-number-001"},'
        b'"errors":[{"code":1e400}]}}]}]}'
    )

    with pytest.raises(WhatsAppCloudQuarantineError, match="invalid_json_number"):
        WhatsAppCloudWebhookDecoder().decode_delivery(body)


def test_decoder_enforces_message_count_and_nesting_depth_bounds() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    message: dict[str, object] = {
        "from": "15550002222",
        "id": "wamid.same",
        "timestamp": "1786071000",
        "type": "text",
        "text": {"body": "hello"},
    }
    with pytest.raises(WhatsAppCloudQuarantineError, match="too_many_messages"):
        WhatsAppCloudWebhookDecoder(max_messages=2).decode_delivery(
            _messages_body([message, dict(message), dict(message)])
        )

    nested: object = "leaf"
    for _ in range(12):
        nested = {"next": nested}
    with pytest.raises(WhatsAppCloudQuarantineError, match="payload_too_deep"):
        WhatsAppCloudWebhookDecoder(max_depth=10).decode_delivery(json.dumps(nested).encode())


def test_decoder_rejects_oversize_body_before_parsing() -> None:
    from observer.connectors.whatsapp_cloud import (
        WhatsAppCloudQuarantineError,
        WhatsAppCloudWebhookDecoder,
    )

    with pytest.raises(WhatsAppCloudQuarantineError, match="payload_too_large"):
        WhatsAppCloudWebhookDecoder(max_body_bytes=2).decode_delivery(b"{} ")


def test_adapter_has_no_outbound_messaging_surface_and_secrets_are_redacted() -> None:
    import observer.connectors.whatsapp_cloud as whatsapp_cloud

    authenticator = whatsapp_cloud.WhatsAppCloudDeliveryAuthenticator(
        app_secret=APP_SECRET,
    )
    public_names = {
        name for name, _value in inspect.getmembers(whatsapp_cloud) if not name.startswith("_")
    }
    public_methods = {
        name
        for name, _value in inspect.getmembers(authenticator, predicate=callable)
        if not name.startswith("_")
    }

    assert not public_names.intersection(
        {"send", "send_message", "reply", "send_template", "create_template"}
    )
    assert public_methods == {"authenticate"}
    assert APP_SECRET not in repr(authenticator)

    with pytest.raises(whatsapp_cloud.WhatsAppCloudRequestError) as raised:
        authenticator.authenticate(
            exact_body=b"{}",
            signature_header="sha256=" + ("0" * 64),
            delivery_id="delivery-001",
            received_at=NOW,
        )
    assert APP_SECRET not in str(raised.value)
    assert APP_SECRET not in repr(raised.value)
