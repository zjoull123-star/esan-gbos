from __future__ import annotations

import pytest

from .conftest import NOW, SITE


def test_suggestion_never_auto_merges_and_human_accept_is_revision_pinned(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import GatewayActorScope, InboxItem
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repo = InMemoryWorkflowRepository()
    service = ConversationService(repo)
    left = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
    )
    right = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-02",
        message_ref="MSG-02",
        team_ref="TEM-01",
        received_at=NOW,
    )
    repo.save_inbox(scope, left)
    repo.save_inbox(scope, right)
    suggestion = service.propose(
        scope,
        left_inbox_ref=left.inbox_item_ref,
        right_inbox_ref=right.inbox_item_ref,
        signals=("message_id_family", "participant_time_digest"),
        confidence=0.99,
        now=NOW,
    )
    assert suggestion.status == "proposed"
    assert service.get_conversation_for(scope, left.inbox_item_ref) is None
    actor = GatewayActorScope(
        site_id=SITE,
        actor_ref="manager@example.invalid",
        team_refs=("TEM-01",),
        roles=("Sales Manager",),
    )
    conversation = service.accept(
        scope,
        actor=actor,
        suggestion_ref=suggestion.suggestion_ref,
        expected_suggestion_revision=1,
        expected_left_revision=1,
        expected_right_revision=1,
        request_id="REQ-MERGE-01",
        idempotency_key="merge-01",
        now=NOW,
    )
    assert conversation.message_refs == ("MSG-01", "MSG-02")


def test_cross_team_or_sales_user_cannot_merge(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import AuthorizationError, GatewayActorScope, InboxItem
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repo = InMemoryWorkflowRepository()
    service = ConversationService(repo)
    left = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
    )
    right = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-02",
        message_ref="MSG-02",
        team_ref="TEM-01",
        received_at=NOW,
    )
    repo.save_inbox(scope, left)
    repo.save_inbox(scope, right)
    suggestion = service.propose(
        scope,
        left_inbox_ref=left.inbox_item_ref,
        right_inbox_ref=right.inbox_item_ref,
        signals=("message_id_family",),
        confidence=0.5,
        now=NOW,
    )
    actor = GatewayActorScope(
        site_id=SITE,
        actor_ref="sales@example.invalid",
        team_refs=("TEM-01",),
        roles=("Sales User",),
    )
    with pytest.raises(AuthorizationError):
        service.accept(
            scope,
            actor=actor,
            suggestion_ref=suggestion.suggestion_ref,
            expected_suggestion_revision=1,
            expected_left_revision=1,
            expected_right_revision=1,
            request_id="REQ-MERGE-01",
            idempotency_key="merge-01",
            now=NOW,
        )


def test_reject_and_split_are_audited_human_commands(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import GatewayActorScope, InboxItem
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repo = InMemoryWorkflowRepository()
    service = ConversationService(repo)
    item = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
    )
    other = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-02",
        message_ref="MSG-02",
        team_ref="TEM-01",
        received_at=NOW,
    )
    repo.save_inbox(scope, item)
    repo.save_inbox(scope, other)
    suggestion = service.propose(
        scope,
        left_inbox_ref=item.inbox_item_ref,
        right_inbox_ref=other.inbox_item_ref,
        signals=("digest",),
        confidence=0.2,
        now=NOW,
    )
    actor = GatewayActorScope(
        site_id=SITE,
        actor_ref="reviewer@example.invalid",
        team_refs=("TEM-01",),
        roles=("Reviewer",),
    )
    rejected = service.reject(
        scope,
        actor=actor,
        suggestion_ref=suggestion.suggestion_ref,
        expected_revision=1,
        request_id="REQ-REJECT-01",
        idempotency_key="reject-01",
        now=NOW,
    )
    assert rejected.status == "rejected"
    assert repo.audit_count(scope) == 1
