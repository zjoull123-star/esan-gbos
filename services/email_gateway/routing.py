from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .models import (
    IdentityProjection,
    InboxItem,
    Mailbox,
    RouteDecision,
    RoutingRule,
    TenantScope,
    ValidationError,
    stable_ref,
)
from .protocols import FrappeRouteAuthority

AUTHORIZED_INBOX_LIST_SQL = """
    SELECT inbox_item_ref, revision
      FROM email_gateway.inbox_items
     WHERE site_id = %s
       AND team_ref = ANY(%s)
       AND (assignee_user_ref IS NULL OR assignee_user_ref = %s)
     ORDER BY received_at DESC, inbox_item_ref
     LIMIT %s
"""


class RoutingService:
    def __init__(self, authority: FrappeRouteAuthority) -> None:
        self.authority = authority

    def route(
        self,
        *,
        scope: TenantScope,
        inbox: InboxItem,
        mailbox: Mailbox,
        projection: IdentityProjection | None,
        rules: tuple[RoutingRule, ...],
    ) -> RouteDecision:
        now = datetime.now(UTC)
        if (
            inbox.site_id != scope.site_id
            or mailbox.site_id != scope.site_id
            or inbox.mailbox_ref != mailbox.mailbox_ref
            or inbox.team_ref != mailbox.default_team_ref
        ):
            return self._unassigned(scope, inbox, mailbox, "scope_mismatch", now)
        if (
            projection is None
            or projection.status != "confirmed"
            or projection.site_id != scope.site_id
            or projection.processing_purpose != scope.processing_purpose
            or projection.team_ref != mailbox.default_team_ref
            or projection.identity_type != "Party"
        ):
            return self._unassigned(scope, inbox, mailbox, "identity_unavailable", now)
        authority = self.authority.resolve(
            scope=scope,
            team_ref=mailbox.default_team_ref,
            projection=projection,
            expected_party_revision=None,
        )
        if authority.route_status == "assigned" and authority.team_ref == mailbox.default_team_ref:
            return RouteDecision(
                decision_ref=stable_ref("RTE", scope.site_id, inbox.inbox_item_ref, "authority"),
                site_id=scope.site_id,
                inbox_item_ref=inbox.inbox_item_ref,
                mailbox_ref=mailbox.mailbox_ref,
                route_status="assigned",
                team_ref=mailbox.default_team_ref,
                party_ref=authority.party_ref,
                party_revision=authority.party_revision,
                owner_user_ref=authority.owner_user_ref,
                owner_eligibility_revision=authority.owner_eligibility_revision,
                safe_reason_code=None,
                decided_at=authority.resolved_at,
            )
        for rule in sorted(rules, key=lambda item: (item.priority, item.rule_ref)):
            if (
                rule.enabled
                and rule.site_id == scope.site_id
                and rule.mailbox_ref == mailbox.mailbox_ref
                and rule.team_ref == mailbox.default_team_ref
            ):
                return RouteDecision(
                    decision_ref=stable_ref(
                        "RTE", scope.site_id, inbox.inbox_item_ref, rule.rule_ref
                    ),
                    site_id=scope.site_id,
                    inbox_item_ref=inbox.inbox_item_ref,
                    mailbox_ref=mailbox.mailbox_ref,
                    route_status="assigned",
                    team_ref=mailbox.default_team_ref,
                    party_ref=None,
                    party_revision=None,
                    owner_user_ref=rule.owner_user_ref,
                    owner_eligibility_revision=None,
                    safe_reason_code=None,
                    decided_at=authority.resolved_at,
                )
        return self._unassigned(
            scope,
            inbox,
            mailbox,
            authority.safe_reason_code or "owner_unavailable",
            authority.resolved_at,
        )

    @staticmethod
    def apply_decision(
        *,
        inbox: InboxItem,
        decision: RouteDecision,
        assignee_team_ref: str | None,
        assignee_enabled: bool,
        now: datetime,
    ) -> InboxItem:
        if inbox.state != "identity_pending":
            raise ValidationError("routing applies only to identity_pending")
        if decision.site_id != inbox.site_id or decision.inbox_item_ref != inbox.inbox_item_ref:
            raise ValidationError("routing decision inbox mismatch")
        if decision.team_ref != inbox.team_ref:
            raise ValidationError("routing decision team mismatch")
        if now < inbox.updated_at:
            raise ValidationError("routing clock regression")
        if decision.route_status == "assigned":
            if assignee_team_ref != inbox.team_ref:
                raise ValidationError("routing assignee team mismatch")
            if not assignee_enabled:
                raise ValidationError("routing assignee is not eligible")
            return replace(
                inbox,
                assignee_user_ref=decision.owner_user_ref,
                state="assigned",
                revision=inbox.revision + 1,
                updated_at=now,
            )
        if decision.route_status != "unassigned" or decision.owner_user_ref is not None:
            raise ValidationError("invalid routing decision")
        return replace(
            inbox,
            assignee_user_ref=None,
            state="unassigned",
            revision=inbox.revision + 1,
            updated_at=now,
        )

    @staticmethod
    def _unassigned(
        scope: TenantScope,
        inbox: InboxItem,
        mailbox: Mailbox,
        reason: str,
        now: datetime,
    ) -> RouteDecision:
        return RouteDecision(
            decision_ref=stable_ref("RTE", scope.site_id, inbox.inbox_item_ref, reason),
            site_id=scope.site_id,
            inbox_item_ref=inbox.inbox_item_ref,
            mailbox_ref=mailbox.mailbox_ref,
            route_status="unassigned",
            team_ref=mailbox.default_team_ref,
            party_ref=None,
            party_revision=None,
            owner_user_ref=None,
            owner_eligibility_revision=None,
            safe_reason_code=reason,
            decided_at=now,
        )
