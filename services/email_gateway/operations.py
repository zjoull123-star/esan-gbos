from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from .models import (
    AuditEvent,
    AuthorizationError,
    GatewayActorScope,
    InboxItem,
    RevisionConflict,
    ScopeViolation,
    TenantScope,
    ValidationError,
    canonical_digest,
    stable_ref,
)

_SALES_ROLE = "Sales User"
_SUPERVISOR_ROLES = frozenset({"Sales Manager", "Reviewer"})
_IDENTITY_ROUTE_WORKERS = frozenset({"identity_worker", "routing_worker"})
_HUMAN_REQUEST_STATES = frozenset({"waiting_internal", "converted", "closed"})
_OUTBOUND_STATES = frozenset({"send_queued", "send_uncertain", "waiting_customer"})
_BUSINESS_PREFIXES = ("PTY-", "CNT-", "CRM-LEAD-", "CRM-DEAL-")


class WorkflowRepository(Protocol):
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

    def replay(
        self, scope: TenantScope, idempotency_key: str, payload_digest: str
    ) -> object | None: ...


class InboxOperations:
    """Human Inbox commands; identity confirmation and outbound execution are absent by design."""

    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    def claim(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        inbox_item_ref: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "claim",
            "actor_ref": actor.actor_ref,
            "inbox_item_ref": inbox_item_ref,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }
        return self._execute(
            scope,
            actor=actor,
            actor_enabled=actor_enabled,
            required_roles=frozenset({_SALES_ROLE}),
            inbox_item_ref=inbox_item_ref,
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type="inbox_claimed",
            now=now,
            change=lambda inbox: self._claim_change(inbox, actor),
        )

    def reassign(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        inbox_item_ref: str,
        assignee_user_ref: str | None,
        assignee_team_ref: str,
        assignee_enabled: bool,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        target_state = "assigned" if assignee_user_ref is not None else "unassigned"
        payload = {
            "operation": "reassign",
            "actor_ref": actor.actor_ref,
            "inbox_item_ref": inbox_item_ref,
            "assignee_user_ref": assignee_user_ref,
            "assignee_team_ref": assignee_team_ref,
            "assignee_enabled": assignee_enabled,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            self._ensure_mutable(inbox)
            if assignee_team_ref != inbox.team_ref:
                raise ScopeViolation("assignee team mismatch")
            if assignee_user_ref is not None and not assignee_enabled:
                raise AuthorizationError("disabled assignee is not eligible")
            return replace(inbox, state=target_state, assignee_user_ref=assignee_user_ref)

        return self._execute(
            scope,
            actor=actor,
            actor_enabled=actor_enabled,
            required_roles=_SUPERVISOR_ROLES,
            inbox_item_ref=inbox_item_ref,
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type="inbox_reassigned",
            now=now,
            change=change,
        )

    def transition(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        inbox_item_ref: str,
        target_state: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "transition",
            "actor_ref": actor.actor_ref,
            "inbox_item_ref": inbox_item_ref,
            "target_state": target_state,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            self._ensure_mutable(inbox)
            if inbox.assignee_user_ref != actor.actor_ref:
                raise AuthorizationError("only the current assignee may transition the item")
            allowed = (
                (inbox.state == "assigned" and target_state == "draft")
                or (inbox.state == "draft" and target_state == "assigned")
                or (inbox.state in {"assigned", "draft"} and target_state in _HUMAN_REQUEST_STATES)
            )
            if target_state in _OUTBOUND_STATES or not allowed:
                raise AuthorizationError("inbox transition is not authorized")
            return replace(inbox, state=target_state)

        return self._execute(
            scope,
            actor=actor,
            actor_enabled=actor_enabled,
            required_roles=frozenset({_SALES_ROLE}),
            inbox_item_ref=inbox_item_ref,
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type="inbox_transitioned",
            now=now,
            change=change,
        )

    def reopen(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        inbox_item_ref: str,
        target_state: str,
        assignee_user_ref: str | None,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "reopen",
            "actor_ref": actor.actor_ref,
            "inbox_item_ref": inbox_item_ref,
            "target_state": target_state,
            "assignee_user_ref": assignee_user_ref,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            if inbox.state != "closed" or target_state not in {"assigned", "unassigned"}:
                raise AuthorizationError("only a closed item may be reopened")
            if (target_state == "assigned") != (assignee_user_ref is not None):
                raise ValidationError("reopen assignee does not match target state")
            return replace(
                inbox,
                state=target_state,
                assignee_user_ref=assignee_user_ref,
            )

        return self._execute(
            scope,
            actor=actor,
            actor_enabled=actor_enabled,
            required_roles=_SUPERVISOR_ROLES,
            inbox_item_ref=inbox_item_ref,
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type="inbox_reopened",
            now=now,
            change=change,
        )

    def apply_identity_route(
        self,
        scope: TenantScope,
        *,
        worker_kind: str,
        inbox_item_ref: str,
        target_state: str,
        assignee_user_ref: str | None,
        assignee_team_ref: str | None,
        assignee_enabled: bool,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "identity_route",
            "worker_kind": worker_kind,
            "inbox_item_ref": inbox_item_ref,
            "target_state": target_state,
            "assignee_user_ref": assignee_user_ref,
            "assignee_team_ref": assignee_team_ref,
            "assignee_enabled": assignee_enabled,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }
        digest = canonical_digest(payload)
        replay = self.repository.replay(scope, idempotency_key, digest)
        if replay is not None:
            return self._inbox_replay(replay)
        if worker_kind not in _IDENTITY_ROUTE_WORKERS:
            raise AuthorizationError("identity/routing worker required")
        inbox = self._load_revision(scope, inbox_item_ref, expected_revision)
        if inbox.state != "identity_pending" or target_state not in {"unassigned", "assigned"}:
            raise AuthorizationError("identity/routing transition is not authorized")
        if target_state == "assigned":
            if (
                assignee_user_ref is None
                or assignee_team_ref != inbox.team_ref
                or not assignee_enabled
            ):
                raise AuthorizationError("routing assignee is not eligible")
        elif assignee_user_ref is not None or assignee_team_ref is not None:
            raise ValidationError("unassigned route cannot name an assignee")
        result = self._persist(
            scope,
            inbox,
            replace(inbox, state=target_state, assignee_user_ref=assignee_user_ref),
            actor_ref=f"email-gateway-{worker_kind}",
            event_type="inbox_identity_routed",
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload_digest=digest,
            now=now,
        )
        return result

    def link_business(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        inbox_item_ref: str,
        business_ref: str,
        authority_valid: bool,
        authority_team_ref: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "link_business",
            "actor_ref": actor.actor_ref,
            "inbox_item_ref": inbox_item_ref,
            "business_ref": business_ref,
            "authority_valid": authority_valid,
            "authority_team_ref": authority_team_ref,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            self._ensure_mutable(inbox)
            if not authority_valid:
                raise AuthorizationError("closed Frappe authority validation required")
            if authority_team_ref != inbox.team_ref:
                raise ScopeViolation("business authority team mismatch")
            if not business_ref.startswith(_BUSINESS_PREFIXES):
                raise ValidationError("unsupported existing business reference")
            if business_ref in inbox.business_links:
                return inbox
            return replace(inbox, business_links=(*inbox.business_links, business_ref))

        return self._execute(
            scope,
            actor=actor,
            actor_enabled=actor_enabled,
            required_roles=frozenset({_SALES_ROLE}) | _SUPERVISOR_ROLES,
            inbox_item_ref=inbox_item_ref,
            expected_revision=expected_revision,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            event_type="inbox_business_linked",
            now=now,
            change=change,
        )

    def _execute(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        actor_enabled: bool,
        required_roles: frozenset[str],
        inbox_item_ref: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        event_type: str,
        now: datetime,
        change: Callable[[InboxItem], InboxItem],
    ) -> InboxItem:
        self._authorize_actor(scope, actor, actor_enabled, required_roles)
        scoped_inbox = self.repository.get_inbox(scope, inbox_item_ref)
        if scoped_inbox is None:
            raise ValidationError("inbox item not found")
        if scoped_inbox.team_ref not in actor.team_refs:
            raise ScopeViolation("actor team mismatch")
        digest = canonical_digest(payload)
        replay = self.repository.replay(scope, idempotency_key, digest)
        if replay is not None:
            return self._inbox_replay(replay)
        inbox = self._require_revision(scoped_inbox, expected_revision)
        changed = change(inbox)
        return self._persist(
            scope,
            inbox,
            changed,
            actor_ref=actor.actor_ref,
            event_type=event_type,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload_digest=digest,
            now=now,
        )

    @staticmethod
    def _authorize_actor(
        scope: TenantScope,
        actor: GatewayActorScope,
        actor_enabled: bool,
        required_roles: frozenset[str],
    ) -> None:
        if actor.site_id != scope.site_id:
            raise ScopeViolation("actor site mismatch")
        if not actor_enabled:
            raise AuthorizationError("disabled actor")
        if required_roles.isdisjoint(actor.roles):
            role_names = "/".join(sorted(required_roles))
            raise AuthorizationError(f"operation requires {role_names}")

    def _load_revision(
        self, scope: TenantScope, inbox_item_ref: str, expected_revision: int
    ) -> InboxItem:
        inbox = self.repository.get_inbox(scope, inbox_item_ref)
        if inbox is None:
            raise ValidationError("inbox item not found")
        return self._require_revision(inbox, expected_revision)

    @staticmethod
    def _require_revision(inbox: InboxItem, expected_revision: int) -> InboxItem:
        if inbox.revision != expected_revision:
            raise RevisionConflict("inbox revision conflict")
        return inbox

    @staticmethod
    def _claim_change(inbox: InboxItem, actor: GatewayActorScope) -> InboxItem:
        if inbox.state != "unassigned" or inbox.assignee_user_ref is not None:
            raise AuthorizationError("only an unassigned item may be claimed")
        return replace(inbox, state="assigned", assignee_user_ref=actor.actor_ref)

    @staticmethod
    def _ensure_mutable(inbox: InboxItem) -> None:
        if inbox.state == "quarantined":
            raise AuthorizationError("quarantined evidence is read-only")
        if inbox.state in _OUTBOUND_STATES:
            raise AuthorizationError("outbound state is reserved")

    def _persist(
        self,
        scope: TenantScope,
        before: InboxItem,
        changed: InboxItem,
        *,
        actor_ref: str,
        event_type: str,
        request_id: str,
        idempotency_key: str,
        payload_digest: str,
        now: datetime,
    ) -> InboxItem:
        if now < before.updated_at:
            raise ValidationError("inbox operation clock regression")
        revised = replace(changed, revision=before.revision + 1, updated_at=now)
        audit_event = AuditEvent(
            audit_ref=stable_ref("AUD", scope.site_id, request_id),
            site_id=scope.site_id,
            actor_ref=actor_ref,
            event_type=event_type,
            subject_ref=revised.inbox_item_ref,
            request_id=request_id,
            idempotency_key=f"audit:{idempotency_key}",
            payload_digest=payload_digest,
            occurred_at=now,
        )
        return self.repository.apply_inbox_operation(
            scope,
            before=before,
            revised=revised,
            audit_event=audit_event,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    @staticmethod
    def _inbox_replay(value: object) -> InboxItem:
        if not isinstance(value, InboxItem):
            raise ValidationError("workflow replay type conflict")
        return value
