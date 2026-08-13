from __future__ import annotations

from collections.abc import Mapping

from .mailboxes import MailboxRegistry
from .models import (
    EmailMessagePublication,
    IntakeResult,
    RevisionConflict,
    TenantScope,
    ValidationError,
    require_scope,
)
from .repository import IntakeRepository


class GatewayIntakeService:
    def __init__(self, repository: IntakeRepository, mailboxes: MailboxRegistry) -> None:
        self.repository = repository
        self.mailboxes = mailboxes

    def accept(self, scope: TenantScope, publication: EmailMessagePublication) -> IntakeResult:
        require_scope(
            scope,
            site_id=publication.site_id,
            processing_purpose=publication.processing_purpose,
        )
        mailbox = self.mailboxes.get(scope, publication.mailbox_ref)
        if mailbox is None:
            raise ValidationError("mailbox not found")
        if mailbox.config_revision != publication.mailbox_config_revision:
            raise RevisionConflict("mailbox publication revision conflict")
        if mailbox.observer_connector_instance_ref != publication.observer_connector_instance_ref:
            raise RevisionConflict("observer connector projection conflict")
        if mailbox.status != "active" or not mailbox.inbound_enabled:
            raise ValidationError("mailbox is not accepting publications")
        return self.repository.accept(scope, publication, mailbox)

    def load_participant_authority_binding(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> Mapping[str, object] | None:
        reader = getattr(self.repository, "load_participant_authority_binding", None)
        if not callable(reader):
            return None
        value = reader(scope, inbox_item_ref=inbox_item_ref)
        return value if isinstance(value, Mapping) else None
