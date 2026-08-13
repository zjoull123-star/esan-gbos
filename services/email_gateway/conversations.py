from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import (
    AuditEvent,
    AuthorizationError,
    Conversation,
    GatewayActorScope,
    RevisionConflict,
    ScopeViolation,
    TenantScope,
    ThreadSuggestion,
    ValidationError,
    canonical_digest,
    stable_ref,
)
from .repositories.workflow import InMemoryWorkflowRepository

_MERGE_ROLES = frozenset({"Sales Manager", "Reviewer"})


class ConversationService:
    def __init__(self, repository: InMemoryWorkflowRepository) -> None:
        self.repository = repository

    def propose(
        self,
        scope: TenantScope,
        *,
        left_inbox_ref: str,
        right_inbox_ref: str,
        signals: tuple[str, ...],
        confidence: float,
        now: datetime,
    ) -> ThreadSuggestion:
        if left_inbox_ref == right_inbox_ref:
            raise ValidationError("suggestion requires distinct inbox items")
        if not signals or len(signals) != len(set(signals)):
            raise ValidationError("invalid suggestion signals")
        if not 0 <= confidence <= 1:
            raise ValidationError("invalid suggestion confidence")
        left = self.repository.get_inbox(scope, left_inbox_ref)
        right = self.repository.get_inbox(scope, right_inbox_ref)
        if left is None or right is None:
            raise ValidationError("suggestion inbox not found")
        if left.team_ref != right.team_ref:
            raise ScopeViolation("cross-team suggestion rejected")
        suggestion = ThreadSuggestion(
            suggestion_ref=stable_ref(
                "SGG", scope.site_id, *sorted((left_inbox_ref, right_inbox_ref))
            ),
            site_id=scope.site_id,
            team_ref=left.team_ref,
            left_inbox_ref=left_inbox_ref,
            right_inbox_ref=right_inbox_ref,
            signals=signals,
            confidence=confidence,
            status="proposed",
            revision=1,
            reviewed_by=None,
            reviewed_at=None,
            created_at=now,
        )
        return self.repository.save_suggestion(scope, suggestion)

    def accept(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        suggestion_ref: str,
        expected_suggestion_revision: int,
        expected_left_revision: int,
        expected_right_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> Conversation:
        suggestion = self._authorized_suggestion(scope, actor, suggestion_ref)
        payload_digest = canonical_digest(
            {
                "suggestion_ref": suggestion_ref,
                "suggestion_revision": expected_suggestion_revision,
                "left_revision": expected_left_revision,
                "right_revision": expected_right_revision,
            }
        )
        replay = self.repository.replay(scope, idempotency_key, payload_digest)
        if replay is not None:
            if not isinstance(replay, Conversation):
                raise ValidationError("workflow replay type conflict")
            return replay
        if suggestion.status != "proposed" or suggestion.revision != expected_suggestion_revision:
            raise RevisionConflict("suggestion revision conflict")
        left = self.repository.get_inbox(scope, suggestion.left_inbox_ref)
        right = self.repository.get_inbox(scope, suggestion.right_inbox_ref)
        if left is None or right is None:
            raise ValidationError("suggestion inbox not found")
        if left.revision != expected_left_revision or right.revision != expected_right_revision:
            raise RevisionConflict("inbox revision conflict")
        if left.team_ref != right.team_ref or left.team_ref != suggestion.team_ref:
            raise ScopeViolation("cross-team conversation rejected")
        if (
            self.repository.get_conversation_for(scope, left.inbox_item_ref) is not None
            or self.repository.get_conversation_for(scope, right.inbox_item_ref) is not None
        ):
            raise RevisionConflict("suggestion source already consumed")
        conversation = Conversation(
            conversation_ref=stable_ref("CON", scope.site_id, suggestion.suggestion_ref),
            site_id=scope.site_id,
            team_ref=suggestion.team_ref,
            party_ref=None,
            contact_ref=None,
            owner_user_ref=None,
            lifecycle_state="open",
            first_message_at=min(left.received_at, right.received_at),
            last_message_at=max(left.received_at, right.received_at),
            message_refs=(left.message_ref, right.message_ref),
            inbox_item_refs=(left.inbox_item_ref, right.inbox_item_ref),
            revision=1,
        )
        conversation = self.repository.save_conversation(scope, conversation)
        self.repository.save_suggestion(
            scope,
            replace(
                suggestion,
                status="accepted",
                revision=suggestion.revision + 1,
                reviewed_by=actor.actor_ref,
                reviewed_at=now,
            ),
        )
        self.repository.append_audit(
            scope,
            AuditEvent(
                audit_ref=stable_ref("AUD", scope.site_id, request_id),
                site_id=scope.site_id,
                actor_ref=actor.actor_ref,
                event_type="conversation_merge",
                subject_ref=conversation.conversation_ref,
                request_id=request_id,
                idempotency_key=f"audit:{idempotency_key}",
                payload_digest=payload_digest,
                occurred_at=now,
            ),
        )
        self.repository.remember(scope, idempotency_key, payload_digest, conversation)
        return conversation

    def reject(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        suggestion_ref: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> ThreadSuggestion:
        suggestion = self._authorized_suggestion(scope, actor, suggestion_ref)
        if suggestion.status != "proposed" or suggestion.revision != expected_revision:
            raise RevisionConflict("suggestion revision conflict")
        payload_digest = canonical_digest(
            {"suggestion_ref": suggestion_ref, "revision": expected_revision, "decision": "reject"}
        )
        replay = self.repository.replay(scope, idempotency_key, payload_digest)
        if replay is not None:
            if not isinstance(replay, ThreadSuggestion):
                raise ValidationError("workflow replay type conflict")
            return replay
        result = self.repository.save_suggestion(
            scope,
            replace(
                suggestion,
                status="rejected",
                revision=suggestion.revision + 1,
                reviewed_by=actor.actor_ref,
                reviewed_at=now,
            ),
        )
        self.repository.append_audit(
            scope,
            AuditEvent(
                audit_ref=stable_ref("AUD", scope.site_id, request_id),
                site_id=scope.site_id,
                actor_ref=actor.actor_ref,
                event_type="thread_suggestion_rejected",
                subject_ref=suggestion_ref,
                request_id=request_id,
                idempotency_key=f"audit:{idempotency_key}",
                payload_digest=payload_digest,
                occurred_at=now,
            ),
        )
        self.repository.remember(scope, idempotency_key, payload_digest, result)
        return result

    def split(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        conversation: Conversation,
        moved_inbox_refs: tuple[str, ...] | None = None,
        expected_revision: int | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> Conversation:
        if actor.site_id != scope.site_id:
            raise ScopeViolation("actor site mismatch")
        if not _MERGE_ROLES.intersection(actor.roles):
            raise AuthorizationError("conversation split requires reviewer role")
        if conversation.team_ref not in actor.team_refs:
            raise ScopeViolation("cross-team split rejected")
        pinned_revision = conversation.revision if expected_revision is None else expected_revision
        if conversation.revision != pinned_revision:
            raise RevisionConflict("conversation revision conflict")
        moved = conversation.inbox_item_refs if moved_inbox_refs is None else moved_inbox_refs
        if (
            not moved
            or len(moved) != len(set(moved))
            or not set(moved).issubset(conversation.inbox_item_refs)
            or len(moved) == len(conversation.inbox_item_refs)
        ):
            raise ValidationError("conversation split requires a strict non-empty proper subset")
        request = request_id or f"legacy-split-{conversation.conversation_ref}"
        idempotency = idempotency_key or f"legacy-split:{conversation.conversation_ref}"
        occurred_at = now or conversation.last_message_at
        payload_digest = canonical_digest(
            {
                "conversation_ref": conversation.conversation_ref,
                "expected_revision": pinned_revision,
                "moved_inbox_refs": moved,
                "request_id": request,
            }
        )
        replay = self.repository.replay(scope, idempotency, payload_digest)
        if replay is not None:
            if not isinstance(replay, Conversation):
                raise ValidationError("workflow replay type conflict")
            return replay
        durable_source = self.repository.get_conversation(scope, conversation.conversation_ref)
        if durable_source != conversation:
            raise RevisionConflict("conversation revision conflict")
        by_inbox = dict(zip(conversation.inbox_item_refs, conversation.message_refs, strict=True))
        remaining = tuple(
            inbox_ref for inbox_ref in conversation.inbox_item_refs if inbox_ref not in moved
        )
        source_revised = replace(
            conversation,
            message_refs=tuple(by_inbox[inbox_ref] for inbox_ref in remaining),
            inbox_item_refs=remaining,
            revision=conversation.revision + 1,
        )
        split = replace(
            conversation,
            conversation_ref=stable_ref("CON", conversation.conversation_ref, "split", *moved),
            message_refs=tuple(by_inbox[inbox_ref] for inbox_ref in moved),
            inbox_item_refs=moved,
            revision=1,
        )
        audit_event = AuditEvent(
            audit_ref=stable_ref("AUD", scope.site_id, request),
            site_id=scope.site_id,
            actor_ref=actor.actor_ref,
            event_type="conversation_split",
            subject_ref=split.conversation_ref,
            request_id=request,
            idempotency_key=f"audit:{idempotency}",
            payload_digest=payload_digest,
            occurred_at=occurred_at,
        )
        return self.repository.split_conversation(
            scope,
            source_before=conversation,
            source_revised=source_revised,
            split=split,
            audit_event=audit_event,
            idempotency_key=idempotency,
            payload_digest=payload_digest,
        )

    def get_conversation_for(self, scope: TenantScope, inbox_item_ref: str) -> Conversation | None:
        return self.repository.get_conversation_for(scope, inbox_item_ref)

    def _authorized_suggestion(
        self, scope: TenantScope, actor: GatewayActorScope, suggestion_ref: str
    ) -> ThreadSuggestion:
        if actor.site_id != scope.site_id:
            raise ScopeViolation("actor site mismatch")
        if not _MERGE_ROLES.intersection(actor.roles):
            raise AuthorizationError("conversation merge requires reviewer role")
        suggestion = self.repository.get_suggestion(scope, suggestion_ref)
        if suggestion is None:
            raise ValidationError("suggestion not found")
        if suggestion.team_ref not in actor.team_refs:
            raise ScopeViolation("actor team mismatch")
        return suggestion
