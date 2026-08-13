from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from services.email_gateway import protocols as gateway_protocols
from services.email_gateway.conversations import ConversationService
from services.email_gateway.drafts import DraftService
from services.email_gateway.identity_projection import IdentityProjectionService
from services.email_gateway.intake import GatewayIntakeService
from services.email_gateway.mailboxes import MailboxRegistry
from services.email_gateway.models import (
    AuthorityRoute,
    EmailMessagePublication,
    GatewayActorScope,
    IdentityProjection,
    Mailbox,
    TenantScope,
)
from services.email_gateway.operations import InboxOperations
from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository
from services.email_gateway.repositories.intake import InMemoryIntakeRepository
from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository
from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository
from services.email_gateway.routing import RoutingService
from services.email_gateway.send_outbox import DisabledSendOutboxRepository
from services.email_gateway.sla import MailboxSlaPolicy, SlaClock
from services.observer.observer.connectors.email_delivery import EmailRawDeliveryDecoder
from services.observer.observer.identity_tokens import HmacSha256IdentityTokenResolver
from services.observer.observer.models import ConnectorKey, stable_ulid
from services.observer.observer.models import TenantScope as ObserverTenantScope
from services.observer.observer.normalizers import EmailObservationNormalizer

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)
SCOPE = TenantScope("site.local", "sales_follow_up")
TEAM_REF = "TEM-01"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class _OfflineAuthority:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, **_: object) -> AuthorityRoute:
        self.calls += 1
        return AuthorityRoute.assigned(
            party_ref="PTY-01",
            party_revision=3,
            team_ref=TEAM_REF,
            team_revision=4,
            owner_user_ref="sales-a",
            owner_eligibility_revision=DIGEST_A,
            resolved_at=NOW,
        )


def _mailbox(suffix: str) -> Mailbox:
    provider_account_ref = f"fake-account-{suffix}"
    return Mailbox(
        mailbox_ref="MBX-" + stable_ulid("offline-human-mailbox", suffix),
        site_id=SCOPE.site_id,
        address_display=f"restricted-{suffix}",
        provider="fake",
        provider_account_ref=provider_account_ref,
        observer_connector_instance_ref=(
            "OCI-" + stable_ulid("email-connector-instance", SCOPE.site_id, provider_account_ref)
        ),
        entry_role="primary",
        business_purpose=SCOPE.processing_purpose,
        default_team_ref=TEAM_REF,
        account_owner_user_ref="gateway-owner",
        priority=10,
        inbound_enabled=True,
        outbound_enabled=False,
        credential_ref="fake-disabled",
        status="active",
        config_revision=1,
        observer_config_projection_receipt=None,
    )


def _publication(mailbox: Mailbox, suffix: str) -> EmailMessagePublication:
    from services.observer.observer.email_publication import build_email_publication

    message = EmailMessage()
    message["From"] = "sender@example.invalid"
    message["To"] = "recipient@example.invalid"
    message["Subject"] = "restricted offline fixture"
    message["Message-ID"] = f"<offline-{suffix}@example.invalid>"
    message.set_content("offline fixture body")
    source_ref = "obs:v1:partition:sha256:" + suffix * 64
    item = EmailRawDeliveryDecoder().decode_delivery(
        message.as_bytes(),
        delivery_id=f"fake:{suffix}",
        received_at=NOW,
        source_ref=source_ref,
    )[0]
    normalized = EmailObservationNormalizer(
        identity_resolver=HmacSha256IdentityTokenResolver(b"x" * 32),
        site_id=SCOPE.site_id,
        purpose=SCOPE.processing_purpose,
    ).normalize(item, source_ref=source_ref)
    observer_publication = build_email_publication(
        scope=ObserverTenantScope(SCOPE.site_id, SCOPE.processing_purpose),
        key=ConnectorKey("email", mailbox.provider_account_ref),
        item=item,
        normalized=normalized,
        mailbox_id=mailbox.mailbox_ref,
        mailbox_config_revision=mailbox.config_revision,
        observer_delivery_ref=f"fake:{suffix}",
        received_at=NOW,
        publication_revision=1,
    )
    wire = observer_publication.to_wire()
    return EmailMessagePublication.from_wire(
        wire,
        processing_purpose=SCOPE.processing_purpose,
        payload_digest="sha256:" + observer_publication.payload_sha256,
    )


def _actor(ref: str, role: str) -> GatewayActorScope:
    return GatewayActorScope(SCOPE.site_id, ref, (TEAM_REF,), (role,))


def test_human_operations_offline_e2e_preserves_one_audit_trail_and_never_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_network(*_: object, **__: object) -> None:
        raise AssertionError("human operations offline E2E attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr(socket.socket, "connect", forbid_network)

    mailboxes = (_mailbox("PRIMARY-A"), _mailbox("PRIMARY-B"))
    registry = MailboxRegistry(InMemoryMailboxRepository())
    for index, mailbox in enumerate(mailboxes, start=1):
        registry.upsert(
            SCOPE,
            mailbox,
            expected_revision=0,
            actor_ref="offline-admin",
            request_id=f"REQ-MBX-{index}",
            idempotency_key=f"mailbox-{index}",
        )
    intake_repository = InMemoryIntakeRepository()
    intake = GatewayIntakeService(intake_repository, registry)
    accepted = tuple(
        intake.accept(SCOPE, _publication(mailbox, str(index)))
        for index, mailbox in enumerate(mailboxes, start=1)
    )
    assert intake_repository.counts(SCOPE) == (2, 2, 2)
    assert len({item.inbox_item.inbox_item_ref for item in accepted}) == 2
    assert all(mailbox.entry_role == "primary" for mailbox in mailboxes)

    identity = IdentityProjectionService(InMemoryIdentityProjectionRepository())
    opaque_sender_ref = accepted[0].message.participants[0].identity_ref
    projection = identity.apply(
        SCOPE,
        IdentityProjection(
            site_id=SCOPE.site_id,
            processing_purpose=SCOPE.processing_purpose,
            opaque_address_ref=opaque_sender_ref,
            external_identity_ref="EID-OFFLINE-PARTY",
            external_identity_revision=1,
            identity_type="Party",
            team_ref=TEAM_REF,
            status="confirmed",
            projection_receipt_ref="IPR-OFFLINE-01",
            observed_at=NOW,
            payload_digest=DIGEST_A,
        ),
    )
    authority = _OfflineAuthority()
    route = RoutingService(authority).route(
        scope=SCOPE,
        inbox=accepted[0].inbox_item,
        mailbox=mailboxes[0],
        projection=projection,
        rules=(),
    )
    assert (route.route_status, route.owner_user_ref, authority.calls) == (
        "assigned",
        "sales-a",
        1,
    )

    workflow = InMemoryWorkflowRepository()
    for item in accepted:
        workflow.save_inbox(SCOPE, item.inbox_item)
    operations = InboxOperations(workflow)
    first = operations.apply_identity_route(
        SCOPE,
        worker_kind="routing_worker",
        inbox_item_ref=accepted[0].inbox_item.inbox_item_ref,
        target_state="assigned",
        assignee_user_ref=route.owner_user_ref,
        assignee_team_ref=TEAM_REF,
        assignee_enabled=True,
        expected_revision=1,
        request_id="REQ-ROUTE-1",
        idempotency_key="route-1",
        now=NOW + timedelta(seconds=1),
    )
    second = operations.apply_identity_route(
        SCOPE,
        worker_kind="identity_worker",
        inbox_item_ref=accepted[1].inbox_item.inbox_item_ref,
        target_state="unassigned",
        assignee_user_ref=None,
        assignee_team_ref=None,
        assignee_enabled=False,
        expected_revision=1,
        request_id="REQ-ROUTE-2",
        idempotency_key="route-2",
        now=NOW + timedelta(seconds=1),
    )

    claim_command = dict(
        actor=_actor("sales-b", "Sales User"),
        actor_enabled=True,
        inbox_item_ref=second.inbox_item_ref,
        expected_revision=second.revision,
        request_id="REQ-CLAIM",
        idempotency_key="claim",
        now=NOW + timedelta(seconds=2),
    )
    claimed = operations.claim(SCOPE, **claim_command)
    audit_after_claim = workflow.audit_count(SCOPE)
    restarted_operations = InboxOperations(workflow)
    assert restarted_operations.claim(SCOPE, **claim_command) == claimed
    assert workflow.audit_count(SCOPE) == audit_after_claim

    reassigned = restarted_operations.reassign(
        SCOPE,
        actor=_actor("manager", "Sales Manager"),
        actor_enabled=True,
        inbox_item_ref=claimed.inbox_item_ref,
        assignee_user_ref="sales-c",
        assignee_team_ref=TEAM_REF,
        assignee_enabled=True,
        expected_revision=claimed.revision,
        request_id="REQ-REASSIGN",
        idempotency_key="reassign",
        now=NOW + timedelta(seconds=3),
    )
    sla = SlaClock.start(
        inbox_item_ref=reassigned.inbox_item_ref,
        received_at=NOW,
        policy=MailboxSlaPolicy("SLA-OFFLINE", 1, 60, NOW),
        quarantined=False,
    ).preserve_for_revision(reassigned.revision, now=NOW + timedelta(seconds=3))
    assert sla.due_at == NOW + timedelta(seconds=60)
    assert sla.close(NOW + timedelta(seconds=61), policy_revision=1).status == ("closed_overdue")

    conversations = ConversationService(workflow)
    initial = conversations.propose(
        SCOPE,
        left_inbox_ref=first.inbox_item_ref,
        right_inbox_ref=reassigned.inbox_item_ref,
        signals=("message_id_family",),
        confidence=0.75,
        now=NOW + timedelta(seconds=4),
    )
    reviewer = _actor("reviewer", "Reviewer")
    assert (
        conversations.reject(
            SCOPE,
            actor=reviewer,
            suggestion_ref=initial.suggestion_ref,
            expected_revision=1,
            request_id="REQ-REJECT",
            idempotency_key="reject",
            now=NOW + timedelta(seconds=5),
        ).status
        == "rejected"
    )
    manual = conversations.propose(
        SCOPE,
        left_inbox_ref=first.inbox_item_ref,
        right_inbox_ref=reassigned.inbox_item_ref,
        signals=("manual_review",),
        confidence=1,
        now=NOW + timedelta(seconds=6),
    )
    conversation = conversations.accept(
        SCOPE,
        actor=reviewer,
        suggestion_ref=manual.suggestion_ref,
        expected_suggestion_revision=1,
        expected_left_revision=first.revision,
        expected_right_revision=reassigned.revision,
        request_id="REQ-MANUAL-MERGE",
        idempotency_key="manual-merge",
        now=NOW + timedelta(seconds=7),
    )
    assert set(conversation.inbox_item_refs) == {first.inbox_item_ref, reassigned.inbox_item_ref}

    linked = restarted_operations.link_business(
        SCOPE,
        actor=_actor("sales-a", "Sales User"),
        actor_enabled=True,
        inbox_item_ref=first.inbox_item_ref,
        business_ref="CRM-DEAL-OFFLINE-01",
        authority_valid=True,
        authority_team_ref=TEAM_REF,
        expected_revision=first.revision,
        request_id="REQ-LINK",
        idempotency_key="link",
        now=NOW + timedelta(seconds=8),
    )
    assert linked.business_links == ("CRM-DEAL-OFFLINE-01",)

    drafts = DraftService(workflow)
    worker = _actor("email-draft-worker", "Email Gateway Worker")
    draft = drafts.create(
        SCOPE,
        actor=worker,
        inbox_item_ref=linked.inbox_item_ref,
        conversation_ref=conversation.conversation_ref,
        content_evidence_ref="EVD-DRAFT-01",
        content_digest=DIGEST_A,
        request_id="REQ-DRAFT-1",
        idempotency_key="draft-1",
        now=NOW + timedelta(seconds=9),
    )
    edited = drafts.update(
        SCOPE,
        actor=worker,
        draft_ref=draft.draft_ref,
        expected_revision=1,
        content_evidence_ref="EVD-DRAFT-02",
        content_digest=DIGEST_B,
        request_id="REQ-DRAFT-2",
        idempotency_key="draft-2",
        now=NOW + timedelta(seconds=10),
    )
    assert (edited.revision, edited.state) == (2, "editable")

    current = tuple(workflow.get_inbox(SCOPE, item.inbox_item.inbox_item_ref) for item in accepted)
    assert all(item is not None for item in current)
    assert all(
        item is not None and item.state not in {"send_queued", "send_uncertain", "waiting_customer"}
        for item in current
    )
    assert all(mailbox.outbound_enabled is False for mailbox in mailboxes)
    assert DisabledSendOutboxRepository(outbound_enabled=False).outbound_enabled is False
    assert {
        "EmailTransport",
        "LLMClient",
        "ModelClient",
        "ProviderSender",
        "SmtpClient",
        "send_email",
    }.isdisjoint(vars(gateway_protocols))
