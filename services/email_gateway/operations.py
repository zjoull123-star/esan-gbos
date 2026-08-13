from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, cast

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
_AUTHORITY_COMMANDS = frozenset({"claim", "reassign", "link_business"})


@dataclass(frozen=True, slots=True)
class InboxCommandAuthority:
    """Closed, server-derived Frappe authority evidence for one Inbox command."""

    schema_version: str
    command: str
    actor_ref_digest: str
    actor_roles: tuple[str, ...]
    actor_team_refs: tuple[str, ...]
    actor_eligibility_revision: str
    inbox_item_ref: str
    expected_inbox_revision: int
    target_user_ref_digest: str | None = None
    target_team_refs: tuple[str, ...] = ()
    target_eligibility_revision: str | None = None
    business_ref: str | None = None
    business_team_ref: str | None = None
    business_revision: int | str | None = None

    def __post_init__(self) -> None:
        if (
            self.schema_version != "1.0"
            or self.command not in _AUTHORITY_COMMANDS
            or not _digest_valid(self.actor_ref_digest)
            or not self.actor_roles
            or not all(_wire_text_valid(role) for role in self.actor_roles)
            or len(self.actor_roles) != len(set(self.actor_roles))
            or not self.actor_team_refs
            or not all(_wire_text_valid(team) for team in self.actor_team_refs)
            or len(self.actor_team_refs) != len(set(self.actor_team_refs))
            or not _digest_valid(self.actor_eligibility_revision)
            or not _wire_text_valid(self.inbox_item_ref)
            or isinstance(self.expected_inbox_revision, bool)
            or self.expected_inbox_revision < 1
        ):
            raise ValidationError("invalid Inbox command authority receipt")
        if (self.target_user_ref_digest is None) != (self.target_eligibility_revision is None):
            raise ValidationError("invalid target authority binding")
        if self.target_user_ref_digest is None and self.target_team_refs:
            raise ValidationError("invalid target authority teams")
        if self.target_user_ref_digest is not None and not self.target_team_refs:
            raise ValidationError("invalid target authority teams")
        if self.target_user_ref_digest is not None and (
            not _digest_valid(self.target_user_ref_digest)
            or not all(_wire_text_valid(team) for team in self.target_team_refs)
        ):
            raise ValidationError("invalid target authority binding")
        if self.target_eligibility_revision is not None and not _digest_valid(
            self.target_eligibility_revision
        ):
            raise ValidationError("invalid target authority revision")
        if (self.business_ref is None) != (self.business_revision is None):
            raise ValidationError("invalid business authority binding")
        if (self.business_ref is None) != (self.business_team_ref is None):
            raise ValidationError("invalid business authority team")
        if self.business_ref is not None:
            revision_valid = (
                isinstance(self.business_revision, int)
                and not isinstance(self.business_revision, bool)
                and self.business_revision >= 1
            ) or _digest_valid(self.business_revision)
            if (
                not _wire_text_valid(self.business_ref)
                or not _wire_text_valid(self.business_team_ref)
                or not revision_valid
            ):
                raise ValidationError("invalid business authority binding")

    @classmethod
    def from_wire(cls, value: object) -> InboxCommandAuthority:
        fields = {
            "schema_version",
            "command",
            "actor_ref_digest",
            "actor_roles",
            "actor_team_refs",
            "actor_eligibility_revision",
            "inbox_item_ref",
            "expected_inbox_revision",
            "target_user_ref_digest",
            "target_team_refs",
            "target_eligibility_revision",
            "business_ref",
            "business_team_ref",
            "business_revision",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValidationError("invalid Inbox command authority receipt")
        roles = value.get("actor_roles")
        actor_teams = value.get("actor_team_refs")
        target_teams = value.get("target_team_refs")
        if (
            not all(
                isinstance(value.get(field), str)
                for field in (
                    "schema_version",
                    "command",
                    "actor_ref_digest",
                    "actor_eligibility_revision",
                    "inbox_item_ref",
                )
            )
            or isinstance(value.get("expected_inbox_revision"), bool)
            or not isinstance(value.get("expected_inbox_revision"), int)
            or not isinstance(roles, list)
            or not all(isinstance(role, str) for role in roles)
            or not isinstance(actor_teams, list)
            or not all(isinstance(team, str) for team in actor_teams)
            or not isinstance(target_teams, list)
            or not all(isinstance(team, str) for team in target_teams)
        ):
            raise ValidationError("invalid Inbox command authority receipt")
        try:
            return cls(
                schema_version=value["schema_version"],
                command=value["command"],
                actor_ref_digest=value["actor_ref_digest"],
                actor_roles=tuple(roles),
                actor_team_refs=tuple(actor_teams),
                actor_eligibility_revision=value["actor_eligibility_revision"],
                inbox_item_ref=value["inbox_item_ref"],
                expected_inbox_revision=value["expected_inbox_revision"],
                target_user_ref_digest=_optional_wire_text(value["target_user_ref_digest"]),
                target_team_refs=tuple(target_teams),
                target_eligibility_revision=_optional_wire_text(
                    value["target_eligibility_revision"]
                ),
                business_ref=_optional_wire_text(value["business_ref"]),
                business_team_ref=_optional_wire_text(value["business_team_ref"]),
                business_revision=value["business_revision"],
            )
        except KeyError, TypeError, ValueError:
            raise ValidationError("invalid Inbox command authority receipt") from None

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "command": self.command,
            "actor_ref_digest": self.actor_ref_digest,
            "actor_roles": list(self.actor_roles),
            "actor_team_refs": list(self.actor_team_refs),
            "actor_eligibility_revision": self.actor_eligibility_revision,
            "inbox_item_ref": self.inbox_item_ref,
            "expected_inbox_revision": self.expected_inbox_revision,
            "target_user_ref_digest": self.target_user_ref_digest,
            "target_team_refs": list(self.target_team_refs),
            "target_eligibility_revision": self.target_eligibility_revision,
            "business_ref": self.business_ref,
            "business_team_ref": self.business_team_ref,
            "business_revision": self.business_revision,
        }


def _optional_wire_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _wire_text_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 256
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _digest_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


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
        from .repositories.workflow import PostgresWorkflowRepository

        if isinstance(repository, PostgresWorkflowRepository):
            from .repositories.sla import PostgresSlaRepository

            self.repository = cast(
                WorkflowRepository,
                PostgresSlaRepository(repository.connection, repository),
            )
        else:
            self.repository = repository

    def claim(
        self,
        scope: TenantScope,
        *,
        actor: GatewayActorScope,
        authority: InboxCommandAuthority,
        inbox_item_ref: str,
        expected_revision: int,
        request_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> InboxItem:
        payload = {
            "operation": "claim",
            "actor_ref": actor.actor_ref,
            "authority_receipt_digest": canonical_digest(authority.to_wire()),
            "inbox_item_ref": inbox_item_ref,
            "expected_revision": expected_revision,
            "request_id": request_id,
        }
        return self._execute(
            scope,
            actor=actor,
            authority=authority,
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
        authority: InboxCommandAuthority,
        inbox_item_ref: str,
        assignee_user_ref: str | None,
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
            "authority_receipt_digest": canonical_digest(authority.to_wire()),
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            self._ensure_mutable(inbox)
            if assignee_user_ref is not None and inbox.team_ref not in authority.target_team_refs:
                raise ScopeViolation("assignee team mismatch")
            expected_target_digest = (
                None
                if assignee_user_ref is None
                else canonical_digest({"site_id": scope.site_id, "user_ref": assignee_user_ref})
            )
            if authority.target_user_ref_digest != expected_target_digest:
                raise AuthorizationError("assignee authority mismatch")
            return replace(inbox, state=target_state, assignee_user_ref=assignee_user_ref)

        return self._execute(
            scope,
            actor=actor,
            authority=authority,
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
        authority: InboxCommandAuthority,
        inbox_item_ref: str,
        business_ref: str,
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
            "authority_receipt_digest": canonical_digest(authority.to_wire()),
            "expected_revision": expected_revision,
            "request_id": request_id,
        }

        def change(inbox: InboxItem) -> InboxItem:
            self._ensure_mutable(inbox)
            if authority.business_team_ref != inbox.team_ref:
                raise ScopeViolation("business authority team mismatch")
            if authority.business_ref != business_ref:
                raise AuthorizationError("business authority mismatch")
            if not business_ref.startswith(_BUSINESS_PREFIXES):
                raise ValidationError("unsupported existing business reference")
            if business_ref in inbox.business_links:
                return inbox
            return replace(inbox, business_links=(*inbox.business_links, business_ref))

        return self._execute(
            scope,
            actor=actor,
            authority=authority,
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
        authority: InboxCommandAuthority | None = None,
        actor_enabled: bool | None = None,
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
        self._authorize_actor(scope, actor, authority, actor_enabled, required_roles)
        scoped_inbox = self.repository.get_inbox(scope, inbox_item_ref)
        if scoped_inbox is None:
            raise ValidationError("inbox item not found")
        if scoped_inbox.team_ref not in actor.team_refs:
            raise ScopeViolation("actor team mismatch")
        if authority is not None:
            self._validate_authority(
                authority,
                actor=actor,
                inbox=scoped_inbox,
                command=str(payload["operation"]),
                expected_revision=expected_revision,
            )
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
            authority=authority,
        )

    @staticmethod
    def _authorize_actor(
        scope: TenantScope,
        actor: GatewayActorScope,
        authority: InboxCommandAuthority | None,
        actor_enabled: bool | None,
        required_roles: frozenset[str],
    ) -> None:
        if actor.site_id != scope.site_id:
            raise ScopeViolation("actor site mismatch")
        if authority is None and actor_enabled is not True:
            raise AuthorizationError("disabled actor")
        if required_roles.isdisjoint(actor.roles):
            role_names = "/".join(sorted(required_roles))
            raise AuthorizationError(f"operation requires {role_names}")
        if authority is not None and tuple(sorted(actor.roles)) != tuple(
            sorted(authority.actor_roles)
        ):
            raise AuthorizationError("actor role authority drift")

    @staticmethod
    def _validate_authority(
        authority: InboxCommandAuthority,
        *,
        actor: GatewayActorScope,
        inbox: InboxItem,
        command: str,
        expected_revision: int,
    ) -> None:
        if (
            authority.command != command
            or authority.actor_ref_digest
            != canonical_digest({"site_id": actor.site_id, "user_ref": actor.actor_ref})
            or authority.inbox_item_ref != inbox.inbox_item_ref
            or authority.expected_inbox_revision != expected_revision
        ):
            raise AuthorizationError("Inbox command authority drift")
        if inbox.team_ref not in authority.actor_team_refs:
            raise ScopeViolation("Inbox command authority team mismatch")

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
        authority: InboxCommandAuthority | None = None,
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
        get_sla = getattr(self.repository, "get_sla", None)
        apply_sla = getattr(self.repository, "apply_inbox_sla_operation", None)
        if callable(get_sla) and callable(apply_sla):
            sla_before = get_sla(scope, before.inbox_item_ref)
            if sla_before is None:
                raise ValidationError("durable Inbox SLA state missing")
            if changed.state == "closed" and before.state != "closed":
                sla_revised = sla_before.close(now, policy_revision=sla_before.policy_revision)
            elif before.state == "closed" and changed.state != "closed":
                sla_revised = sla_before.reopen(now, policy_revision=sla_before.policy_revision)
            else:
                sla_revised = sla_before.preserve_for_revision(revised.revision, now=now)
            return cast(
                InboxItem,
                apply_sla(
                    scope,
                    before=before,
                    revised=revised,
                    sla_before=sla_before,
                    sla_revised=sla_revised,
                    audit_event=audit_event,
                    idempotency_key=idempotency_key,
                    payload_digest=payload_digest,
                    authority_receipt=None if authority is None else authority.to_wire(),
                ),
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
