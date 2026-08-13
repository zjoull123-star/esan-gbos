from __future__ import annotations

from datetime import UTC, datetime

import pytest
from observer.connectors.email_provider import EmailProviderError
from observer.connectors.fake_email_provider import FakeEmailProvider, FakeEmailProviderMode

NOW = datetime(2026, 8, 13, 13, tzinfo=UTC)


def test_fake_provider_ordered_duplicate_and_out_of_order_are_deterministic() -> None:
    ordered = FakeEmailProvider(mode=FakeEmailProviderMode.ORDERED_SUCCESS, now=NOW)
    duplicate = FakeEmailProvider(mode=FakeEmailProviderMode.DUPLICATE, now=NOW)
    out_of_order = FakeEmailProvider(mode=FakeEmailProviderMode.OUT_OF_ORDER, now=NOW)

    assert [d.delivery_id for d in ordered.poll(None, 10).deliveries] == [
        "fake:0001",
        "fake:0002",
    ]
    assert [d.delivery_id for d in duplicate.poll(None, 10).deliveries] == [
        "fake:0001",
        "fake:0001",
    ]
    assert [d.delivery_id for d in out_of_order.poll(None, 10).deliveries] == [
        "fake:0002",
        "fake:0001",
    ]
    assert not hasattr(ordered, "checkpoint")
    assert not hasattr(ordered, "advance_checkpoint")


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        (FakeEmailProviderMode.RATE_LIMITED, "provider_rate_limited"),
        (FakeEmailProviderMode.TIMEOUT, "provider_timeout"),
    ],
)
def test_fake_provider_transient_modes_have_safe_closed_errors(mode, code) -> None:
    provider = FakeEmailProvider(mode=mode, now=NOW)
    with pytest.raises(EmailProviderError, match=code):
        provider.poll(None, 10)


def test_fake_provider_crash_restart_recovers_the_same_ordered_batch() -> None:
    provider = FakeEmailProvider(mode=FakeEmailProviderMode.CRASH_RESTART, now=NOW)
    with pytest.raises(EmailProviderError, match="provider_crash"):
        provider.poll(None, 10)

    recovered = provider.poll(None, 10)
    assert [value.delivery_id for value in recovered.deliveries] == [
        "fake:0001",
        "fake:0002",
    ]


def test_fake_provider_malformed_and_oversized_modes_use_same_delivery_path() -> None:
    malformed = FakeEmailProvider(mode=FakeEmailProviderMode.MALFORMED_MIME, now=NOW)
    oversized = FakeEmailProvider(mode=FakeEmailProviderMode.OVERSIZED_ATTACHMENT, now=NOW)

    assert malformed.poll(None, 10).deliveries[0].media_type == "message/rfc822"
    assert b"malformed" in malformed.poll(None, 10).deliveries[0].exact_bytes
    assert len(oversized.poll(None, 10).deliveries[0].exact_bytes) > 5_000_000
