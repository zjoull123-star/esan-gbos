from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .models import (
    AuthorizationError,
    Draft,
    GatewayActorScope,
    RevisionConflict,
    ScopeViolation,
    TenantScope,
    ValidationError,
    canonical_digest,
    stable_ref,
)
from .repositories.workflow import InMemoryWorkflowRepository


class DraftService:
    def __init__(self, repository: InMemoryWorkflowRepository) -> None:
        self.repository = repository

    def create(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        inbox_item_ref: str,
        conversation_ref: str | None,
        content_evidence_ref: str,
        content_digest: str,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> Draft:
        self._authorize(scope, actor)
        payload_digest = canonical_digest(
            {
                "inbox_item_ref": inbox_item_ref,
                "conversation_ref": conversation_ref,
                "content_evidence_ref": content_evidence_ref,
                "content_digest": content_digest,
            }
        )
        draft = Draft(
            draft_ref=stable_ref("DRF", scope.site_id, inbox_item_ref),
            site_id=scope.site_id,
            inbox_item_ref=inbox_item_ref,
            conversation_ref=conversation_ref,
            content_evidence_ref=content_evidence_ref,
            content_digest=content_digest,
            revision=1,
            state="editable",
            updated_at=now,
        )
        return self.repository.save_draft(
            scope,
            draft,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    def update(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        draft_ref: str,
        expected_revision: int,
        content_evidence_ref: str,
        content_digest: str,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> Draft:
        self._authorize(scope, actor)
        current = self.repository.get_draft(scope, draft_ref)
        if current is None:
            raise ValidationError("draft not found")
        if current.revision != expected_revision:
            raise RevisionConflict("draft revision conflict")
        if current.state != "editable":
            raise ValidationError("draft is not editable")
        payload_digest = canonical_digest(
            {
                "draft_ref": draft_ref,
                "expected_revision": expected_revision,
                "content_evidence_ref": content_evidence_ref,
                "content_digest": content_digest,
            }
        )
        return self.repository.save_draft(
            scope,
            replace(
                current,
                content_evidence_ref=content_evidence_ref,
                content_digest=content_digest,
                revision=current.revision + 1,
                updated_at=now,
            ),
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    @staticmethod
    def _authorize(scope: TenantScope, actor: GatewayActorScope) -> None:
        if actor.site_id != scope.site_id:
            raise ScopeViolation("actor site mismatch")
        if "Email Gateway Worker" not in actor.roles:
            raise AuthorizationError("draft repository requires internal worker")
