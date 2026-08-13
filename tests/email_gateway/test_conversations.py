from __future__ import annotations

from dataclasses import replace

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


def test_merge_rejects_stale_consumed_or_ai_actor_and_replay_drift(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import (
        AuthorizationError,
        GatewayActorScope,
        IdempotencyConflict,
        InboxItem,
        RevisionConflict,
    )
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
        signals=("provider_thread",),
        confidence=1,
        now=NOW,
    )
    assert service.get_conversation_for(scope, left.inbox_item_ref) is None
    ai = GatewayActorScope(SITE, "ai-worker", ("TEM-01",), ("AI Assistant",))
    with pytest.raises(AuthorizationError):
        service.accept(
            scope,
            actor=ai,
            suggestion_ref=suggestion.suggestion_ref,
            expected_suggestion_revision=1,
            expected_left_revision=1,
            expected_right_revision=1,
            request_id="REQ-AI-MERGE",
            idempotency_key="ai-merge",
            now=NOW,
        )
    manager = GatewayActorScope(SITE, "manager", ("TEM-01",), ("Sales Manager",))
    merged = service.accept(
        scope,
        actor=manager,
        suggestion_ref=suggestion.suggestion_ref,
        expected_suggestion_revision=1,
        expected_left_revision=1,
        expected_right_revision=1,
        request_id="REQ-MERGE",
        idempotency_key="merge",
        now=NOW,
    )
    assert merged.revision == 1
    with pytest.raises(IdempotencyConflict):
        service.accept(
            scope,
            actor=manager,
            suggestion_ref=suggestion.suggestion_ref,
            expected_suggestion_revision=2,
            expected_left_revision=1,
            expected_right_revision=1,
            request_id="REQ-MERGE",
            idempotency_key="merge",
            now=NOW,
        )
    with pytest.raises(RevisionConflict):
        service.accept(
            scope,
            actor=manager,
            suggestion_ref=suggestion.suggestion_ref,
            expected_suggestion_revision=1,
            expected_left_revision=1,
            expected_right_revision=1,
            request_id="REQ-MERGE-AGAIN",
            idempotency_key="merge-again",
            now=NOW,
        )


def test_split_is_revision_pinned_audited_and_preserves_source_refs(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import Conversation, GatewayActorScope
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repo = InMemoryWorkflowRepository()
    service = ConversationService(repo)
    source = Conversation(
        conversation_ref="CON-SOURCE",
        site_id=SITE,
        team_ref="TEM-01",
        party_ref="PTY-01",
        contact_ref="CNT-01",
        owner_user_ref="sales-01",
        lifecycle_state="open",
        first_message_at=NOW,
        last_message_at=NOW,
        message_refs=("MSG-01", "MSG-02"),
        inbox_item_refs=("INB-01", "INB-02"),
        revision=3,
    )
    from services.email_gateway.models import InboxItem

    first = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
    )
    second = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-02",
        team_ref="TEM-01",
        received_at=NOW,
    )
    source = replace(
        source,
        inbox_item_refs=(first.inbox_item_ref, second.inbox_item_ref),
    )
    repo.save_inbox(scope, first)
    repo.save_inbox(scope, second)
    repo.save_conversation(scope, source)
    actor = GatewayActorScope(SITE, "reviewer", ("TEM-01",), ("Reviewer",))
    split = service.split(
        scope,
        actor=actor,
        conversation=source,
        moved_inbox_refs=(second.inbox_item_ref,),
        expected_revision=3,
        request_id="REQ-SPLIT-01",
        idempotency_key="split-01",
        now=NOW,
    )
    assert split.message_refs == ("MSG-02",)
    assert split.inbox_item_refs == (second.inbox_item_ref,)
    assert split.revision == 1
    assert source.message_refs == ("MSG-01", "MSG-02")
    revised_source = repo.get_conversation(scope, source.conversation_ref)
    assert revised_source is not None
    assert revised_source.message_refs == ("MSG-01",)
    assert revised_source.inbox_item_refs == (first.inbox_item_ref,)
    assert revised_source.revision == 4
    assert repo.get_conversation_for(scope, first.inbox_item_ref) == revised_source
    assert repo.get_conversation_for(scope, second.inbox_item_ref) == split
    assert repo.audit_count(scope) == 1


def test_split_requires_strict_non_empty_proper_subset(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import Conversation, GatewayActorScope, ValidationError
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repo = InMemoryWorkflowRepository()
    service = ConversationService(repo)
    source = Conversation(
        conversation_ref="CON-SOURCE",
        site_id=SITE,
        team_ref="TEM-01",
        party_ref=None,
        contact_ref=None,
        owner_user_ref=None,
        lifecycle_state="open",
        first_message_at=NOW,
        last_message_at=NOW,
        message_refs=("MSG-01", "MSG-02"),
        inbox_item_refs=("INB-01", "INB-02"),
        revision=1,
    )
    actor = GatewayActorScope(SITE, "reviewer", ("TEM-01",), ("Reviewer",))
    for moved in ((), source.inbox_item_refs):
        with pytest.raises(ValidationError, match="proper subset"):
            service.split(
                scope,
                actor=actor,
                conversation=source,
                moved_inbox_refs=moved,
                expected_revision=1,
                request_id=f"REQ-SPLIT-{len(moved)}",
                idempotency_key=f"split-{len(moved)}",
                now=NOW,
            )


def test_split_injected_failure_rolls_back_source_new_audit_and_receipt(scope) -> None:
    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import Conversation, GatewayActorScope, InboxItem
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    def fail_after_source(phase: str) -> None:
        if phase == "after_split_source_write":
            raise RuntimeError("injected split failure")

    repo = InMemoryWorkflowRepository(transaction_failure_injector=fail_after_source)
    service = ConversationService(repo)
    first = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
    )
    second = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-02",
        team_ref="TEM-01",
        received_at=NOW,
    )
    repo.save_inbox(scope, first)
    repo.save_inbox(scope, second)
    source = Conversation(
        conversation_ref="CON-SOURCE-ROLLBACK",
        site_id=SITE,
        team_ref="TEM-01",
        party_ref=None,
        contact_ref=None,
        owner_user_ref=None,
        lifecycle_state="open",
        first_message_at=NOW,
        last_message_at=NOW,
        message_refs=(first.message_ref, second.message_ref),
        inbox_item_refs=(first.inbox_item_ref, second.inbox_item_ref),
        revision=1,
    )
    repo.save_conversation(scope, source)
    actor = GatewayActorScope(SITE, "reviewer", ("TEM-01",), ("Reviewer",))
    with pytest.raises(RuntimeError, match="injected"):
        service.split(
            scope,
            actor=actor,
            conversation=source,
            moved_inbox_refs=(second.inbox_item_ref,),
            expected_revision=1,
            request_id="REQ-SPLIT-ROLLBACK",
            idempotency_key="split-rollback",
            now=NOW,
        )
    assert repo.get_conversation(scope, source.conversation_ref) == source
    assert repo.get_conversation_for(scope, second.inbox_item_ref) == source
    assert repo.audit_count(scope) == 0
