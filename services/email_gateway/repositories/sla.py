from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Protocol

from ..models import (
    AuditEvent,
    IdempotencyConflict,
    InboxItem,
    RevisionConflict,
    TenantScope,
    canonical_digest,
    require_scope,
    stable_ref,
)
from ..postgres import Connection, redacted_database_errors, site_transaction
from ..sla import SlaClock


class _WorkflowDelegate(Protocol):
    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None: ...

    def apply_inbox_operation(
        self,
        scope: TenantScope,
        *,
        before: InboxItem,
        revised: InboxItem,
        audit_event: AuditEvent,
        idempotency_key: str,
        payload_digest: str,
    ) -> InboxItem: ...


class InMemorySlaRepository:
    """Atomic in-memory reference repository for Inbox and SLA transitions."""

    def __init__(
        self,
        *,
        transaction_failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self._inbox: dict[tuple[str, str], InboxItem] = {}
        self._sla: dict[tuple[str, str], SlaClock] = {}
        self._audit: list[AuditEvent] = []
        self._receipts: dict[tuple[str, str], tuple[str, InboxItem, SlaClock]] = {}
        self._authorities: dict[tuple[str, str], dict[str, object]] = {}
        self._failure = transaction_failure_injector
        self._lock = RLock()

    def save_inbox_with_sla(self, scope: TenantScope, inbox: InboxItem, sla: SlaClock) -> InboxItem:
        require_scope(scope, site_id=inbox.site_id)
        if sla.inbox_item_ref != inbox.inbox_item_ref:
            raise RevisionConflict("Inbox SLA binding conflict")
        with self._lock:
            self._inbox[(scope.site_id, inbox.inbox_item_ref)] = inbox
            self._sla[(scope.site_id, inbox.inbox_item_ref)] = sla
        return inbox

    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None:
        return self._inbox.get((scope.site_id, inbox_ref))

    def get_sla(self, scope: TenantScope, inbox_ref: str) -> SlaClock | None:
        return self._sla.get((scope.site_id, inbox_ref))

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> InboxItem | None:
        receipt = self._receipts.get((scope.site_id, idempotency_key))
        if receipt is None:
            return None
        prior_digest, inbox, _sla = receipt
        if prior_digest != payload_digest:
            raise IdempotencyConflict("SLA replay drift")
        return inbox

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
        authority_receipt: dict[str, object] | None = None,
    ) -> InboxItem:
        for value in (before, revised):
            require_scope(scope, site_id=value.site_id)
        require_scope(scope, site_id=audit_event.site_id)
        if not idempotency_key or not payload_digest:
            raise IdempotencyConflict("SLA replay drift")
        inbox_key = (scope.site_id, before.inbox_item_ref)
        receipt_key = (scope.site_id, idempotency_key)
        with self._lock:
            replay = self._receipts.get(receipt_key)
            if replay is not None:
                prior_digest, prior_inbox, prior_sla = replay
                if (
                    prior_digest != payload_digest
                    or prior_inbox != revised
                    or prior_sla != sla_revised
                ):
                    raise IdempotencyConflict("SLA replay drift")
                return prior_inbox
            current_inbox = self._inbox.get(inbox_key)
            current_sla = self._sla.get(inbox_key)
            if (
                current_inbox != before
                or current_sla != sla_before
                or revised.revision != before.revision + 1
                or sla_revised.inbox_item_ref != revised.inbox_item_ref
                or sla_revised.policy_ref != sla_before.policy_ref
                or sla_revised.policy_revision != sla_before.policy_revision
                or sla_revised.started_at != sla_before.started_at
                or sla_revised.due_at != sla_before.due_at
                or sla_revised.audit_revision < sla_before.audit_revision
            ):
                raise RevisionConflict("Inbox SLA revision conflict")
            audit_length = len(self._audit)
            try:
                self._inbox[inbox_key] = revised
                self._fail("after_inbox_write")
                self._sla[inbox_key] = sla_revised
                self._fail("after_sla_write")
                self._audit.append(audit_event)
                self._fail("after_audit_write")
                self._receipts[receipt_key] = (payload_digest, revised, sla_revised)
                if authority_receipt is not None:
                    self._authorities[receipt_key] = dict(authority_receipt)
                self._fail("after_receipt_write")
            except Exception:
                self._inbox[inbox_key] = before
                self._sla[inbox_key] = sla_before
                del self._audit[audit_length:]
                self._receipts.pop(receipt_key, None)
                self._authorities.pop(receipt_key, None)
                raise
            return revised

    def audit_count(self, scope: TenantScope) -> int:
        return sum(event.site_id == scope.site_id for event in self._audit)

    def _fail(self, phase: str) -> None:
        if self._failure is not None:
            self._failure(phase)


class PostgresSlaRepository:
    """Postgres seam for one Inbox CAS, SLA, audit, and authority transaction."""

    def __init__(self, connection: Connection, workflow: _WorkflowDelegate) -> None:
        self.connection = connection
        self.workflow = workflow

    def get_inbox(self, scope: TenantScope, inbox_ref: str) -> InboxItem | None:
        return self.workflow.get_inbox(scope, inbox_ref)

    def apply_inbox_operation(
        self,
        scope: TenantScope,
        *,
        before: InboxItem,
        revised: InboxItem,
        audit_event: AuditEvent,
        idempotency_key: str,
        payload_digest: str,
    ) -> InboxItem:
        return self.workflow.apply_inbox_operation(
            scope,
            before=before,
            revised=revised,
            audit_event=audit_event,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    def get_sla(self, scope: TenantScope, inbox_ref: str) -> SlaClock | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT inbox_item_ref, policy_ref, policy_revision, started_at, due_at,
                       status, completed_at, provider_accepted_receipt_ref,
                       closed_at, closed_outcome, audit_revision
                  FROM email_gateway.inbox_sla_clocks
                 WHERE site_id = %s AND inbox_item_ref = %s
                """,
                (scope.site_id, inbox_ref),
            )
            row = cursor.fetchone()
        return None if row is None else _sla_from_row(row)

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> InboxItem | None:
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT inbox_item_ref, payload_digest
                  FROM email_gateway.inbox_operation_requests
                 WHERE site_id = %s AND idempotency_key = %s
                """,
                (scope.site_id, idempotency_key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if str(row[1]) != payload_digest:
            raise IdempotencyConflict("SLA replay drift")
        result = self.get_inbox(scope, str(row[0]))
        if result is None:
            raise IdempotencyConflict("SLA replay result missing")
        return result

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
        authority_receipt: dict[str, object] | None = None,
    ) -> InboxItem:
        for item in (before, revised):
            require_scope(scope, site_id=item.site_id)
        require_scope(scope, site_id=audit_event.site_id)
        operation = _operation_type(audit_event.event_type)
        authority_ref = None
        authority_revision = None
        actor_ref_digest = None
        target_user_ref_digest = None
        business_ref = None
        if authority_receipt is not None:
            authority_ref = stable_ref("AUR", scope.site_id, idempotency_key)
            authority_revision = canonical_digest(authority_receipt)
            authority_actor_digest = authority_receipt.get("actor_ref_digest")
            if isinstance(authority_actor_digest, str):
                actor_ref_digest = authority_actor_digest
            authority_target_digest = authority_receipt.get("target_user_ref_digest")
            if isinstance(authority_target_digest, str):
                target_user_ref_digest = authority_target_digest
            business_ref = authority_receipt.get("business_ref")
        with redacted_database_errors(), site_transaction(self.connection, scope) as cursor:
            cursor.execute(
                """
                SELECT email_gateway.apply_inbox_sla_operation(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    before.inbox_item_ref,
                    before.revision,
                    revised.state,
                    revised.assignee_user_ref,
                    list(revised.business_links),
                    revised.updated_at,
                    sla_revised.policy_ref,
                    sla_revised.policy_revision,
                    sla_revised.started_at,
                    sla_revised.due_at,
                    sla_revised.status,
                    sla_revised.completed_at,
                    sla_revised.provider_accepted_receipt_ref,
                    sla_revised.closed_at,
                    sla_revised.closed_outcome,
                    sla_revised.audit_revision,
                    operation,
                    audit_event.event_type,
                    audit_event.audit_ref,
                    audit_event.actor_ref,
                    actor_ref_digest,
                    audit_event.request_id,
                    idempotency_key,
                    payload_digest,
                    authority_ref,
                    authority_revision,
                    target_user_ref_digest,
                    business_ref,
                ),
            )
            row = cursor.fetchone()
        if row is None or _database_integer(row[0]) != revised.revision:
            raise RevisionConflict("Inbox SLA revision conflict")
        return revised


def _sla_from_row(row: tuple[object, ...]) -> SlaClock:
    return SlaClock(
        inbox_item_ref=str(row[0]),
        policy_ref=str(row[1]),
        policy_revision=_database_integer(row[2]),
        started_at=row[3],  # type: ignore[arg-type]
        due_at=row[4],  # type: ignore[arg-type]
        status=str(row[5]),
        completed_at=row[6],  # type: ignore[arg-type]
        provider_accepted_receipt_ref=None if row[7] is None else str(row[7]),
        closed_at=row[8],  # type: ignore[arg-type]
        closed_outcome=None if row[9] is None else str(row[9]),
        audit_revision=_database_integer(row[10]),
    )


def _operation_type(event_type: str) -> str:
    values = {
        "inbox_claimed": "claim",
        "inbox_reassigned": "reassign",
        "inbox_transitioned": "transition",
        "inbox_reopened": "reopen",
        "inbox_identity_routed": "identity_route",
        "inbox_business_linked": "link_business",
    }
    try:
        return values[event_type]
    except KeyError:
        raise RevisionConflict("unsupported Inbox SLA operation") from None


def _database_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RevisionConflict("invalid Inbox SLA database revision")
    return value


__all__ = ["InMemorySlaRepository", "PostgresSlaRepository"]
