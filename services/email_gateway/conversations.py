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

_MERGE_ROLES = frozenset({"Sales Manager", "Reviewer", "GBOS Admin"})


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
    ) -> Conversation:
        if not _MERGE_ROLES.intersection(actor.roles):
            raise AuthorizationError("conversation split requires reviewer role")
        if conversation.team_ref not in actor.team_refs:
            raise ScopeViolation("cross-team split rejected")
        return replace(
            conversation,
            conversation_ref=stable_ref("CON", conversation.conversation_ref, "split"),
            revision=1,
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
