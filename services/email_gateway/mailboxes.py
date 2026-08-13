from __future__ import annotations

from .models import Mailbox, MailboxChangeReceipt, TenantScope
from .repository import MailboxRepository


class MailboxRegistry:
    def __init__(self, repository: MailboxRepository) -> None:
        self.repository = repository

    def get(self, scope: TenantScope, mailbox_ref: str) -> Mailbox | None:
        return self.repository.get(scope, mailbox_ref)

    def list(self, scope: TenantScope) -> tuple[Mailbox, ...]:
        return self.repository.list(scope)

    def upsert(
        self,
        scope: TenantScope,
        mailbox: Mailbox,
        *,
        expected_revision: int,
        actor_ref: str,
        request_id: str,
        idempotency_key: str,
    ) -> MailboxChangeReceipt:
        return self.repository.upsert(
            scope,
            mailbox,
            expected_revision=expected_revision,
            actor_ref=actor_ref,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
