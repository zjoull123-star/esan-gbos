from __future__ import annotations

from collections.abc import Mapping
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
from .sla import SlaClock


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


class ParticipantAuthorityBindingReader(Protocol):
    def load_participant_authority_binding(
        self,
        scope: TenantScope,
        *,
        inbox_item_ref: str,
    ) -> Mapping[str, object] | None: ...


class IdentityProjectionRepository(Protocol):
    def get(self, scope: TenantScope, opaque_address_ref: str) -> IdentityProjection | None: ...

    def apply(self, scope: TenantScope, projection: IdentityProjection) -> IdentityProjection: ...


class WorkflowRepository(Protocol):
    def save_inbox(self, scope: TenantScope, inbox: InboxItem) -> InboxItem: ...

    def save_draft(
        self, scope: TenantScope, draft: Draft, *, idempotency_key: str, payload_digest: str
    ) -> Draft: ...


class SlaOperationRepository(Protocol):
    """Atomic Inbox command persistence with its SLA and durable receipts."""

    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None: ...

    def get_sla(self, scope: TenantScope, inbox_ref: str) -> SlaClock | None: ...

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> InboxItem | None: ...

    def apply_inbox_sla_operation(
        self,
        scope: TenantScope,
        *,
        before: InboxItem,
        revised: InboxItem,
        sla_before: SlaClock,
        sla_revised: SlaClock,
        audit_event: AuditEvent,
        idempotency_key: str,
        payload_digest: str,
        authority_receipt: Mapping[str, object] | None = None,
    ) -> InboxItem: ...


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
