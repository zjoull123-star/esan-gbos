from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest
from observer.connectors.email_imap import (
    AttachmentEvidenceCandidate,
    EmailImapMessage,
)
from observer.connectors.whatsapp_cloud import WhatsAppRuntimeMetadata
from observer.models import ConnectorItem, EvidenceArtifact, RawDelivery
from observer.normalizers import (
    EmailImapItemAdapter,
    EmailObservationNormalizer,
    NormalizationRejected,
    WeComObservationNormalizer,
    WhatsAppObservationNormalizer,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SOURCE_REF = "obs:v1:site-partition:sha256:" + "a" * 64


def _identity_resolver() -> object:
    from observer.identity_tokens import (
        HmacSha256IdentityTokenResolver,
        IdentityTokenResolver,
    )

    resolver: IdentityTokenResolver = HmacSha256IdentityTokenResolver(b"i" * 32)
    return resolver


def test_normalized_connector_slice_exposes_explicit_bounded_adapters() -> None:
    module = importlib.import_module("observer.normalizers")

    assert module.WhatsAppObservationNormalizer
    assert module.EmailImapItemAdapter
    assert module.EmailObservationNormalizer
    assert module.WeComObservationNormalizer


def test_whatsapp_normalizer_emits_only_unresolved_identity_and_evidence_refs() -> None:
    body = "private customer body"
    phone = "971501234567"
    contact_name = "Private Person"
    item = ConnectorItem(
        provider_event_id="wamid-001",
        occurred_at=NOW,
        source_cursor="wamid-001",
        payload={
            "kind": "whatsapp_message",
            "message": {
                "id": "wamid-001",
                "timestamp": "1786095000",
                "type": "text",
                "from": phone,
                "text": {"body": body},
            },
            "raw_contacts": ({"wa_id": phone, "profile": {"name": contact_name}},),
            "runtime_metadata": WhatsAppRuntimeMetadata(
                statuses=(),
                provider_errors=(),
                provider_metadata=(),
            ),
        },
    )

    normalized = WhatsAppObservationNormalizer().normalize(
        item,
        source_ref=SOURCE_REF,
    )

    assert normalized.channel == "chat"
    assert normalized.consent_basis == "pilot_deferred_review"
    assert normalized.retention_class == "R1-operational"
    assert normalized.original_language == "und"
    assert normalized.participants[0].role == "external"
    assert normalized.participants[0].identity_ref.startswith("unresolved:delivery:")
    assert normalized.participants[0].display_name is None
    assert tuple(artifact.reference for artifact in normalized.evidence) == (SOURCE_REF,)
    assert all(artifact.content is None for artifact in normalized.evidence)
    rendered = repr((item, normalized))
    assert body not in rendered
    assert phone not in rendered
    assert contact_name not in rendered

    same = WhatsAppObservationNormalizer().normalize(
        item,
        source_ref=SOURCE_REF,
    )
    other_site_source = "obs:v1:other-site-partition:sha256:" + "a" * 64
    other_site = WhatsAppObservationNormalizer().normalize(
        item,
        source_ref=other_site_source,
    )
    assert same.participants[0].identity_ref == normalized.participants[0].identity_ref
    assert other_site.participants[0].identity_ref != normalized.participants[0].identity_ref


def test_whatsapp_normalizer_uses_validated_sender_for_stable_opaque_identity() -> None:
    phone = "15550002222"
    item = ConnectorItem(
        provider_event_id="wamid-stable-001",
        occurred_at=NOW,
        source_cursor="wamid-stable-001",
        payload={
            "kind": "whatsapp_message",
            "message": {
                "id": "wamid-stable-001",
                "timestamp": "1786095000",
                "type": "text",
                "from": phone,
                "text": {"body": "body sentinel"},
            },
            "raw_contacts": ({"wa_id": phone, "profile": {"name": "name sentinel"}},),
            "runtime_metadata": WhatsAppRuntimeMetadata(
                statuses=(), provider_errors=(), provider_metadata=()
            ),
        },
    )
    replay = ConnectorItem(
        provider_event_id="wamid-stable-002",
        occurred_at=NOW,
        source_cursor="wamid-stable-002",
        payload={
            **dict(item.payload),
            "message": {**dict(item.payload["message"]), "id": "wamid-stable-002"},
        },
    )
    normalizer = WhatsAppObservationNormalizer(
        identity_resolver=_identity_resolver(),
        site_id="gbos.localhost",
        purpose="observation_processing",
    )

    first = normalizer.normalize(item, source_ref=SOURCE_REF)
    second = normalizer.normalize(replay, source_ref=SOURCE_REF)

    assert first.participants[0].identity_ref == second.participants[0].identity_ref
    assert first.participants[0].identity_ref.startswith("extid:v1:whatsapp:")
    assert phone not in repr(first)
    assert first.participants[0].display_name is None


def test_whatsapp_normalizer_quarantines_malformed_or_ambiguous_subjects() -> None:
    sentinel = "PII-SENTINEL"
    base = {
        "kind": "whatsapp_message",
        "message": {
            "id": "wamid-invalid",
            "timestamp": "1786095000",
            "type": "text",
            "from": "15550002222",
            "text": {"body": sentinel},
        },
        "raw_contacts": ({"wa_id": "15550009999", "profile": {"name": sentinel}},),
        "runtime_metadata": WhatsAppRuntimeMetadata(
            statuses=(), provider_errors=(), provider_metadata=()
        ),
    }
    item = ConnectorItem(
        provider_event_id="wamid-invalid",
        occurred_at=NOW,
        source_cursor="wamid-invalid",
        payload=base,
    )

    with pytest.raises(NormalizationRejected, match="whatsapp.ambiguous_sender") as captured:
        WhatsAppObservationNormalizer(
            identity_resolver=_identity_resolver(),
            site_id="gbos.localhost",
            purpose="observation_processing",
        ).normalize(item, source_ref=SOURCE_REF)
    assert sentinel not in repr(captured.value)


def test_wecom_normalizer_requires_closed_adapter_shape_and_preserves_only_refs() -> None:
    decrypted_ref = "obs:v1:site-partition:sha256:" + "b" * 64
    item = ConnectorItem(
        provider_event_id="wecom-msg-001",
        occurred_at=NOW,
        source_cursor="42",
        payload={
            "message_type": "text",
            "decrypted_content_ref": decrypted_ref,
            "media_pending": False,
            "identity_subjects": (),
        },
    )

    normalized = WeComObservationNormalizer().normalize(
        item,
        source_ref=SOURCE_REF,
    )

    assert normalized.channel == "chat"
    assert tuple(artifact.reference for artifact in normalized.evidence) == (
        SOURCE_REF,
        decrypted_ref,
    )
    assert normalized.participants[0].role == "unknown"
    assert normalized.participants[0].identity_ref.startswith("unresolved:delivery:")
    assert normalized.retention_class == "R1-operational"

    secret = "private-decrypted-body"
    malformed = ConnectorItem(
        provider_event_id="wecom-msg-002",
        occurred_at=NOW,
        source_cursor="43",
        payload={
            "message_type": "text",
            "decrypted_content_ref": decrypted_ref,
            "media_pending": False,
            "identity_subjects": (),
            "body": secret,
        },
    )
    with pytest.raises(NormalizationRejected, match="wecom.invalid_adapter_shape") as caught:
        WeComObservationNormalizer().normalize(malformed, source_ref=SOURCE_REF)
    assert secret not in repr(caught.value)

    media_pending = ConnectorItem(
        provider_event_id="wecom-msg-media",
        occurred_at=NOW,
        source_cursor="44",
        payload={
            "message_type": "voice",
            "decrypted_content_ref": decrypted_ref,
            "media_pending": True,
            "identity_subjects": (),
        },
    )
    with pytest.raises(NormalizationRejected, match="wecom.media_pending"):
        WeComObservationNormalizer().normalize(
            media_pending,
            source_ref=SOURCE_REF,
        )


def test_email_and_wecom_normalizers_emit_only_opaque_deduplicated_participants() -> None:
    from observer.identity_tokens import TransientIdentitySubject

    email_sentinel = "PII-SENTINEL@Example.INVALID"
    email_item = ConnectorItem(
        provider_event_id="imap:uidvalidity-7:uid-identity",
        occurred_at=NOW,
        source_cursor="imap:uidvalidity-7:uid-identity",
        payload={
            "kind": "email_raw_delivery",
            "source_ref": SOURCE_REF,
            "body_evidence": EvidenceArtifact(
                media_type="text/plain; charset=utf-8",
                locator="message-body",
                role="derived-text",
                content=b"body without address",
            ),
            "attachment_evidence": (),
            "identity_subjects": (
                TransientIdentitySubject(provider="email", subject=email_sentinel),
                TransientIdentitySubject(provider="email", subject=email_sentinel.lower()),
            ),
        },
    )
    resolver = _identity_resolver()
    email = EmailObservationNormalizer(
        identity_resolver=resolver,
        site_id="gbos.localhost",
        purpose="observation_processing",
    ).normalize(email_item, source_ref=SOURCE_REF)

    assert len(email.participants) == 1
    assert email.participants[0].identity_ref.startswith("extid:v1:email:")
    assert email_sentinel not in repr(email)
    assert email.participants[0].display_name is None

    wecom_sentinel = "wmCaseSensitive_001"
    wecom_item = ConnectorItem(
        provider_event_id="wecom-msg-identity",
        occurred_at=NOW,
        source_cursor="45",
        payload={
            "message_type": "text",
            "decrypted_content_ref": SOURCE_REF,
            "media_pending": False,
            "identity_subjects": (
                TransientIdentitySubject(provider="wecom", subject=wecom_sentinel),
                TransientIdentitySubject(provider="wecom", subject=wecom_sentinel),
            ),
        },
    )
    wecom = WeComObservationNormalizer(
        identity_resolver=resolver,
        site_id="gbos.localhost",
        purpose="observation_processing",
    ).normalize(wecom_item, source_ref=SOURCE_REF)

    assert len(wecom.participants) == 1
    assert wecom.participants[0].identity_ref.startswith("extid:v1:wecom:")
    assert wecom_sentinel not in repr(wecom)


def test_configured_normalizer_keeps_absent_subject_delivery_scoped() -> None:
    item = ConnectorItem(
        provider_event_id="wecom-msg-no-subject",
        occurred_at=NOW,
        source_cursor="46",
        payload={
            "message_type": "text",
            "decrypted_content_ref": SOURCE_REF,
            "media_pending": False,
            "identity_subjects": (),
        },
    )

    normalized = WeComObservationNormalizer(
        identity_resolver=_identity_resolver(),
        site_id="gbos.localhost",
        purpose="observation_processing",
    ).normalize(item, source_ref=SOURCE_REF)

    assert normalized.participants[0].identity_ref.startswith("unresolved:delivery:")


def test_email_adapter_fails_closed_and_never_copies_message_or_attachment_bytes() -> None:
    message_body = b"From: private@example.test\r\n\r\nsecret body"
    attachment_bytes = b"private attachment"
    message = EmailImapMessage(
        uid=7,
        provider_event_id="email-event-001",
        checkpoint_candidate='{"mailbox":"INBOX","uid":7,"uidvalidity":1,"version":1}',
        raw_delivery=RawDelivery(
            "email-event-001",
            message_body,
            "message/rfc822",
            NOW,
        ),
        message_id="<private@example.test>",
        evidence_candidates=(
            AttachmentEvidenceCandidate(
                part_index=1,
                media_type="application/pdf",
                filename="private.pdf",
                content=attachment_bytes,
            ),
        ),
        attachment_status="ready",
        attachment_error_code=None,
    )
    attachment_ref = "obs:v1:site-partition:sha256:" + "c" * 64

    item = EmailImapItemAdapter().adapt(
        message,
        source_ref=SOURCE_REF,
        attachment_refs=(attachment_ref,),
    )
    normalized = EmailObservationNormalizer().normalize(
        item,
        source_ref=SOURCE_REF,
    )

    assert item.payload == {
        "kind": "email_imap_message",
        "source_ref": SOURCE_REF,
        "attachment_refs": (attachment_ref,),
    }
    assert tuple(artifact.reference for artifact in normalized.evidence) == (
        SOURCE_REF,
        attachment_ref,
    )
    assert normalized.channel == "email"
    assert normalized.participants[0].role == "unknown"
    assert normalized.retention_class == "R1-operational"
    rendered = repr((message, item, normalized))
    assert "secret body" not in rendered
    assert "private attachment" not in rendered
    assert "private@example.test" not in rendered

    with pytest.raises(NormalizationRejected, match="email.attachment_ref_mismatch"):
        EmailImapItemAdapter().adapt(
            message,
            source_ref=SOURCE_REF,
            attachment_refs=(),
        )


def test_email_normalizer_preserves_transient_body_and_attachment_evidence_for_sink() -> None:
    body = b"private email body"
    attachment = b"private attachment"
    item = ConnectorItem(
        provider_event_id="imap:uidvalidity-7:uid-42",
        occurred_at=NOW,
        source_cursor="imap:uidvalidity-7:uid-42",
        payload={
            "kind": "email_raw_delivery",
            "source_ref": SOURCE_REF,
            "body_evidence": EvidenceArtifact(
                media_type="text/plain; charset=utf-8",
                locator="message-body",
                role="derived-text",
                content=body,
            ),
            "attachment_evidence": (
                EvidenceArtifact(
                    media_type="application/octet-stream",
                    locator="attachment:1",
                    role="attachment",
                    content=attachment,
                ),
            ),
            "identity_subjects": (),
        },
    )

    normalized = EmailObservationNormalizer().normalize(item, source_ref=SOURCE_REF)

    assert tuple(artifact.reference for artifact in normalized.evidence) == (
        SOURCE_REF,
        None,
        None,
    )
    assert normalized.evidence[1].content == body
    assert normalized.evidence[2].content == attachment
    assert body.decode() not in repr(item)
    assert attachment.decode() not in repr(item)


def test_normalizers_reject_unbounded_or_cross_adapter_payloads_with_safe_codes() -> None:
    huge_contacts = tuple({"wa_id": str(index)} for index in range(65))
    oversized = ConnectorItem(
        provider_event_id="wamid-oversized",
        occurred_at=NOW,
        source_cursor="wamid-oversized",
        payload={
            "kind": "whatsapp_message",
            "message": {
                "id": "wamid-oversized",
                "timestamp": "1786095000",
                "type": "text",
                "text": {"body": "do-not-log"},
            },
            "raw_contacts": huge_contacts,
            "runtime_metadata": WhatsAppRuntimeMetadata(
                statuses=(),
                provider_errors=(),
                provider_metadata=(),
            ),
        },
    )

    with pytest.raises(NormalizationRejected, match="whatsapp.contacts_limit") as caught:
        WhatsAppObservationNormalizer(max_contacts=64).normalize(
            oversized,
            source_ref=SOURCE_REF,
        )
    assert "do-not-log" not in repr(caught.value)

    with pytest.raises(NormalizationRejected, match="email.invalid_adapter_shape"):
        EmailObservationNormalizer().normalize(oversized, source_ref=SOURCE_REF)

    media_pending = ConnectorItem(
        provider_event_id="wamid-media",
        occurred_at=NOW,
        source_cursor="wamid-media",
        payload={
            "kind": "whatsapp_message",
            "message": {
                "id": "wamid-media",
                "timestamp": "1786095000",
                "type": "audio",
                "audio": {"id": "media-001", "mime_type": "audio/ogg"},
            },
            "raw_contacts": (),
            "runtime_metadata": WhatsAppRuntimeMetadata(
                statuses=(),
                provider_errors=(),
                provider_metadata=(),
            ),
            "media_download_task": object(),
        },
    )
    with pytest.raises(NormalizationRejected, match="whatsapp.media_pending"):
        WhatsAppObservationNormalizer().normalize(
            media_pending,
            source_ref=SOURCE_REF,
        )
