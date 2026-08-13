from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from .conftest import DIGEST_A, NOW, SITE


def test_audit_append_is_immutable_and_idempotent(scope) -> None:
    from services.email_gateway.audit import AuditService
    from services.email_gateway.models import AuditEvent
    from services.email_gateway.repositories.audit import InMemoryAuditRepository

    service = AuditService(InMemoryAuditRepository())
    event = AuditEvent(
        audit_ref="AUD-01",
        site_id=SITE,
        actor_ref="manager@example.invalid",
        event_type="conversation_merge",
        subject_ref="CON-01",
        request_id="REQ-01",
        idempotency_key="audit-01",
        payload_digest=DIGEST_A,
        occurred_at=NOW,
    )
    assert service.append(scope, event) == event
    assert service.append(scope, event) == event
    with pytest.raises(FrozenInstanceError):
        event.event_type = "changed"  # type: ignore[misc]


def test_audit_repr_redacts_actor_and_subject(scope) -> None:
    from services.email_gateway.audit import AuditService
    from services.email_gateway.models import AuditEvent
    from services.email_gateway.repositories.audit import InMemoryAuditRepository

    event = AuditEvent(
        audit_ref="AUD-01",
        site_id=SITE,
        actor_ref="manager@example.invalid",
        event_type="conversation_merge",
        subject_ref="CON-SECRET-01",
        request_id="REQ-01",
        idempotency_key="audit-01",
        payload_digest=DIGEST_A,
        occurred_at=NOW,
    )
    AuditService(InMemoryAuditRepository()).append(scope, event)
    assert "manager@example.invalid" not in repr(event)
    assert "CON-SECRET-01" not in repr(event)
