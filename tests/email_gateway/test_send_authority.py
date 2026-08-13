from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.email_gateway.email_send_authority import (
    EmailSendAuthority,
    EmailSendAuthorityConflict,
)
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.models import (
    Conversation,
    Draft,
    IdentityProjection,
    InboxItem,
    Mailbox,
    TenantScope,
    canonical_digest,
)
from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository
from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository
from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository
from services.email_gateway.security import GatewayAuthorizationIssuer

SITE = "alpha.example"
TEAM = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAILBOX = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
INBOX = "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MESSAGE = "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV"
CONVERSATION = "CNV-01ARZ3NDEKTSV4RRFFQ69G5FAV"
DRAFT = "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV"
PARTY = "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAPPING = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
ACTOR = "owner@example.invalid"
ACTOR_REF = "USR-6KFQEGWASP2R8CH6JW22BYMY2E"
NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
SCOPE = TenantScope(SITE, "sales_follow_up")
ROLES = {"sender": "mailbox_owner", "recipients": ["original_sender"]}
ROLES_DIGEST = canonical_digest(ROLES)
SENDER = "extid:v1:email:" + "a" * 43
RECIPIENT = "extid:v1:email:" + "b" * 43


class _BindingReader:
    def load_participant_authority_binding(
        self, scope: TenantScope, *, inbox_item_ref: str
    ) -> dict[str, object] | None:
        if scope != SCOPE or inbox_item_ref != INBOX:
            return None
        return {
            "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "inbox_item_ref": INBOX,
            "message_ref": MESSAGE,
            "mailbox_ref": MAILBOX,
            "mailbox_config_revision": 3,
            "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "payload_digest": "sha256:" + "1" * 64,
            "participant_binding_digest": "sha256:" + "2" * 64,
            "evidence_binding_digest": "sha256:" + "3" * 64,
        }


def _service() -> tuple[EmailSendAuthority, InMemoryWorkflowRepository]:
    mailbox_repository = InMemoryMailboxRepository()
    mailbox_repository._mailboxes[(SITE, MAILBOX)] = Mailbox(  # noqa: SLF001
        mailbox_ref=MAILBOX,
        site_id=SITE,
        address_display="Sales",
        provider="fake",
        provider_account_ref="provider-account",
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        entry_role="primary",
        business_purpose="sales_follow_up",
        default_team_ref=TEAM,
        account_owner_user_ref=ACTOR,
        priority=1,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="secretref:v1/email-sales",
        status="active",
        config_revision=3,
        observer_config_projection_receipt=None,
        mailbox_address_identity_ref=SENDER,
    )
    workflow = InMemoryWorkflowRepository()
    workflow.save_inbox(
        SCOPE,
        InboxItem(
            inbox_item_ref=INBOX,
            site_id=SITE,
            mailbox_ref=MAILBOX,
            message_ref=MESSAGE,
            team_ref=TEAM,
            assignee_user_ref=ACTOR,
            priority=1,
            sla_due_at=None,
            state="assigned",
            conversation_ref=CONVERSATION,
            business_links=(),
            revision=4,
            received_at=NOW,
            updated_at=NOW,
        ),
    )
    workflow.save_conversation(
        SCOPE,
        Conversation(
            conversation_ref=CONVERSATION,
            site_id=SITE,
            team_ref=TEAM,
            party_ref=PARTY,
            contact_ref=None,
            owner_user_ref=ACTOR,
            lifecycle_state="open",
            first_message_at=NOW,
            last_message_at=NOW,
            message_refs=(MESSAGE,),
            inbox_item_refs=(INBOX,),
            revision=2,
        ),
    )
    workflow.save_draft(
        SCOPE,
        Draft(
            draft_ref=DRAFT,
            site_id=SITE,
            inbox_item_ref=INBOX,
            conversation_ref=CONVERSATION,
            content_evidence_ref="obs:v1:" + "a" * 32 + ":sha256:" + "b" * 64,
            content_digest="sha256:" + "4" * 64,
            revision=3,
            state="editable",
            updated_at=NOW,
        ),
        idempotency_key="draft-seed",
        payload_digest="sha256:" + "5" * 64,
    )
    identities = InMemoryIdentityProjectionRepository()
    identities.apply(
        SCOPE,
        IdentityProjection(
            site_id=SITE,
            processing_purpose=SCOPE.processing_purpose,
            opaque_address_ref=RECIPIENT,
            external_identity_ref=MAPPING,
            external_identity_revision=7,
            identity_type="Party",
            team_ref=TEAM,
            status="confirmed",
            projection_receipt_ref="IPR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            observed_at=NOW,
            payload_digest="sha256:" + "6" * 64,
        ),
    )
    return (
        EmailSendAuthority(
            mailboxes=MailboxRegistry(mailbox_repository),
            workflow=workflow,
            identities=identities,
            binding_reader=_BindingReader(),
            authorization_issuer=GatewayAuthorizationIssuer(clock=lambda: NOW),
        ),
        workflow,
    )


def test_authorize_uses_current_gateway_state_and_returns_no_raw_participants() -> None:
    service, _workflow = _service()

    result = service.authorize(
        SCOPE,
        actor_ref=ACTOR,
        inbox_item_ref=INBOX,
        draft_ref=DRAFT,
        expected_inbox_revision=4,
        expected_draft_revision=3,
        participant_roles_digest=ROLES_DIGEST,
    )

    assert set(result) == {"gateway_snapshot", "draft_authorization", "draft_evidence_ref"}
    snapshot = result["gateway_snapshot"]
    assert snapshot["party_ref"] == PARTY
    assert snapshot["owner_user_name"] == snapshot["assignee_user_name"] == ACTOR
    assert snapshot["reply_draft_digest"] == "sha256:" + "4" * 64
    assert result["draft_evidence_ref"].startswith("obs:v1:")
    authorization = result["draft_authorization"]
    assert authorization["participant_roles_digest"] == ROLES_DIGEST
    assert authorization["mailbox_config_revision"] == 3
    assert "participants" not in result


def test_authorize_rejects_any_nonfrozen_participant_role_binding() -> None:
    service, _workflow = _service()

    with pytest.raises(EmailSendAuthorityConflict, match="participant_roles_invalid"):
        service.authorize(
            SCOPE,
            actor_ref=ACTOR,
            inbox_item_ref=INBOX,
            draft_ref=DRAFT,
            expected_inbox_revision=4,
            expected_draft_revision=3,
            participant_roles_digest="sha256:" + "9" * 64,
        )


def test_validate_maps_only_current_confirmed_party_recipient_and_rejects_drift() -> None:
    service, workflow = _service()
    authorized = service.authorize(
        SCOPE,
        actor_ref=ACTOR,
        inbox_item_ref=INBOX,
        draft_ref=DRAFT,
        expected_inbox_revision=4,
        expected_draft_revision=3,
        participant_roles_digest=ROLES_DIGEST,
    )

    result = service.validate(
        SCOPE,
        actor_ref=ACTOR,
        expected_gateway_snapshot=authorized["gateway_snapshot"],
        participant_projection=[
            {"address_role": "sender", "opaque_address_ref": SENDER},
            {"address_role": "to", "opaque_address_ref": RECIPIENT},
        ],
    )

    assert result["gateway_snapshot"] == authorized["gateway_snapshot"]
    assert result["participants"] == [
        {"address_role": "sender", "opaque_address_ref": SENDER},
        {
            "address_role": "to",
            "opaque_address_ref": RECIPIENT,
            "identity_mapping_ref": MAPPING,
            "identity_mapping_revision": 7,
        },
    ]
    current = workflow.get_inbox(SCOPE, INBOX)
    assert current is not None
    workflow.save_inbox(
        SCOPE,
        replace(current, revision=current.revision + 1, updated_at=NOW),
    )
    with pytest.raises(EmailSendAuthorityConflict, match="gateway_authority_drift"):
        service.validate(
            SCOPE,
            actor_ref=ACTOR,
            expected_gateway_snapshot=authorized["gateway_snapshot"],
            participant_projection=[
                {"address_role": "sender", "opaque_address_ref": SENDER},
                {"address_role": "to", "opaque_address_ref": RECIPIENT},
            ],
        )


@pytest.mark.parametrize(
    "projection",
    [
        [{"address_role": "sender", "opaque_address_ref": SENDER}],
        [
            {"address_role": "sender", "opaque_address_ref": SENDER},
            {"address_role": "bcc", "opaque_address_ref": RECIPIENT},
        ],
        [
            {"address_role": "sender", "opaque_address_ref": SENDER},
            {"address_role": "to", "opaque_address_ref": "sender@example.invalid"},
        ],
    ],
)
def test_validate_rejects_missing_bcc_or_raw_participant_projection(
    projection: list[dict[str, str]],
) -> None:
    service, _workflow = _service()
    authorized = service.authorize(
        SCOPE,
        actor_ref=ACTOR,
        inbox_item_ref=INBOX,
        draft_ref=DRAFT,
        expected_inbox_revision=4,
        expected_draft_revision=3,
        participant_roles_digest=ROLES_DIGEST,
    )
    with pytest.raises(EmailSendAuthorityConflict):
        service.validate(
            SCOPE,
            actor_ref=ACTOR,
            expected_gateway_snapshot=authorized["gateway_snapshot"],
            participant_projection=projection,
        )


def _approved_command() -> dict[str, object]:
    return {
        "site_id": SITE,
        "processing_purpose": SCOPE.processing_purpose,
        "team_ref": TEAM,
        "actor_user_ref": ACTOR_REF,
        "delegated_approver_user_ref": ACTOR_REF,
        "mailbox_ref": MAILBOX,
        "mailbox_config_revision": 3,
        "inbox_item_ref": INBOX,
        "inbox_item_revision": 4,
        "conversation_ref": CONVERSATION,
        "conversation_revision": 2,
        "reply_draft_ref": DRAFT,
        "reply_draft_revision": 3,
        "reply_draft_digest": "sha256:" + "4" * 64,
        "participants": [
            {"address_role": "sender", "opaque_address_ref": SENDER},
            {
                "address_role": "to",
                "opaque_address_ref": RECIPIENT,
                "identity_mapping_ref": MAPPING,
                "identity_mapping_revision": 7,
            },
        ],
        "party_ref": PARTY,
        "owner_user_ref": ACTOR_REF,
    }


def test_command_validation_requires_outbound_enabled_not_inbound_enabled() -> None:
    service, _workflow = _service()

    with pytest.raises(EmailSendAuthorityConflict, match="mailbox_authority_unavailable"):
        service.validate_command(SCOPE, command=_approved_command())


def test_command_validation_reloads_exact_gateway_revisions_and_participants() -> None:
    service, _workflow = _service()
    repository = service._mailboxes.repository  # noqa: SLF001
    mailbox = repository.get(SCOPE, MAILBOX)
    assert mailbox is not None
    repository._mailboxes[(SITE, MAILBOX)] = replace(  # type: ignore[attr-defined]  # noqa: SLF001
        mailbox,
        outbound_enabled=True,
    )

    result = service.validate_command(SCOPE, command=_approved_command())

    assert result == {
        "mailbox_ref": MAILBOX,
        "mailbox_config_revision": 3,
        "inbox_item_ref": INBOX,
        "inbox_item_revision": 4,
        "conversation_ref": CONVERSATION,
        "conversation_revision": 2,
        "reply_draft_ref": DRAFT,
        "reply_draft_revision": 3,
        "reply_draft_digest": "sha256:" + "4" * 64,
        "participants": (
            {
                "address_role": "sender",
                "opaque_address_ref": SENDER,
                "identity_mapping_ref": None,
                "identity_mapping_revision": None,
            },
            {
                "address_role": "to",
                "opaque_address_ref": RECIPIENT,
                "identity_mapping_ref": MAPPING,
                "identity_mapping_revision": 7,
            },
        ),
    }
