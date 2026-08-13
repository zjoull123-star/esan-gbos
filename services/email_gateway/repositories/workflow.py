from __future__ import annotations

import hashlib
from threading import RLock
from typing import Any

from ..models import (
    AuditEvent,
    Conversation,
    Draft,
    IdempotencyConflict,
    InboxItem,
    RevisionConflict,
    ScopeViolation,
    TenantScope,
    ThreadSuggestion,
    require_scope,
    stable_ref,
)
from ..postgres import Connection, redacted_database_errors, site_transaction
from .audit import PostgresAuditRepository


class InMemoryWorkflowRepository:
    def __init__(self) -> None:
        self._inbox: dict[tuple[str, str], InboxItem] = {}
        self._suggestions: dict[tuple[str, str], ThreadSuggestion] = {}
        self._conversations: dict[tuple[str, str], Conversation] = {}
        self._conversation_by_inbox: dict[tuple[str, str], str] = {}
        self._drafts: dict[tuple[str, str], Draft] = {}
        self._audit: list[AuditEvent] = []
        self._idempotency: dict[tuple[str, str], tuple[str, object]] = {}
        self._lock = RLock()

    def save_inbox(self, scope: TenantScope, inbox: InboxItem) -> InboxItem:
        require_scope(scope, site_id=inbox.site_id)
        with self._lock:
            self._inbox[(scope.site_id, inbox.inbox_item_ref)] = inbox
        return inbox

    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None:
        return self._inbox.get((scope.site_id, inbox_ref))

    def save_suggestion(self, scope: TenantScope, suggestion: ThreadSuggestion) -> ThreadSuggestion:
        require_scope(scope, site_id=suggestion.site_id)
        with self._lock:
            self._suggestions[(scope.site_id, suggestion.suggestion_ref)] = suggestion
        return suggestion

    def get_suggestion(self, scope: TenantScope, suggestion_ref: str) -> ThreadSuggestion | None:
        return self._suggestions.get((scope.site_id, suggestion_ref))

    def save_conversation(self, scope: TenantScope, conversation: Conversation) -> Conversation:
        require_scope(scope, site_id=conversation.site_id)
        with self._lock:
            self._conversations[(scope.site_id, conversation.conversation_ref)] = conversation
            for inbox_ref in conversation.inbox_item_refs:
                self._conversation_by_inbox[(scope.site_id, inbox_ref)] = (
                    conversation.conversation_ref
                )
        return conversation

    def get_conversation_for(self, scope: TenantScope, inbox_ref: str) -> Conversation | None:
        conversation_ref = self._conversation_by_inbox.get((scope.site_id, inbox_ref))
        if conversation_ref is None:
            return None
        return self._conversations[(scope.site_id, conversation_ref)]

    def save_draft(
        self,
        scope: TenantScope,
        draft: Draft,
        *,
        idempotency_key: str,
        payload_digest: str,
    ) -> Draft:
        require_scope(scope, site_id=draft.site_id)
        replay_key = (scope.site_id, idempotency_key)
        with self._lock:
            replay = self._idempotency.get(replay_key)
            if replay is not None:
                prior_digest, result = replay
                if prior_digest != payload_digest:
                    raise IdempotencyConflict("draft idempotency drift")
                if not isinstance(result, Draft):
                    raise IdempotencyConflict("idempotency namespace conflict")
                return result
            self._drafts[(scope.site_id, draft.draft_ref)] = draft
            self._idempotency[replay_key] = (payload_digest, draft)
            return draft

    def get_draft(self, scope: TenantScope, draft_ref: str) -> Draft | None:
        return self._drafts.get((scope.site_id, draft_ref))

    def append_audit(self, scope: TenantScope, event: AuditEvent) -> AuditEvent:
        require_scope(scope, site_id=event.site_id)
        with self._lock:
            self._audit.append(event)
        return event

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> object | None:
        replay = self._idempotency.get((scope.site_id, idempotency_key))
        if replay is None:
            return None
        prior_digest, result = replay
        if prior_digest != payload_digest:
            raise IdempotencyConflict("workflow idempotency drift")
        return result

    def remember(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str, result: object
    ) -> None:
        self._idempotency[(scope.site_id, idempotency_key)] = (payload_digest, result)

    def audit_count(self, scope: TenantScope) -> int:
        return sum(item.site_id == scope.site_id for item in self._audit)


class PostgresWorkflowRepository:
    CLAIMABLE_INBOX_SQL = """
        SELECT inbox_item_ref, revision
          FROM email_gateway.inbox_items
         WHERE site_id = %s AND team_ref = ANY(%s)
         ORDER BY received_at DESC, inbox_item_ref
         LIMIT %s
    """

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def __repr__(self) -> str:
        return "PostgresWorkflowRepository(connection=<redacted>)"

    def save_inbox(self, scope: TenantScope, inbox: InboxItem) -> InboxItem:
        require_scope(scope, site_id=inbox.site_id)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            _require_mailbox_scope(cursor, scope, inbox.mailbox_ref)
            _lock_workflow_ref(cursor, scope, "inbox", inbox.inbox_item_ref)
            current = self._load_inbox(cursor, scope, inbox.inbox_item_ref, lock=True)
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO email_gateway.inbox_items (
                        site_id, inbox_item_ref, mailbox_ref, message_ref, team_ref,
                        assignee_user_ref, priority, sla_due_at, state, conversation_ref,
                        business_links, revision, received_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    _inbox_params(inbox),
                )
                return inbox
            if current == inbox:
                return current
            if inbox.revision != current.revision + 1:
                if inbox.revision == current.revision:
                    raise IdempotencyConflict("inbox revision drift")
                raise RevisionConflict("inbox revision conflict")
            cursor.execute(
                """
                UPDATE email_gateway.inbox_items
                   SET mailbox_ref = %s, message_ref = %s, team_ref = %s,
                       assignee_user_ref = %s, priority = %s, sla_due_at = %s,
                       state = %s, conversation_ref = %s, business_links = %s,
                       revision = %s, received_at = %s, updated_at = %s
                 WHERE site_id = %s AND inbox_item_ref = %s AND revision = %s
                RETURNING inbox_item_ref
                """,
                (
                    inbox.mailbox_ref,
                    inbox.message_ref,
                    inbox.team_ref,
                    inbox.assignee_user_ref,
                    inbox.priority,
                    inbox.sla_due_at,
                    inbox.state,
                    inbox.conversation_ref,
                    list(inbox.business_links),
                    inbox.revision,
                    inbox.received_at,
                    inbox.updated_at,
                    scope.site_id,
                    inbox.inbox_item_ref,
                    current.revision,
                ),
            )
            if cursor.fetchone() is None:
                raise RevisionConflict("inbox revision conflict")
            return inbox

    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            return self._load_inbox(cursor, scope, inbox_ref)

    def save_suggestion(self, scope: TenantScope, suggestion: ThreadSuggestion) -> ThreadSuggestion:
        require_scope(scope, site_id=suggestion.site_id)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            if (
                self._load_inbox(cursor, scope, suggestion.left_inbox_ref) is None
                or self._load_inbox(cursor, scope, suggestion.right_inbox_ref) is None
            ):
                raise ScopeViolation("suggestion inbox purpose mismatch")
            _lock_workflow_ref(cursor, scope, "suggestion", suggestion.suggestion_ref)
            current = self._load_suggestion(cursor, scope, suggestion.suggestion_ref, lock=True)
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO email_gateway.thread_suggestions (
                        site_id, suggestion_ref, team_ref, left_inbox_ref,
                        right_inbox_ref, signals, confidence, status, revision,
                        reviewed_by, reviewed_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        suggestion.site_id,
                        suggestion.suggestion_ref,
                        suggestion.team_ref,
                        suggestion.left_inbox_ref,
                        suggestion.right_inbox_ref,
                        list(suggestion.signals),
                        suggestion.confidence,
                        suggestion.status,
                        suggestion.revision,
                        suggestion.reviewed_by,
                        suggestion.reviewed_at,
                        suggestion.created_at,
                    ),
                )
                return suggestion
            if current == suggestion:
                return current
            if suggestion.revision != current.revision + 1:
                if suggestion.revision == current.revision:
                    raise IdempotencyConflict("suggestion revision drift")
                raise RevisionConflict("suggestion revision conflict")
            cursor.execute(
                """
                UPDATE email_gateway.thread_suggestions
                   SET status = %s, revision = %s, reviewed_by = %s, reviewed_at = %s
                 WHERE site_id = %s AND suggestion_ref = %s AND revision = %s
                RETURNING suggestion_ref
                """,
                (
                    suggestion.status,
                    suggestion.revision,
                    suggestion.reviewed_by,
                    suggestion.reviewed_at,
                    scope.site_id,
                    suggestion.suggestion_ref,
                    current.revision,
                ),
            )
            if cursor.fetchone() is None:
                raise RevisionConflict("suggestion revision conflict")
            return suggestion

    def get_suggestion(self, scope: TenantScope, suggestion_ref: str) -> ThreadSuggestion | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            return self._load_suggestion(cursor, scope, suggestion_ref)

    def save_conversation(self, scope: TenantScope, conversation: Conversation) -> Conversation:
        require_scope(scope, site_id=conversation.site_id)
        if not conversation.message_refs or len(conversation.message_refs) != len(
            conversation.inbox_item_refs
        ):
            raise IdempotencyConflict("invalid conversation membership")
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            for message_ref, inbox_ref in zip(
                conversation.message_refs,
                conversation.inbox_item_refs,
                strict=True,
            ):
                scoped_inbox = self._load_inbox(cursor, scope, inbox_ref)
                if scoped_inbox is None or scoped_inbox.message_ref != message_ref:
                    raise ScopeViolation("conversation member purpose mismatch")
            _lock_workflow_ref(cursor, scope, "conversation", conversation.conversation_ref)
            current = self._load_conversation(
                cursor, scope, conversation.conversation_ref, lock=True
            )
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO email_gateway.conversations (
                        site_id, conversation_ref, team_ref, party_ref, contact_ref,
                        owner_user_ref, lifecycle_state, first_message_at,
                        last_message_at, revision
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        conversation.site_id,
                        conversation.conversation_ref,
                        conversation.team_ref,
                        conversation.party_ref,
                        conversation.contact_ref,
                        conversation.owner_user_ref,
                        conversation.lifecycle_state,
                        conversation.first_message_at,
                        conversation.last_message_at,
                        conversation.revision,
                    ),
                )
                for ordinal, (message_ref, inbox_ref) in enumerate(
                    zip(
                        conversation.message_refs,
                        conversation.inbox_item_refs,
                        strict=True,
                    ),
                    start=1,
                ):
                    cursor.execute(
                        """
                        INSERT INTO email_gateway.conversation_messages (
                            site_id, conversation_ref, message_ref, inbox_item_ref, ordinal
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            scope.site_id,
                            conversation.conversation_ref,
                            message_ref,
                            inbox_ref,
                            ordinal,
                        ),
                    )
                return conversation
            if current == conversation:
                return current
            if (
                conversation.message_refs != current.message_refs
                or conversation.inbox_item_refs != current.inbox_item_refs
            ):
                raise RevisionConflict("conversation membership is immutable")
            if conversation.revision != current.revision + 1:
                if conversation.revision == current.revision:
                    raise IdempotencyConflict("conversation revision drift")
                raise RevisionConflict("conversation revision conflict")
            cursor.execute(
                """
                UPDATE email_gateway.conversations
                   SET team_ref = %s, party_ref = %s, contact_ref = %s,
                       owner_user_ref = %s, lifecycle_state = %s,
                       first_message_at = %s, last_message_at = %s,
                       revision = %s, updated_at = now()
                 WHERE site_id = %s AND conversation_ref = %s AND revision = %s
                RETURNING conversation_ref
                """,
                (
                    conversation.team_ref,
                    conversation.party_ref,
                    conversation.contact_ref,
                    conversation.owner_user_ref,
                    conversation.lifecycle_state,
                    conversation.first_message_at,
                    conversation.last_message_at,
                    conversation.revision,
                    scope.site_id,
                    conversation.conversation_ref,
                    current.revision,
                ),
            )
            if cursor.fetchone() is None:
                raise RevisionConflict("conversation revision conflict")
            return conversation

    def get_conversation_for(self, scope: TenantScope, inbox_ref: str) -> Conversation | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT member.conversation_ref
                  FROM email_gateway.conversation_messages AS member
                  JOIN email_gateway.inbox_items AS inbox
                    ON inbox.site_id = member.site_id
                   AND inbox.inbox_item_ref = member.inbox_item_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE member.site_id = %s AND member.inbox_item_ref = %s
                   AND mailbox.business_purpose = %s
                 ORDER BY member.conversation_ref
                 LIMIT 1
                """,
                (scope.site_id, inbox_ref, scope.processing_purpose),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._load_conversation(cursor, scope, str(row[0]))

    def save_draft(
        self,
        scope: TenantScope,
        draft: Draft,
        *,
        idempotency_key: str,
        payload_digest: str,
    ) -> Draft:
        require_scope(scope, site_id=draft.site_id)
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            if self._load_inbox(cursor, scope, draft.inbox_item_ref) is None:
                raise ScopeViolation("draft inbox purpose mismatch")
            _lock_workflow_ref(
                cursor,
                scope,
                "draft",
                f"{draft.draft_ref}\x1f{idempotency_key}",
            )
            cursor.execute(
                """
                SELECT draft.draft_ref, draft.site_id, draft.inbox_item_ref,
                       draft.conversation_ref, draft.content_evidence_ref,
                       draft.content_digest, draft.revision, draft.state,
                       draft.updated_at, draft.request_id
                  FROM email_gateway.reply_drafts AS draft
                  JOIN email_gateway.inbox_items AS inbox
                    ON inbox.site_id = draft.site_id
                   AND inbox.inbox_item_ref = draft.inbox_item_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE draft.site_id = %s AND draft.idempotency_key = %s
                   AND mailbox.business_purpose = %s
                """,
                (scope.site_id, idempotency_key, scope.processing_purpose),
            )
            replay_row = cursor.fetchone()
            if replay_row is not None:
                if str(replay_row[9]) != payload_digest:
                    raise IdempotencyConflict("draft idempotency drift")
                return _draft_from_row(replay_row[:9])

            cursor.execute(
                """
                SELECT draft.draft_ref, draft.site_id, draft.inbox_item_ref,
                       draft.conversation_ref, draft.content_evidence_ref,
                       draft.content_digest, draft.revision, draft.state,
                       draft.updated_at
                  FROM email_gateway.reply_drafts AS draft
                  JOIN email_gateway.inbox_items AS inbox
                    ON inbox.site_id = draft.site_id
                   AND inbox.inbox_item_ref = draft.inbox_item_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE draft.site_id = %s AND draft.draft_ref = %s
                   AND mailbox.business_purpose = %s
                 FOR UPDATE OF draft
                """,
                (scope.site_id, draft.draft_ref, scope.processing_purpose),
            )
            current_row = cursor.fetchone()
            current = None if current_row is None else _draft_from_row(current_row)
            if current is None:
                cursor.execute(
                    """
                    INSERT INTO email_gateway.reply_drafts (
                        site_id, draft_ref, inbox_item_ref, conversation_ref,
                        content_evidence_ref, content_digest, state, revision,
                        request_id, idempotency_key, updated_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        draft.site_id,
                        draft.draft_ref,
                        draft.inbox_item_ref,
                        draft.conversation_ref,
                        draft.content_evidence_ref,
                        draft.content_digest,
                        draft.state,
                        draft.revision,
                        payload_digest,
                        idempotency_key,
                        draft.updated_at,
                        draft.updated_at,
                    ),
                )
                return draft
            if current == draft:
                return current
            if draft.revision != current.revision + 1:
                if draft.revision == current.revision:
                    raise IdempotencyConflict("draft revision drift")
                raise RevisionConflict("draft revision conflict")
            cursor.execute(
                """
                UPDATE email_gateway.reply_drafts
                   SET inbox_item_ref = %s, conversation_ref = %s,
                       content_evidence_ref = %s, content_digest = %s,
                       state = %s, revision = %s, request_id = %s,
                       idempotency_key = %s, updated_at = %s
                 WHERE site_id = %s AND draft_ref = %s AND revision = %s
                RETURNING draft_ref
                """,
                (
                    draft.inbox_item_ref,
                    draft.conversation_ref,
                    draft.content_evidence_ref,
                    draft.content_digest,
                    draft.state,
                    draft.revision,
                    payload_digest,
                    idempotency_key,
                    draft.updated_at,
                    scope.site_id,
                    draft.draft_ref,
                    current.revision,
                ),
            )
            if cursor.fetchone() is None:
                raise RevisionConflict("draft revision conflict")
            return draft

    def get_draft(self, scope: TenantScope, draft_ref: str) -> Draft | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT draft.draft_ref, draft.site_id, draft.inbox_item_ref,
                       draft.conversation_ref, draft.content_evidence_ref,
                       draft.content_digest, draft.revision, draft.state,
                       draft.updated_at
                  FROM email_gateway.reply_drafts AS draft
                  JOIN email_gateway.inbox_items AS inbox
                    ON inbox.site_id = draft.site_id
                   AND inbox.inbox_item_ref = draft.inbox_item_ref
                  JOIN email_gateway.mailboxes AS mailbox
                    ON mailbox.site_id = inbox.site_id
                   AND mailbox.mailbox_ref = inbox.mailbox_ref
                 WHERE draft.site_id = %s AND draft.draft_ref = %s
                   AND mailbox.business_purpose = %s
                """,
                (scope.site_id, draft_ref, scope.processing_purpose),
            )
            row = cursor.fetchone()
            return None if row is None else _draft_from_row(row)

    def append_audit(self, scope: TenantScope, event: AuditEvent) -> AuditEvent:
        return PostgresAuditRepository(self.connection).append(scope, event)

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> object | None:
        marker_key = _workflow_key(
            scope.site_id,
            scope.processing_purpose,
            idempotency_key,
        )
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT event_type, subject_ref, payload_digest
                  FROM email_gateway.audit_events
                 WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, marker_key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if str(row[2]) != payload_digest:
            raise IdempotencyConflict("workflow idempotency drift")
        result_type = str(row[0]).removeprefix("workflow_idempotency_")
        result_ref = str(row[1])
        if result_type == "conversation":
            with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
                return self._load_conversation(cursor, scope, result_ref)
        if result_type == "suggestion":
            return self.get_suggestion(scope, result_ref)
        if result_type == "draft":
            return self.get_draft(scope, result_ref)
        if result_type == "inbox":
            return self.get_inbox(scope, result_ref)
        raise IdempotencyConflict("workflow replay type conflict")

    def remember(
        self,
        scope: TenantScope,
        idempotency_key: str,
        payload_digest: str,
        result: object,
    ) -> None:
        if isinstance(result, Conversation):
            result_type = "conversation"
            result_ref = result.conversation_ref
            occurred_at = result.last_message_at
        elif isinstance(result, ThreadSuggestion):
            result_type = "suggestion"
            result_ref = result.suggestion_ref
            occurred_at = result.reviewed_at or result.created_at
        elif isinstance(result, Draft):
            result_type = "draft"
            result_ref = result.draft_ref
            occurred_at = result.updated_at
        elif isinstance(result, InboxItem):
            result_type = "inbox"
            result_ref = result.inbox_item_ref
            occurred_at = result.updated_at
        else:
            raise IdempotencyConflict("workflow replay type conflict")
        marker_key = _workflow_key(
            scope.site_id,
            scope.processing_purpose,
            idempotency_key,
        )
        event = AuditEvent(
            audit_ref=stable_ref("AUD", scope.site_id, marker_key),
            site_id=scope.site_id,
            actor_ref="email-gateway-workflow",
            event_type=f"workflow_idempotency_{result_type}",
            subject_ref=result_ref,
            request_id=marker_key,
            idempotency_key=marker_key,
            payload_digest=payload_digest,
            occurred_at=occurred_at,
        )
        PostgresAuditRepository(self.connection).append(scope, event)

    def audit_count(self, scope: TenantScope) -> int:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT count(*)
                  FROM email_gateway.audit_events
                 WHERE site_id = %s
                   AND event_type NOT LIKE 'workflow_idempotency_%%'
                """,
                (scope.site_id,),
            )
            row = cursor.fetchone()
            return 0 if row is None else int(row[0])

    @staticmethod
    def _load_inbox(
        cursor: object,
        scope: TenantScope,
        inbox_ref: str,
        *,
        lock: bool = False,
    ) -> InboxItem | None:
        query = """
            SELECT inbox.inbox_item_ref, inbox.site_id, inbox.mailbox_ref,
                   inbox.message_ref, inbox.team_ref, inbox.assignee_user_ref,
                   inbox.priority, inbox.sla_due_at, inbox.state,
                   inbox.conversation_ref, inbox.business_links, inbox.revision,
                   inbox.received_at, inbox.updated_at
              FROM email_gateway.inbox_items AS inbox
              JOIN email_gateway.mailboxes AS mailbox
                ON mailbox.site_id = inbox.site_id
               AND mailbox.mailbox_ref = inbox.mailbox_ref
             WHERE inbox.site_id = %s AND inbox.inbox_item_ref = %s
               AND mailbox.business_purpose = %s
        """
        if lock:
            query += " FOR UPDATE"
        cursor.execute(  # type: ignore[attr-defined]
            query,
            (scope.site_id, inbox_ref, scope.processing_purpose),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        return None if row is None else _inbox_from_row(row)

    @staticmethod
    def _load_suggestion(
        cursor: object,
        scope: TenantScope,
        suggestion_ref: str,
        *,
        lock: bool = False,
    ) -> ThreadSuggestion | None:
        query = """
            SELECT suggestion_ref, site_id, team_ref, left_inbox_ref,
                   right_inbox_ref, signals, confidence, status, revision,
                   reviewed_by, reviewed_at, created_at
              FROM email_gateway.thread_suggestions AS suggestion
             WHERE suggestion.site_id = %s AND suggestion.suggestion_ref = %s
               AND EXISTS (
                   SELECT 1
                     FROM email_gateway.inbox_items AS inbox
                     JOIN email_gateway.mailboxes AS mailbox
                       ON mailbox.site_id = inbox.site_id
                      AND mailbox.mailbox_ref = inbox.mailbox_ref
                    WHERE inbox.site_id = suggestion.site_id
                      AND inbox.inbox_item_ref = suggestion.left_inbox_ref
                      AND mailbox.business_purpose = %s
               )
        """
        if lock:
            query += " FOR UPDATE"
        cursor.execute(  # type: ignore[attr-defined]
            query,
            (scope.site_id, suggestion_ref, scope.processing_purpose),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        return None if row is None else _suggestion_from_row(row)

    @staticmethod
    def _load_conversation(
        cursor: object,
        scope: TenantScope,
        conversation_ref: str,
        *,
        lock: bool = False,
    ) -> Conversation | None:
        query = """
            SELECT conversation_ref, site_id, team_ref, party_ref, contact_ref,
                   owner_user_ref, lifecycle_state, first_message_at,
                   last_message_at, revision
              FROM email_gateway.conversations AS conversation
             WHERE conversation.site_id = %s AND conversation.conversation_ref = %s
               AND EXISTS (
                   SELECT 1
                     FROM email_gateway.conversation_messages AS member
                     JOIN email_gateway.inbox_items AS inbox
                       ON inbox.site_id = member.site_id
                      AND inbox.inbox_item_ref = member.inbox_item_ref
                     JOIN email_gateway.mailboxes AS mailbox
                       ON mailbox.site_id = inbox.site_id
                      AND mailbox.mailbox_ref = inbox.mailbox_ref
                    WHERE member.site_id = conversation.site_id
                      AND member.conversation_ref = conversation.conversation_ref
                      AND mailbox.business_purpose = %s
               )
        """
        if lock:
            query += " FOR UPDATE"
        cursor.execute(  # type: ignore[attr-defined]
            query,
            (scope.site_id, conversation_ref, scope.processing_purpose),
        )
        row = cursor.fetchone()  # type: ignore[attr-defined]
        if row is None:
            return None
        cursor.execute(  # type: ignore[attr-defined]
            """
            SELECT message_ref, inbox_item_ref
              FROM email_gateway.conversation_messages
             WHERE site_id = %s AND conversation_ref = %s
             ORDER BY ordinal
            """,
            (scope.site_id, conversation_ref),
        )
        membership = cursor.fetchall()  # type: ignore[attr-defined]
        return Conversation(
            conversation_ref=str(row[0]),
            site_id=str(row[1]),
            team_ref=str(row[2]),
            party_ref=None if row[3] is None else str(row[3]),
            contact_ref=None if row[4] is None else str(row[4]),
            owner_user_ref=None if row[5] is None else str(row[5]),
            lifecycle_state=str(row[6]),
            first_message_at=row[7],
            last_message_at=row[8],
            message_refs=tuple(str(item[0]) for item in membership),
            inbox_item_refs=tuple(str(item[1]) for item in membership),
            revision=int(row[9]),
        )


def _inbox_params(inbox: InboxItem) -> tuple[object, ...]:
    return (
        inbox.site_id,
        inbox.inbox_item_ref,
        inbox.mailbox_ref,
        inbox.message_ref,
        inbox.team_ref,
        inbox.assignee_user_ref,
        inbox.priority,
        inbox.sla_due_at,
        inbox.state,
        inbox.conversation_ref,
        list(inbox.business_links),
        inbox.revision,
        inbox.received_at,
        inbox.updated_at,
    )


def _inbox_from_row(row: tuple[Any, ...]) -> InboxItem:
    return InboxItem(
        inbox_item_ref=str(row[0]),
        site_id=str(row[1]),
        mailbox_ref=str(row[2]),
        message_ref=str(row[3]),
        team_ref=str(row[4]),
        assignee_user_ref=None if row[5] is None else str(row[5]),
        priority=int(row[6]),
        sla_due_at=row[7],
        state=str(row[8]),
        conversation_ref=None if row[9] is None else str(row[9]),
        business_links=tuple(str(item) for item in row[10]),
        revision=int(row[11]),
        received_at=row[12],
        updated_at=row[13],
    )


def _suggestion_from_row(row: tuple[Any, ...]) -> ThreadSuggestion:
    return ThreadSuggestion(
        suggestion_ref=str(row[0]),
        site_id=str(row[1]),
        team_ref=str(row[2]),
        left_inbox_ref=str(row[3]),
        right_inbox_ref=str(row[4]),
        signals=tuple(str(item) for item in row[5]),
        confidence=float(row[6]),
        status=str(row[7]),
        revision=int(row[8]),
        reviewed_by=None if row[9] is None else str(row[9]),
        reviewed_at=row[10],
        created_at=row[11],
    )


def _draft_from_row(row: tuple[Any, ...]) -> Draft:
    return Draft(
        draft_ref=str(row[0]),
        site_id=str(row[1]),
        inbox_item_ref=str(row[2]),
        conversation_ref=None if row[3] is None else str(row[3]),
        content_evidence_ref=str(row[4]),
        content_digest=str(row[5]),
        revision=int(row[6]),
        state=str(row[7]),
        updated_at=row[8],
    )


def _workflow_key(site_id: str, processing_purpose: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"workflow-idempotency-v1\x1f{site_id}\x1f{processing_purpose}"
        f"\x1f{idempotency_key}".encode()
    ).hexdigest()
    return f"workflow:{digest}"


def _lock_workflow_ref(
    cursor: object,
    scope: TenantScope,
    namespace: str,
    value: str,
) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        """
        SELECT pg_advisory_xact_lock(
            hashtextextended(%s || chr(31) || %s || chr(31) || %s || chr(31) || %s, 0)
        )
        """,
        (scope.site_id, scope.processing_purpose, namespace, value),
    )


def _require_mailbox_scope(
    cursor: object,
    scope: TenantScope,
    mailbox_ref: str,
) -> None:
    cursor.execute(  # type: ignore[attr-defined]
        """
        SELECT 1
          FROM email_gateway.mailboxes
         WHERE site_id = %s AND business_purpose = %s AND mailbox_ref = %s
        """,
        (scope.site_id, scope.processing_purpose, mailbox_ref),
    )
    if cursor.fetchone() is None:  # type: ignore[attr-defined]
        raise ScopeViolation("mailbox processing purpose mismatch")


__all__ = ["InMemoryWorkflowRepository", "PostgresWorkflowRepository"]
