"""Zero-network deterministic email provider for crash and ordering tests."""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from enum import StrEnum

from ..models import RawDelivery, _require_aware
from .email_provider import EmailProviderError, EmailProviderPollResult


class FakeEmailProviderMode(StrEnum):
    ORDERED_SUCCESS = "ordered_success"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    MALFORMED_MIME = "malformed_mime"
    OVERSIZED_ATTACHMENT = "oversized_attachment"
    CRASH_RESTART = "crash_restart"


class FakeEmailProvider:
    __slots__ = ("_attempt", "_mode", "_now")

    def __init__(self, *, mode: FakeEmailProviderMode, now: datetime) -> None:
        if not isinstance(mode, FakeEmailProviderMode):
            raise TypeError("invalid fake email provider mode")
        _require_aware(now, "now")
        self._mode = mode
        self._now = now
        self._attempt = 0

    @property
    def provider_kind(self) -> str:
        return "fake"

    def poll(self, checkpoint: str | None, limit: int) -> EmailProviderPollResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if self._mode is FakeEmailProviderMode.RATE_LIMITED:
            raise EmailProviderError("provider_rate_limited")
        if self._mode is FakeEmailProviderMode.TIMEOUT:
            raise EmailProviderError("provider_timeout")
        self._attempt += 1
        if self._mode is FakeEmailProviderMode.CRASH_RESTART and self._attempt == 1:
            raise EmailProviderError("provider_crash")
        deliveries: tuple[RawDelivery, ...]
        if self._mode is FakeEmailProviderMode.MALFORMED_MIME:
            deliveries = (
                RawDelivery(
                    "fake:malformed",
                    b"malformed\x00mime",
                    "message/rfc822",
                    self._now,
                ),
            )
        elif self._mode is FakeEmailProviderMode.OVERSIZED_ATTACHMENT:
            message = EmailMessage()
            message["From"] = "sender@example.invalid"
            message["To"] = "recipient@example.invalid"
            message.set_content("body")
            message.add_attachment(
                b"x" * 5_000_001,
                maintype="application",
                subtype="octet-stream",
                filename="oversized.bin",
            )
            deliveries = (
                RawDelivery("fake:oversized", message.as_bytes(), "message/rfc822", self._now),
            )
        else:
            first = self._delivery("fake:0001")
            second = self._delivery("fake:0002")
            if self._mode in {
                FakeEmailProviderMode.ORDERED_SUCCESS,
                FakeEmailProviderMode.CRASH_RESTART,
            }:
                deliveries = (first, second)
            elif self._mode is FakeEmailProviderMode.DUPLICATE:
                deliveries = (first, first)
            else:
                deliveries = (second, first)
        return EmailProviderPollResult(
            expected_cursor=checkpoint,
            candidate_cursor="fake:cursor:0002",
            deliveries=deliveries[:limit],
        )

    def _delivery(self, delivery_id: str) -> RawDelivery:
        message = EmailMessage()
        message["From"] = "sender@example.invalid"
        message["To"] = "recipient@example.invalid"
        message["Subject"] = "fake subject"
        message["Message-ID"] = f"<{delivery_id}@example.invalid>"
        message.set_content("fake body")
        return RawDelivery(delivery_id, message.as_bytes(), "message/rfc822", self._now)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self._mode.value!r}, network=disabled)"


__all__ = ["FakeEmailProvider", "FakeEmailProviderMode"]
