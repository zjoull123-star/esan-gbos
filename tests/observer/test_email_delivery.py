from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from services.observer.observer.connectors.email_delivery import (
    EmailRawDeliveryDecoder,
)
from services.observer.observer.local_pilot_ingestion import DeliveryQuarantine
from services.observer.observer.models import EvidenceArtifact

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SOURCE_REF = "obs:v1:partition:sha256:" + "a" * 64


def _message(*, message_id: str = "<duplicate@example.invalid>") -> bytes:
    message = EmailMessage()
    message["From"] = "private@example.invalid"
    message["To"] = "pilot@example.invalid"
    message["Subject"] = "private subject"
    message["Message-ID"] = message_id
    message.set_content("private plain body")
    message.add_alternative(
        "<html><body><p>private html body</p></body></html>",
        subtype="html",
    )
    message.add_attachment(
        b"private attachment",
        maintype="application",
        subtype="octet-stream",
        filename="private.bin",
    )
    return message.as_bytes()


def test_decoder_binds_provider_event_to_durable_delivery_id_not_message_id() -> None:
    decoder = EmailRawDeliveryDecoder()

    first = decoder.decode_delivery(
        _message(),
        delivery_id="imap:uidvalidity-7:uid-41",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )
    second = decoder.decode_delivery(
        _message(),
        delivery_id="imap:uidvalidity-7:uid-42",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )

    assert first[0].provider_event_id == "imap:uidvalidity-7:uid-41"
    assert second[0].provider_event_id == "imap:uidvalidity-7:uid-42"
    assert first[0].provider_event_id != second[0].provider_event_id
    payload = first[0].payload
    assert payload["source_ref"] == SOURCE_REF
    body = payload["body_evidence"]
    attachments = payload["attachment_evidence"]
    assert isinstance(body, EvidenceArtifact)
    assert body.media_type == "text/plain; charset=utf-8"
    assert body.content == b"private plain body\n"
    assert isinstance(attachments, tuple)
    assert attachments[0].content == b"private attachment"
    rendered = repr(first[0])
    assert "private plain body" not in rendered
    assert "private attachment" not in rendered
    assert "private@example.invalid" not in rendered


def test_decoder_requires_persisted_delivery_metadata() -> None:
    with pytest.raises(DeliveryQuarantine, match="email.delivery_metadata_required"):
        EmailRawDeliveryDecoder().decode(_message())


@pytest.mark.parametrize(
    ("decoder", "raw", "code"),
    [
        (
            EmailRawDeliveryDecoder(max_message_bytes=64),
            _message(),
            "email.message_too_large",
        ),
        (
            EmailRawDeliveryDecoder(max_parts=2),
            _message(),
            "email.mime_part_limit",
        ),
        (
            EmailRawDeliveryDecoder(max_attachment_bytes=4),
            _message(),
            "email.attachment_too_large",
        ),
        (
            EmailRawDeliveryDecoder(max_text_bytes=4),
            _message(),
            "email.text_too_large",
        ),
    ],
)
def test_decoder_quarantines_mime_bombs_without_partial_output(
    decoder: EmailRawDeliveryDecoder,
    raw: bytes,
    code: str,
) -> None:
    with pytest.raises(DeliveryQuarantine, match=code):
        decoder.decode_delivery(
            raw,
            delivery_id="imap:uidvalidity-7:uid-41",
            received_at=NOW,
            source_ref=SOURCE_REF,
        )


def test_html_only_body_is_converted_to_bounded_text() -> None:
    message = EmailMessage()
    message["From"] = "private@example.invalid"
    message["To"] = "pilot@example.invalid"
    message.set_content(
        "<html><body><p>Hello&nbsp;<strong>pilot</strong></p>"
        "<script>do-not-copy()</script></body></html>",
        subtype="html",
    )

    item = EmailRawDeliveryDecoder().decode_delivery(
        message.as_bytes(),
        delivery_id="imap:uidvalidity-7:uid-43",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]

    body = item.payload["body_evidence"]
    assert isinstance(body, EvidenceArtifact)
    assert body.content == b"Hello pilot"
    assert b"do-not-copy" not in body.content
