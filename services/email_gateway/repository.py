from __future__ import annotations

from typing import Protocol

from .models import (
    AuditEvent,
    Draft,
    GatewayActorScope,
    IdentityProjection,
    InboxItem,
    IntakeResult,
    Mailbox,
    MailboxChangeReceipt,
    TenantScope,
)
from .phase1_read import ConnectorHealth, Page, Phase1InboxItem, Phase1Mailbox


class MailboxRepository(Protocol):
    def get(self, scope: TenantScope, mailbox_ref: str) -> Mailbox | None: ...

    def list(self, scope: TenantScope) -> tuple[Mailbox, ...]: ...

    def upsert(
        self,
        scope: TenantScope,
        mailbox: Mailbox,
        *,
        expected_revision: int,
        actor_ref: str,
        request_id: str,
        idempotency_key: str,
    ) -> MailboxChangeReceipt: ...


class IntakeRepository(Protocol):
    def accept(self, scope: TenantScope, publication: object, mailbox: Mailbox) -> IntakeResult: ...


class IdentityProjectionRepository(Protocol):
    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None: ...

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection: ...


class WorkflowRepository(Protocol):
    def save_inbox(self, scope: TenantScope, inbox: InboxItem) -> InboxItem: ...

    def save_draft(
        self, scope: TenantScope, draft: Draft, *, idempotency_key: str, payload_digest: str
    ) -> Draft: ...


class AuditRepository(Protocol):
    def append(self, scope: TenantScope, event: AuditEvent) -> AuditEvent: ...


class Phase1ReadRepository(Protocol):
    def list_mailboxes(
        self, site_id: str, *, page_size: int, cursor: str | None
    ) -> Page[Phase1Mailbox]: ...

    def get_mailbox(self, site_id: str, mailbox_ref: str) -> Phase1Mailbox | None: ...

    def mailboxes_for_health(self, site_id: str) -> tuple[Phase1Mailbox, ...]: ...

    def list_inbox(
        self,
        actor: GatewayActorScope,
        *,
        state: str | None,
        page_size: int,
        cursor: str | None,
    ) -> Page[Phase1InboxItem]: ...

    def get_inbox(
        self, actor: GatewayActorScope, inbox_item_ref: str
    ) -> Phase1InboxItem | None: ...


class ConnectorHealthReader(Protocol):
    def read(
        self, site_id: str, mailboxes: tuple[Phase1Mailbox, ...]
    ) -> tuple[ConnectorHealth, ...]: ...
