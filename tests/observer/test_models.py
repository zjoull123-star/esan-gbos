from __future__ import annotations

from datetime import UTC, datetime

from observer.models import ConnectorItem, RawDelivery

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


def test_raw_delivery_repr_is_an_explicit_content_safe_summary() -> None:
    sentinel = "private-message-contact@example.com/customer-file.pdf"
    delivery = RawDelivery(
        delivery_id=f"provider:{sentinel}",
        exact_bytes=sentinel.encode(),
        media_type=f"application/{sentinel}",
        received_at=NOW,
    )

    rendered = repr(delivery)

    assert sentinel not in rendered
    assert "exact_bytes" not in rendered
    assert "byte_size=" in rendered
    assert "body_sha256=" in rendered
    assert delivery.delivery_id == f"provider:{sentinel}"
    assert delivery.exact_bytes == sentinel.encode()
    assert delivery.media_type == f"application/{sentinel}"


def test_connector_item_repr_never_renders_payload_or_provider_identifiers() -> None:
    sentinel = "private-contact@example.com/customer-file.pdf/message body"
    item = ConnectorItem(
        provider_event_id=f"event:{sentinel}",
        occurred_at=NOW,
        source_cursor=f"cursor:{sentinel}",
        payload={
            "message": sentinel,
            "contact": sentinel,
            "filename": sentinel,
        },
    )

    rendered = repr(item)

    assert sentinel not in rendered
    assert "'message'" not in rendered
    assert "'contact'" not in rendered
    assert "'filename'" not in rendered
    assert "payload_entries=3" in rendered
    assert item.provider_event_id == f"event:{sentinel}"
    assert item.payload["message"] == sentinel
