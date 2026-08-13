from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from .conftest import DIGEST_A, NOW, OPAQUE_FROM, SITE


def test_gateway_package_and_closed_models_are_available(mailbox, publication) -> None:
    from services.email_gateway import models

    assert models.Mailbox is type(mailbox)
    assert models.EmailMessagePublication is type(publication)


def test_models_are_immutable_and_reject_unknown_enums(mailbox) -> None:
    from services.email_gateway.models import Mailbox, ValidationError

    with pytest.raises(FrozenInstanceError):
        mailbox.status = "paused"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="provider"):
        Mailbox(**{**mailbox.to_wire(), "provider": "smtp_magic"})
    with pytest.raises(ValidationError, match="status"):
        replace(mailbox, status="enabled")


def test_participants_reject_raw_address_and_duplicates(publication) -> None:
    from services.email_gateway.models import PublicationParticipant, ValidationError

    with pytest.raises(ValidationError, match="opaque"):
        PublicationParticipant(role="from", identity_ref="customer@example.invalid")
    duplicate = PublicationParticipant(role="from", identity_ref=OPAQUE_FROM)
    with pytest.raises(ValidationError, match="duplicate"):
        replace(publication, participants=(duplicate, duplicate))


def test_repr_redacts_restricted_values(mailbox, publication) -> None:
    from services.email_gateway.models import Draft, IdentityProjection

    projection = IdentityProjection(
        site_id=SITE,
        processing_purpose="sales_follow_up",
        opaque_address_ref=OPAQUE_FROM,
        external_identity_ref="EXT-01",
        external_identity_revision=2,
        identity_type="Party",
        team_ref="TEM-01",
        status="confirmed",
        projection_receipt_ref="IPR-01",
        observed_at=NOW,
        payload_digest=DIGEST_A,
    )
    draft = Draft(
        draft_ref="DRF-01",
        site_id=SITE,
        inbox_item_ref="INB-01",
        conversation_ref=None,
        content_evidence_ref="EVD-DRAFT-01",
        content_digest=DIGEST_A,
        revision=1,
        state="editable",
        updated_at=NOW,
    )
    rendered = repr((mailbox, publication, projection, draft))
    for forbidden in (
        "primary@company.invalid",
        "Restricted subject",
        OPAQUE_FROM,
        "EXT-01",
        "EVD-DRAFT-01",
    ):
        assert forbidden not in rendered


def test_mailbox_identity_ref_is_optional_only_for_legacy_read_and_redacted(mailbox) -> None:
    from services.email_gateway.models import ValidationError

    identity_ref = "extid:v1:email:" + "M" * 43
    legacy = replace(mailbox, mailbox_address_identity_ref=None)
    current = replace(mailbox, mailbox_address_identity_ref=identity_ref)

    assert legacy.mailbox_address_identity_ref is None
    assert current.to_wire()["mailbox_address_identity_ref"] == identity_ref
    assert identity_ref not in repr(current)
    with pytest.raises(ValidationError, match="mailbox address identity"):
        replace(mailbox, mailbox_address_identity_ref="mailbox@example.invalid")


def test_wire_factories_reject_extra_fields_and_cross_site(mailbox) -> None:
    from services.email_gateway.models import Mailbox, ValidationError

    wire = mailbox.to_wire()
    with pytest.raises(ValidationError, match="unknown"):
        Mailbox.from_wire({**wire, "raw_password": "never"})
    with pytest.raises(ValidationError, match="site"):
        Mailbox.from_wire({**wire, "site_id": "other.local"}, expected_site_id=SITE)


def test_publication_wire_is_exact_frozen_contract() -> None:
    from services.email_gateway.models import EmailMessagePublication, ValidationError

    examples = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "email_gateway"
            / "examples"
            / "provider-neutral-v1.json"
        ).read_text()
    )
    wire = examples["cases"]["email-message-publication-v1.0.schema.json"]["valid"][
        "subject_projection"
    ]
    publication = EmailMessagePublication.from_wire(
        wire,
        processing_purpose="sales_follow_up",
        payload_digest="sha256:" + "f" * 64,
    )
    assert publication.to_wire() == wire
    assert publication.processing_purpose == "sales_follow_up"
    with pytest.raises(ValidationError, match="unknown"):
        EmailMessagePublication.from_wire(
            {**wire, "processing_purpose": "sales_follow_up"},
            processing_purpose="sales_follow_up",
            payload_digest="sha256:" + "f" * 64,
        )


def test_publication_requires_exactly_one_subject_representation(publication) -> None:
    from services.email_gateway.models import ValidationError

    with pytest.raises(ValidationError, match="subject"):
        replace(publication, subject_projection=None, subject_digest=None)
    with pytest.raises(ValidationError, match="subject"):
        replace(publication, subject_projection="safe", subject_digest=DIGEST_A)


def test_stable_refs_use_contract_ulids_not_hex_placeholders() -> None:
    from services.email_gateway.models import stable_ref

    assert stable_ref("MSG", "site.local", "message").startswith("MSG-")
    assert len(stable_ref("MSG", "site.local", "message")) == 30
    assert all(
        character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        for character in stable_ref("MSG", "site.local", "message")[4:]
    )


def test_workflow_value_objects_reject_invalid_state_revision_and_mutable_aliases() -> None:
    from services.email_gateway.models import (
        Conversation,
        RouteDecision,
        RoutingRule,
        SendOutbox,
        ThreadSuggestion,
        ValidationError,
    )

    with pytest.raises(ValidationError):
        RouteDecision(
            "RTE-01",
            SITE,
            "INB-01",
            "MBX-01",
            "assigned",
            "TEM-01",
            None,
            None,
            None,
            None,
            "owner_unavailable",
            NOW,
        )
    with pytest.raises(ValidationError):
        RoutingRule("RUL-01", SITE, "TEM-01", "MBX-01", "owner", 1001, 1, True)
    with pytest.raises(ValidationError):
        ThreadSuggestion(
            "SGG-01",
            SITE,
            "TEM-01",
            "INB-01",
            "INB-01",
            ("digest",),
            0.5,
            "proposed",
            1,
            None,
            None,
            NOW,
        )
    with pytest.raises(ValidationError):
        Conversation(
            "CON-01",
            SITE,
            "TEM-01",
            None,
            None,
            None,
            "open",
            NOW,
            NOW,
            ["MSG-01"],  # type: ignore[arg-type]
            ("INB-01",),
            1,
        )
    with pytest.raises(ValidationError):
        SendOutbox("SND-01", SITE, "queued", DIGEST_A)


def test_mailbox_connector_projection_builds_exact_closed_wire_and_digest() -> None:
    import jsonschema

    from services.email_gateway.models import MailboxConnectorProjection, canonical_digest

    projection = MailboxConnectorProjection(
        site_id=SITE,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        provider_kind="imap_smtp",
        entry_role="primary",
        business_purpose="sales_follow_up",
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email/primary",
        inbound_enabled=True,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_config_revision=2,
        activation_not_before=NOW,
        projection_revision=2,
        mailbox_address_identity_ref="extid:v1:email:" + "M" * 43,
    )

    wire = projection.to_wire()

    assert set(wire) == {
        "site_id",
        "observer_connector_instance_ref",
        "provider_kind",
        "entry_role",
        "business_purpose",
        "team_ref",
        "credential_ref",
        "inbound_enabled",
        "mailbox_address_identity_ref",
        "activation_watermark",
        "projection_revision",
        "projection_digest",
    }
    assert wire["activation_watermark"] == {
        "mailbox_id": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 2,
        "not_before": "2026-08-13T01:02:03Z",
    }
    digest_input = dict(wire)
    digest_input.pop("projection_digest")
    assert wire["projection_digest"] == canonical_digest(digest_input)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "email_gateway"
            / "mailbox-connector-projection-v2.0.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(wire)


def test_mailbox_connector_projection_rejects_fake_and_naive_watermark() -> None:
    from services.email_gateway.models import MailboxConnectorProjection, ValidationError

    values = {
        "site_id": SITE,
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "provider_kind": "fake",
        "entry_role": "primary",
        "business_purpose": "sales_follow_up",
        "team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "credential_ref": "secretref:v1/email/primary",
        "inbound_enabled": True,
        "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 1,
        "activation_not_before": NOW,
        "projection_revision": 1,
        "mailbox_address_identity_ref": "extid:v1:email:" + "M" * 43,
    }
    with pytest.raises(ValidationError, match="provider"):
        MailboxConnectorProjection(**values)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="activation"):
        MailboxConnectorProjection(
            **{
                **values,
                "provider_kind": "wecom_app_mail",
                "activation_not_before": NOW.replace(tzinfo=None),
            }  # type: ignore[arg-type]
        )


def test_legacy_mailbox_projection_remains_exact_v1() -> None:
    from services.email_gateway.models import MailboxConnectorProjection

    projection = MailboxConnectorProjection(
        site_id=SITE,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        provider_kind="imap_smtp",
        entry_role="primary",
        business_purpose="sales_follow_up",
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email/primary",
        inbound_enabled=True,
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_config_revision=1,
        activation_not_before=NOW,
        projection_revision=1,
    )

    assert "mailbox_address_identity_ref" not in projection.to_wire()
