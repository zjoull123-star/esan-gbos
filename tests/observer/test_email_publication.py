from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from observer.connectors.email_delivery import EmailRawDeliveryDecoder
from observer.email_publication import build_email_publication
from observer.identity_tokens import HmacSha256IdentityTokenResolver
from observer.models import ConnectorKey, TenantScope
from observer.normalizers import EmailObservationNormalizer

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("email", "sales-mailbox")
SOURCE_REF = "obs:v1:" + "a" * 32 + ":sha256:" + "b" * 64
ROOT = Path(__file__).parents[2]


def _message() -> bytes:
    message = EmailMessage()
    message["From"] = "Private Sender <sender@example.invalid>"
    message["To"] = "to-one@example.invalid, to-two@example.invalid"
    message["Cc"] = "cc@example.invalid"
    message["Bcc"] = "bcc@example.invalid"
    message["Subject"] = "PRIVATE SUBJECT SENTINEL"
    message["Message-ID"] = "<PRIVATE-MESSAGE-ID@example.invalid>"
    message["In-Reply-To"] = "<PRIVATE-PARENT@example.invalid>"
    message["References"] = "<PRIVATE-ROOT@example.invalid> <PRIVATE-PARENT@example.invalid>"
    message.set_content("PRIVATE BODY SENTINEL")
    return message.as_bytes()


def test_publication_preserves_roles_as_opaque_refs_and_only_header_digests() -> None:
    item = EmailRawDeliveryDecoder().decode_delivery(
        _message(),
        delivery_id="delivery-001",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]
    resolver = HmacSha256IdentityTokenResolver(b"x" * 32)
    normalized = EmailObservationNormalizer(
        identity_resolver=resolver,
        site_id=SCOPE.site_id,
        purpose=SCOPE.processing_purpose,
    ).normalize(item, source_ref=SOURCE_REF)

    publication = build_email_publication(
        scope=SCOPE,
        key=KEY,
        item=item,
        normalized=normalized,
        mailbox_id="MBX-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        mailbox_config_revision=7,
        observer_delivery_ref="delivery-001",
        received_at=NOW,
        publication_revision=1,
    )

    assert [participant.address_role for participant in publication.participants] == [
        "from",
        "to",
        "to",
        "cc",
        "bcc",
    ]
    assert all(
        value.identity_ref.startswith("extid:v1:email:") for value in publication.participants
    )
    assert publication.subject_digest.startswith("sha256:")
    assert publication.message_id_digest.startswith("sha256:")
    assert publication.in_reply_to_digest.startswith("sha256:")
    assert len(publication.references_digests) == 2

    rendered = json.dumps(publication.to_payload(), sort_keys=True)
    for private in (
        "sender@example.invalid",
        "to-one@example.invalid",
        "to-two@example.invalid",
        "cc@example.invalid",
        "bcc@example.invalid",
        "PRIVATE SUBJECT SENTINEL",
        "PRIVATE-MESSAGE-ID",
        "PRIVATE-PARENT",
        "PRIVATE-ROOT",
        "PRIVATE BODY SENTINEL",
    ):
        assert private not in rendered
        assert private not in repr(publication)


def test_publication_is_bound_to_mailbox_config_delivery_and_evidence() -> None:
    item = EmailRawDeliveryDecoder().decode_delivery(
        _message(),
        delivery_id="delivery-002",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]
    normalized = EmailObservationNormalizer().normalize(item, source_ref=SOURCE_REF)

    publication = build_email_publication(
        scope=SCOPE,
        key=KEY,
        item=item,
        normalized=normalized,
        mailbox_id="MBX-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        mailbox_config_revision=9,
        observer_delivery_ref="delivery-002",
        received_at=NOW,
        publication_revision=1,
    )

    payload = publication.to_payload()
    assert payload["site_id"] == SCOPE.site_id
    assert payload["mailbox_config_revision"] == 9
    assert payload["observer_connector_instance_ref"].startswith("OCI-")
    assert payload["observer_delivery_ref"].startswith("DLV-")
    assert payload["evidence_refs"][0].startswith("EVR-")
    assert payload["idempotency_key"].startswith("idem:v1:")
    assert set(payload["header_digests"]) == {
        "message_id",
        "in_reply_to",
        "references",
    }


def test_publication_wire_validates_against_frozen_contract() -> None:
    item = EmailRawDeliveryDecoder().decode_delivery(
        _message(),
        delivery_id="delivery-schema",
        received_at=NOW,
        source_ref=SOURCE_REF,
    )[0]
    normalized = EmailObservationNormalizer().normalize(item, source_ref=SOURCE_REF)
    publication = build_email_publication(
        scope=SCOPE,
        key=KEY,
        item=item,
        normalized=normalized,
        mailbox_id="MBX-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        mailbox_config_revision=1,
        observer_delivery_ref="delivery-schema",
        received_at=NOW,
        publication_revision=1,
    )
    schema = json.loads(
        (
            ROOT / "contracts" / "email_gateway" / "email-message-publication-v1.0.schema.json"
        ).read_text()
    )

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(publication.to_wire())
