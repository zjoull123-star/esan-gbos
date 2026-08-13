from __future__ import annotations

from dataclasses import replace

import pytest

from .conftest import DIGEST_A


def _service(scope, mailbox):
    from services.email_gateway.intake import GatewayIntakeService
    from services.email_gateway.mailboxes import MailboxRegistry
    from services.email_gateway.repositories.intake import InMemoryIntakeRepository
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
    return GatewayIntakeService(InMemoryIntakeRepository(), registry)


def test_accept_publication_is_atomic_and_replay_stable(scope, mailbox, publication) -> None:
    service = _service(scope, mailbox)
    first = service.accept(scope, publication)
    second = service.accept(scope, publication)
    assert first == second
    assert first.receipt.mailbox_ref == mailbox.mailbox_ref
    assert first.inbox_item.message_ref == first.message.message_ref
    assert first.inbox_item.state == "identity_pending"
    binding = service.load_participant_authority_binding(
        scope, inbox_item_ref=first.inbox_item.inbox_item_ref
    )
    assert binding == service.load_participant_authority_binding(
        scope, inbox_item_ref=first.inbox_item.inbox_item_ref
    )
    assert binding is not None
    assert set(binding) == {
        "gateway_receipt_ref",
        "publication_ref",
        "inbox_item_ref",
        "message_ref",
        "mailbox_ref",
        "mailbox_config_revision",
        "observer_delivery_ref",
        "payload_digest",
        "participant_binding_digest",
        "evidence_binding_digest",
    }
    assert binding["mailbox_config_revision"] == 1
    service.mailboxes.upsert(
        scope,
        replace(mailbox, config_revision=2),
        expected_revision=1,
        actor_ref="admin@example.invalid",
        request_id="REQ-02",
        idempotency_key="mailbox-02",
    )
    assert (
        service.load_participant_authority_binding(
            scope, inbox_item_ref=first.inbox_item.inbox_item_ref
        )
        == binding
    )


def test_publication_digest_drift_conflicts(scope, mailbox, publication) -> None:
    from services.email_gateway.models import IdempotencyConflict

    service = _service(scope, mailbox)
    service.accept(scope, publication)
    with pytest.raises(IdempotencyConflict):
        service.accept(scope, replace(publication, payload_digest=DIGEST_A))


def test_same_message_at_two_mailboxes_creates_independent_inbox_items(
    scope, mailbox, publication
) -> None:
    from services.email_gateway.intake import GatewayIntakeService
    from services.email_gateway.mailboxes import MailboxRegistry
    from services.email_gateway.repositories.intake import InMemoryIntakeRepository
    from services.email_gateway.repositories.mailboxes import InMemoryMailboxRepository

    registry = MailboxRegistry(InMemoryMailboxRepository())
    intake = InMemoryIntakeRepository()
    service = GatewayIntakeService(intake, registry)
    for index, current in enumerate(
        (
            mailbox,
            replace(
                mailbox,
                mailbox_ref="MBX-02",
                provider_account_ref="provider-account-02",
                observer_connector_instance_ref="observer-email-02",
                address_display="second@company.invalid",
            ),
        ),
        1,
    ):
        registry.upsert(
            scope,
            current,
            expected_revision=0,
            actor_ref="admin@example.invalid",
            request_id=f"REQ-MBX-{index}",
            idempotency_key=f"mailbox-{index}",
        )
    first = service.accept(scope, publication)
    second = service.accept(
        scope,
        replace(
            publication,
            publication_ref="PUB-02",
            mailbox_ref="MBX-02",
            observer_connector_instance_ref="observer-email-02",
            observer_delivery_ref="DEL-02",
            evidence_refs=("EVD-RAW-02",),
            idempotency_key="publication-02",
        ),
    )
    assert first.message.message_id_digest == second.message.message_id_digest
    assert first.inbox_item.inbox_item_ref != second.inbox_item.inbox_item_ref
    assert first.receipt.mailbox_ref != second.receipt.mailbox_ref
    first_binding = service.load_participant_authority_binding(
        scope, inbox_item_ref=first.inbox_item.inbox_item_ref
    )
    second_binding = service.load_participant_authority_binding(
        scope, inbox_item_ref=second.inbox_item.inbox_item_ref
    )
    assert first_binding is not None and second_binding is not None
    assert first_binding["evidence_binding_digest"] != second_binding["evidence_binding_digest"]


def test_stale_mailbox_revision_fails_before_writes(scope, mailbox, publication) -> None:
    from services.email_gateway.models import RevisionConflict

    service = _service(scope, mailbox)
    with pytest.raises(RevisionConflict):
        service.accept(scope, replace(publication, mailbox_config_revision=2))
    assert service.repository.counts(scope) == (0, 0, 0)
