from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from .conftest import DIGEST_A, NOW, OPAQUE_FROM, SITE


class FakeAuthority:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    def resolve(self, *, scope, team_ref, projection, expected_party_revision=None):
        self.calls += 1
        return self.response


def _projection(*, status: str = "confirmed", team_ref: str = "TEM-01"):
    from services.email_gateway.models import IdentityProjection

    return IdentityProjection(
        site_id=SITE,
        processing_purpose="sales_follow_up",
        opaque_address_ref=OPAQUE_FROM,
        external_identity_ref="EXT-01",
        external_identity_revision=1,
        identity_type="Party",
        team_ref=team_ref,
        status=status,
        projection_receipt_ref="IPR-01",
        observed_at=NOW,
        payload_digest=DIGEST_A,
    )


def test_route_uses_mailbox_team_then_live_authority(scope, mailbox) -> None:
    from services.email_gateway.models import AuthorityRoute, InboxItem
    from services.email_gateway.routing import RoutingService

    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref=mailbox.mailbox_ref,
        message_ref="MSG-01",
        team_ref=mailbox.default_team_ref,
        received_at=NOW,
    )
    authority = FakeAuthority(
        AuthorityRoute.assigned(
            party_ref="PTY-01",
            party_revision=2,
            team_ref="TEM-01",
            team_revision=3,
            owner_user_ref="owner@example.invalid",
            owner_eligibility_revision=DIGEST_A,
            resolved_at=NOW,
        )
    )
    decision = RoutingService(authority).route(
        scope=scope,
        inbox=inbox,
        mailbox=mailbox,
        projection=_projection(),
        rules=(),
    )
    assert decision.route_status == "assigned"
    assert decision.owner_user_ref == "owner@example.invalid"
    assert decision.team_ref == mailbox.default_team_ref
    assert authority.calls == 1


def test_stale_revoked_or_cross_team_authority_fails_closed(scope, mailbox) -> None:
    from services.email_gateway.models import AuthorityRoute, InboxItem
    from services.email_gateway.routing import RoutingService

    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref=mailbox.mailbox_ref,
        message_ref="MSG-01",
        team_ref=mailbox.default_team_ref,
        received_at=NOW,
    )
    authority = FakeAuthority(
        AuthorityRoute.assigned(
            party_ref="PTY-01",
            party_revision=2,
            team_ref="TEM-OTHER",
            team_revision=3,
            owner_user_ref="owner@example.invalid",
            owner_eligibility_revision=DIGEST_A,
            resolved_at=NOW,
        )
    )
    service = RoutingService(authority)
    for projection in (
        _projection(status="revoked"),
        _projection(team_ref="TEM-OTHER"),
        _projection(),
    ):
        decision = service.route(
            scope=scope,
            inbox=inbox,
            mailbox=mailbox,
            projection=projection,
            rules=(),
        )
        assert decision.route_status == "unassigned"
        assert decision.owner_user_ref is None


def test_same_team_explicit_rule_is_only_fallback(scope, mailbox) -> None:
    from services.email_gateway.models import AuthorityRoute, InboxItem, RoutingRule
    from services.email_gateway.routing import RoutingService

    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref=mailbox.mailbox_ref,
        message_ref="MSG-01",
        team_ref=mailbox.default_team_ref,
        received_at=NOW,
    )
    service = RoutingService(FakeAuthority(AuthorityRoute.unassigned("owner_unavailable", NOW)))
    decision = service.route(
        scope=scope,
        inbox=inbox,
        mailbox=mailbox,
        projection=_projection(),
        rules=(
            RoutingRule(
                rule_ref="RULE-01",
                site_id=SITE,
                team_ref="TEM-01",
                mailbox_ref="MBX-01",
                owner_user_ref="fallback@example.invalid",
                priority=1,
                revision=1,
                enabled=True,
            ),
        ),
    )
    assert decision.owner_user_ref == "fallback@example.invalid"
    cross_team = replace(decision, team_ref="TEM-OTHER")
    assert cross_team.team_ref != mailbox.default_team_ref


def test_authority_wire_is_exact_closed_contract() -> None:
    from services.email_gateway.models import AuthorityRoute, ValidationError

    examples = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts/email_gateway/examples/provider-neutral-v1.json"
        ).read_text()
    )
    cases = examples["cases"]["frappe-route-authority-v1.0.schema.json"]["valid"]
    for name in ("assigned", "unassigned"):
        result = AuthorityRoute.from_wire(cases[name])
        assert result.to_wire() == cases[name]
    with pytest.raises(ValidationError, match="unknown"):
        AuthorityRoute.from_wire({**cases["assigned"], "contact_email": "raw@example.invalid"})
