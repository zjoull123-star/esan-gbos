from __future__ import annotations

import pytest

from .conftest import DIGEST_A, DIGEST_B, NOW, SITE


def test_draft_create_and_edit_are_internal_revisioned_operations(scope) -> None:
    from services.email_gateway.drafts import DraftService
    from services.email_gateway.models import GatewayActorScope
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    service = DraftService(InMemoryWorkflowRepository())
    actor = GatewayActorScope(
        site_id=SITE,
        actor_ref="worker:email-draft",
        team_refs=("TEM-01",),
        roles=("Email Gateway Worker",),
    )
    draft = service.create(
        scope,
        actor=actor,
        inbox_item_ref="INB-01",
        conversation_ref=None,
        content_evidence_ref="EVD-DRAFT-01",
        content_digest=DIGEST_A,
        request_id="REQ-DRF-01",
        idempotency_key="draft-01",
        now=NOW,
    )
    edited = service.update(
        scope,
        actor=actor,
        draft_ref=draft.draft_ref,
        expected_revision=1,
        content_evidence_ref="EVD-DRAFT-02",
        content_digest=DIGEST_B,
        request_id="REQ-DRF-02",
        idempotency_key="draft-02",
        now=NOW,
    )
    assert draft.revision == 1
    assert edited.revision == 2
    assert draft.content_digest == DIGEST_A


def test_sales_actor_cannot_call_internal_draft_repository(scope) -> None:
    from services.email_gateway.drafts import DraftService
    from services.email_gateway.models import AuthorizationError, GatewayActorScope
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    service = DraftService(InMemoryWorkflowRepository())
    actor = GatewayActorScope(
        site_id=SITE,
        actor_ref="sales@example.invalid",
        team_refs=("TEM-01",),
        roles=("Sales User",),
    )
    with pytest.raises(AuthorizationError):
        service.create(
            scope,
            actor=actor,
            inbox_item_ref="INB-01",
            conversation_ref=None,
            content_evidence_ref="EVD-DRAFT-01",
            content_digest=DIGEST_A,
            request_id="REQ-DRF-01",
            idempotency_key="draft-01",
            now=NOW,
        )
