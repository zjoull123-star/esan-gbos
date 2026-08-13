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


def test_decoder_emits_redacted_sender_and_bounded_deduplicated_recipients() -> None:
    from services.observer.observer.identity_tokens import TransientIdentitySubject

    message = EmailMessage()
    message["From"] = "Private Sender < Sender@Example.INVALID >"
    message["To"] = "first@example.invalid, FIRST@example.invalid"
    message["Cc"] = "Second Person <second@example.invalid>"
    message["Message-ID"] = "<identity@example.invalid>"
    message.set_content("private body")

    item = EmailRawDeliveryDecoder(max_identity_recipients=3).decode_delivery(
        message.as_bytes(),
        delivery_id="imap:uidvalidity-7:uid-identity",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]

    subjects = item.payload["identity_subjects"]
    assert subjects == (
        TransientIdentitySubject(provider="email", subject="Sender@Example.INVALID"),
        TransientIdentitySubject(provider="email", subject="first@example.invalid"),
        TransientIdentitySubject(provider="email", subject="second@example.invalid"),
    )
    assert "Sender@Example.INVALID" not in repr(subjects)
    assert "first@example.invalid" not in repr(subjects)


def test_decoder_preserves_exact_address_header_roles_and_private_header_digests() -> None:
    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "to@example.invalid"
    message["Cc"] = "cc@example.invalid"
    message["Bcc"] = "bcc@example.invalid"
    message["Subject"] = "PRIVATE SUBJECT ROLE TEST"
    message["Message-ID"] = "<PRIVATE-MESSAGE@example.invalid>"
    message["In-Reply-To"] = "<PRIVATE-PARENT@example.invalid>"
    message["References"] = "<PRIVATE-ROOT@example.invalid> <PRIVATE-PARENT@example.invalid>"
    message.set_content("private body")

    item = EmailRawDeliveryDecoder().decode_delivery(
        message.as_bytes(),
        delivery_id="imap:uidvalidity-7:uid-role",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]

    role_subjects = item.payload["email_participant_subjects"]
    assert tuple(value.address_role for value in role_subjects) == (
        "from",
        "to",
        "cc",
        "bcc",
    )
    header_facts = item.payload["email_header_facts"]
    assert header_facts.subject_digest.startswith("sha256:")
    assert header_facts.message_id_digest.startswith("sha256:")
    assert header_facts.in_reply_to_digest.startswith("sha256:")
    assert len(header_facts.references_digests) == 2
    rendered = repr((item, role_subjects, header_facts))
    for private in (
        "sender@example.invalid",
        "to@example.invalid",
        "cc@example.invalid",
        "bcc@example.invalid",
        "PRIVATE SUBJECT ROLE TEST",
        "PRIVATE-MESSAGE",
        "PRIVATE-PARENT",
        "PRIVATE-ROOT",
    ):
        assert private not in rendered


def test_decoder_quarantines_ambiguous_or_oversized_address_headers_safely() -> None:
    ambiguous = EmailMessage()
    ambiguous["From"] = "first@example.invalid, PII-SENTINEL@example.invalid"
    ambiguous["To"] = "recipient@example.invalid"
    ambiguous.set_content("body")

    with pytest.raises(DeliveryQuarantine) as captured:
        EmailRawDeliveryDecoder().decode_delivery(
            ambiguous.as_bytes(),
            delivery_id="imap:uidvalidity-7:uid-ambiguous",
            received_at=NOW,
            source_ref=SOURCE_REF,
        )
    assert str(captured.value) == "email.ambiguous_sender"
    assert "PII-SENTINEL" not in repr(captured.value)

    too_many = EmailMessage()
    too_many["From"] = "sender@example.invalid"
    too_many["To"] = "one@example.invalid, two@example.invalid"
    too_many.set_content("body")
    with pytest.raises(DeliveryQuarantine, match="email.recipient_limit"):
        EmailRawDeliveryDecoder(max_identity_recipients=1).decode_delivery(
            too_many.as_bytes(),
            delivery_id="imap:uidvalidity-7:uid-too-many",
            received_at=NOW,
            source_ref=SOURCE_REF,
        )


def test_decoder_requires_persisted_delivery_metadata() -> None:
    with pytest.raises(DeliveryQuarantine, match="email.delivery_metadata_required"):
        EmailRawDeliveryDecoder().decode(_message())


def test_decoder_quarantines_message_with_empty_publication_header_digests() -> None:
    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "recipient@example.invalid"
    message["Subject"] = "bounded subject"
    message.set_content("body")

    with pytest.raises(DeliveryQuarantine, match="email.thread_headers_missing"):
        EmailRawDeliveryDecoder().decode_delivery(
            message.as_bytes(),
            delivery_id="imap:uidvalidity-7:uid-no-thread-headers",
            received_at=NOW,
            source_ref=SOURCE_REF,
        )


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
    message["Message-ID"] = "<html-only@example.invalid>"
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
