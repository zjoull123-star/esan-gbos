from __future__ import annotations

from dataclasses import replace

import pytest


def test_registry_allows_multiple_primary_mailboxes(scope, mailbox) -> None:
    from services.email_gateway.mailboxes import MailboxRegistry
    from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository

    registry = MailboxRegistry(InMemoryMailboxRepository())
    first = registry.upsert(
        scope,
        mailbox,
        expected_revision=0,
        actor_ref="admin@example.invalid",
        request_id="REQ-01",
        idempotency_key="mailbox-01",
    )
    second_mailbox = replace(
        mailbox,
        mailbox_ref="MBX-02",
        observer_connector_instance_ref="observer-email-02",
        provider_account_ref="provider-account-02",
        address_display="second@company.invalid",
        config_revision=1,
    )
    second = registry.upsert(
        scope,
        second_mailbox,
        expected_revision=0,
        actor_ref="admin@example.invalid",
        request_id="REQ-02",
        idempotency_key="mailbox-02",
    )
    assert first.mailbox.entry_role == second.mailbox.entry_role == "primary"
    assert [item.mailbox_ref for item in registry.list(scope)] == ["MBX-01", "MBX-02"]


def test_mailbox_security_change_requires_cas_and_increments_revision(scope, mailbox) -> None:
    from services.email_gateway.mailboxes import MailboxRegistry
    from services.email_gateway.models import RevisionConflict
    from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository

    registry = MailboxRegistry(InMemoryMailboxRepository())
    registry.upsert(
        scope,
        mailbox,
        expected_revision=0,
        actor_ref="admin@example.invalid",
        request_id="REQ-01",
        idempotency_key="mailbox-01",
    )
    changed = replace(mailbox, inbound_enabled=False, status="paused")
    with pytest.raises(RevisionConflict):
        registry.upsert(
            scope,
            changed,
            expected_revision=0,
            actor_ref="admin@example.invalid",
            request_id="REQ-02",
            idempotency_key="mailbox-02",
        )
    receipt = registry.upsert(
        scope,
        changed,
        expected_revision=1,
        actor_ref="admin@example.invalid",
        request_id="REQ-02",
        idempotency_key="mailbox-02",
    )
    assert receipt.mailbox.config_revision == 2
    assert receipt.config_publication_ref


def test_mailbox_idempotency_replays_but_digest_drift_conflicts(scope, mailbox) -> None:
    from services.email_gateway.mailboxes import MailboxRegistry
    from services.email_gateway.models import IdempotencyConflict
    from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository

    registry = MailboxRegistry(InMemoryMailboxRepository())
    kwargs = dict(
        expected_revision=0,
        actor_ref="admin@example.invalid",
        request_id="REQ-01",
        idempotency_key="mailbox-01",
    )
    original = registry.upsert(scope, mailbox, **kwargs)
    assert registry.upsert(scope, mailbox, **kwargs) == original
    with pytest.raises(IdempotencyConflict):
        registry.upsert(scope, replace(mailbox, priority=20), **kwargs)
