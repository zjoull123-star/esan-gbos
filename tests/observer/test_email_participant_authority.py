from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from observer.email_participant_authority import (
    EmailParticipantAuthorityBinding,
    EmailParticipantAuthorityConflict,
    EmailParticipantAuthorityRecord,
    EmailParticipantAuthorityResolver,
    InMemoryEmailParticipantAuthorityRepository,
    canonical_binding_digest,
    validate_gateway_receipt_binding,
)
from observer.email_publication import header_digest
from observer.evidence_store import ContentAddressedEvidenceStore
from observer.identity_tokens import HmacSha256IdentityTokenResolver
from observer.models import TenantScope

SITE = "alpha.example"
SCOPE = TenantScope(SITE, "observation_processing")
NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
PUBLICATION = "PUB-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
MAILBOX = "MBX-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
DELIVERY = "DLV-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
INBOX = "INB-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
MESSAGE = "MSG-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
RECEIPT = "EGR-01KZQEC7B9A41Q2ZCDPFGQ7V5K"
IDENTITY_RESOLVER = HmacSha256IdentityTokenResolver(b"observer-authority-test-key-0001")


def _raw_message() -> bytes:
    message = EmailMessage()
    message["From"] = "Private Sender <sender@example.invalid>"
    message["To"] = "mailbox@example.invalid"
    message["Cc"] = "copy@example.invalid"
    message["Bcc"] = "blind@example.invalid"
    message["Subject"] = "Private subject"
    message["Message-ID"] = "<message@example.invalid>"
    message.set_content("Private body")
    return message.as_bytes()


def _identity_ref(address: str) -> str:
    return IDENTITY_RESOLVER.resolve(SITE, "observation_processing", "email", address)


def _publication_payload(*, verified_identities: bool = False) -> dict[str, object]:
    identities = (
        tuple(
            _identity_ref(address)
            for address in (
                "sender@example.invalid",
                "mailbox@example.invalid",
                "copy@example.invalid",
                "blind@example.invalid",
            )
        )
        if verified_identities
        else tuple("extid:v1:email:" + character * 43 for character in "abcd")
    )
    return {
        "publication_id": PUBLICATION,
        "site_id": SITE,
        "mailbox_id": MAILBOX,
        "mailbox_config_revision": 3,
        "observer_connector_instance_ref": "OCI-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        "observer_delivery_ref": DELIVERY,
        "received_at": "2026-08-13T10:00:00Z",
        "evidence_refs": ["EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K"],
        "participants": [
            {"address_role": role, "identity_ref": identity_ref}
            for role, identity_ref in zip(("from", "to", "cc", "bcc"), identities, strict=True)
        ],
        "subject_digest": header_digest("Private subject"),
        "header_digests": {"message_id": header_digest("<message@example.invalid>")},
        "publication_revision": 1,
        "idempotency_key": "idem:v1:" + "0" * 64,
    }


def _receipt(payload: dict[str, object], **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "gateway_receipt_ref": RECEIPT,
        "publication_ref": PUBLICATION,
        "inbox_item_ref": INBOX,
        "message_ref": MESSAGE,
        "mailbox_ref": MAILBOX,
        "mailbox_config_revision": 3,
        "observer_delivery_ref": DELIVERY,
        "payload_digest": canonical_binding_digest(payload),
        "participant_binding_digest": canonical_binding_digest(payload["participants"]),
        "evidence_binding_digest": canonical_binding_digest(payload["evidence_refs"]),
    }
    value.update(changes)
    return value


def test_canonical_binding_digest_is_stable_across_mapping_order_and_list_order_sensitive() -> None:
    assert canonical_binding_digest({"b": 2, "a": [1, 3]}) == canonical_binding_digest(
        {"a": [1, 3], "b": 2}
    )
    assert canonical_binding_digest({"a": [1, 3], "b": 2}) != canonical_binding_digest(
        {"a": [3, 1], "b": 2}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"publication_ref": "PUB-01KZQEC7B9A41Q2ZCDPFGQ7V5M"},
        {"mailbox_config_revision": True},
        {"payload_digest": "sha256:" + "1" * 64},
        {"participant_binding_digest": "sha256:" + "2" * 64},
        {"evidence_binding_digest": "sha256:" + "3" * 64},
        {"authority_valid": True},
    ],
)
def test_gateway_receipt_binding_rejects_drift_and_caller_boolean(
    change: dict[str, object],
) -> None:
    payload = _publication_payload()
    with pytest.raises(EmailParticipantAuthorityConflict):
        validate_gateway_receipt_binding(
            _receipt(payload, **change),
            publication_payload=payload,
            payload_digest=canonical_binding_digest(payload),
        )


def test_authority_binding_repr_and_repository_contain_no_raw_addresses(tmp_path: Path) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload()
    binding = validate_gateway_receipt_binding(
        _receipt(payload),
        publication_payload=payload,
        payload_digest=canonical_binding_digest(payload),
    )
    record = EmailParticipantAuthorityRecord(
        binding=binding,
        publication_payload=payload,
        delivery_id="provider-delivery-1",
        object_ref=stored.object_ref,
        exact_body_sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
        media_type="message/rfc822",
        received_at=NOW,
    )
    repository = InMemoryEmailParticipantAuthorityRepository((record,))

    rendered = repr((binding, record, repository))
    for private in (
        "sender@example.invalid",
        "mailbox@example.invalid",
        "copy@example.invalid",
        "blind@example.invalid",
    ):
        assert private not in rendered


def test_resolver_without_identity_hmac_fails_closed_after_reparse(tmp_path: Path) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload()
    binding = validate_gateway_receipt_binding(
        _receipt(payload),
        publication_payload=payload,
        payload_digest=canonical_binding_digest(payload),
    )
    repository = InMemoryEmailParticipantAuthorityRepository(
        (
            EmailParticipantAuthorityRecord(
                binding=binding,
                publication_payload=payload,
                delivery_id="provider-delivery-1",
                object_ref=stored.object_ref,
                exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                byte_size=len(raw),
                media_type="message/rfc822",
                received_at=NOW,
            ),
        )
    )
    resolver = EmailParticipantAuthorityResolver(repository=repository, store=store)

    with pytest.raises(
        EmailParticipantAuthorityConflict,
        match="participant_identity_authority_unavailable",
    ):
        resolver(
            SCOPE,
            binding,
            {"sender": "mailbox_owner", "recipients": ["original_sender", "original_cc"]},
        )
    with pytest.raises(
        EmailParticipantAuthorityConflict,
        match="participant_authority_unavailable",
    ):
        resolver(
            TenantScope("other.example", "observation_processing"),
            binding,
            {"sender": "mailbox_owner", "recipients": ["original_sender"]},
        )


def test_resolver_recomputes_all_identity_hmacs_and_uniquely_matches_mailbox_owner(
    tmp_path: Path,
) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload(verified_identities=True)
    binding = validate_gateway_receipt_binding(
        _receipt(payload),
        publication_payload=payload,
        payload_digest=canonical_binding_digest(payload),
    )
    resolver = EmailParticipantAuthorityResolver(
        repository=InMemoryEmailParticipantAuthorityRepository(
            (
                EmailParticipantAuthorityRecord(
                    binding=binding,
                    publication_payload=payload,
                    delivery_id="provider-delivery-1",
                    object_ref=stored.object_ref,
                    exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    media_type="message/rfc822",
                    received_at=NOW,
                    mailbox_address_identity_ref=_identity_ref("mailbox@example.invalid"),
                ),
            )
        ),
        store=store,
        identity_resolver=IDENTITY_RESOLVER,
    )

    resolved = resolver(
        SCOPE,
        binding,
        {"sender": "mailbox_owner", "recipients": ["original_sender", "original_cc"]},
    )

    assert resolved["from"] == "mailbox@example.invalid"
    assert resolved["to"] == ["sender@example.invalid"]
    assert resolved["cc"] == ["copy@example.invalid"]
    assert resolved["participant_projection"] == [
        {"address_role": "sender", "opaque_address_ref": _identity_ref("mailbox@example.invalid")},
        {"address_role": "to", "opaque_address_ref": _identity_ref("sender@example.invalid")},
        {"address_role": "cc", "opaque_address_ref": _identity_ref("copy@example.invalid")},
    ]
    assert "@" not in repr(resolved["participant_projection"])
    assert resolved["parsed_address_roles_digest"] == canonical_binding_digest(
        ["from", "to", "cc", "bcc"]
    )


@pytest.mark.parametrize("configured_mailbox_identity", [None, "wrong"])
def test_resolver_rejects_missing_mailbox_authority_or_raw_identity_hmac_drift(
    tmp_path: Path,
    configured_mailbox_identity: str | None,
) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload(verified_identities=True)
    if configured_mailbox_identity == "wrong":
        participants = payload["participants"]
        assert isinstance(participants, list)
        participants[0] = {
            "address_role": "from",
            "identity_ref": "extid:v1:email:" + "z" * 43,
        }
    binding = validate_gateway_receipt_binding(
        _receipt(payload),
        publication_payload=payload,
        payload_digest=canonical_binding_digest(payload),
    )
    resolver = EmailParticipantAuthorityResolver(
        repository=InMemoryEmailParticipantAuthorityRepository(
            (
                EmailParticipantAuthorityRecord(
                    binding=binding,
                    publication_payload=payload,
                    delivery_id="provider-delivery-1",
                    object_ref=stored.object_ref,
                    exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    media_type="message/rfc822",
                    received_at=NOW,
                    mailbox_address_identity_ref=(
                        None
                        if configured_mailbox_identity is None
                        else _identity_ref("mailbox@example.invalid")
                    ),
                ),
            )
        ),
        store=store,
        identity_resolver=IDENTITY_RESOLVER,
    )

    expected = (
        "mailbox_owner_authority_unavailable"
        if configured_mailbox_identity is None
        else "delivery_identity_binding_drift"
    )
    with pytest.raises(EmailParticipantAuthorityConflict, match=expected):
        resolver(
            SCOPE,
            binding,
            {"sender": "mailbox_owner", "recipients": ["original_sender"]},
        )


def test_resolver_rejects_assigned_owner_without_observer_authority(tmp_path: Path) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload(verified_identities=True)
    binding = validate_gateway_receipt_binding(
        _receipt(payload),
        publication_payload=payload,
        payload_digest=canonical_binding_digest(payload),
    )
    resolver = EmailParticipantAuthorityResolver(
        repository=InMemoryEmailParticipantAuthorityRepository(
            (
                EmailParticipantAuthorityRecord(
                    binding=binding,
                    publication_payload=payload,
                    delivery_id="provider-delivery-1",
                    object_ref=stored.object_ref,
                    exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    media_type="message/rfc822",
                    received_at=NOW,
                    mailbox_address_identity_ref=_identity_ref("mailbox@example.invalid"),
                ),
            )
        ),
        store=store,
        identity_resolver=IDENTITY_RESOLVER,
    )

    with pytest.raises(PermissionError, match="assigned owner authority"):
        resolver(
            SCOPE,
            binding,
            {"sender": "mailbox_owner", "recipients": ["assigned_owner"]},
        )


def test_resolver_fails_closed_on_cas_or_published_participant_drift(tmp_path: Path) -> None:
    raw = _raw_message()
    store = ContentAddressedEvidenceStore(tmp_path / "cas")
    stored = store.put(SCOPE, raw, media_type="message/rfc822")
    payload = _publication_payload()
    payload["participants"] = list(reversed(payload["participants"]))  # type: ignore[arg-type]
    binding = EmailParticipantAuthorityBinding.from_wire(_receipt(_publication_payload()))
    resolver = EmailParticipantAuthorityResolver(
        repository=InMemoryEmailParticipantAuthorityRepository(
            (
                EmailParticipantAuthorityRecord(
                    binding=binding,
                    publication_payload=payload,
                    delivery_id="provider-delivery-1",
                    object_ref=stored.object_ref,
                    exact_body_sha256=hashlib.sha256(raw).hexdigest(),
                    byte_size=len(raw),
                    media_type="message/rfc822",
                    received_at=NOW,
                ),
            )
        ),
        store=store,
    )

    with pytest.raises(EmailParticipantAuthorityConflict):
        resolver(
            SCOPE,
            binding,
            {"sender": "mailbox_owner", "recipients": ["original_sender"]},
        )
