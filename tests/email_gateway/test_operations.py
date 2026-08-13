from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from .conftest import NOW, SITE


def _actor(*, role: str = "Sales User", team: str = "TEM-01", ref: str = "sales-01"):
    from services.email_gateway.models import GatewayActorScope

    return GatewayActorScope(
        site_id=SITE,
        actor_ref=ref,
        team_refs=(team,),
        roles=(role,),
    )


def _item(*, state: str = "unassigned", team: str = "TEM-01", assignee: str | None = None):
    from services.email_gateway.models import InboxItem

    return replace(
        InboxItem.new(
            site_id=SITE,
            mailbox_ref="MBX-01",
            message_ref="MSG-01",
            team_ref=team,
            received_at=NOW,
            state=state,
        ),
        assignee_user_ref=assignee,
    )


def _service(scope, item):
    from services.email_gateway.operations import InboxOperations
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    repository = InMemoryWorkflowRepository()
    repository.save_inbox(scope, item)
    return InboxOperations(repository), repository


def _authority(
    command: str,
    *,
    actor=None,
    inbox_ref: str | None = None,
    expected_revision: int = 1,
    target_user_ref: str | None = None,
    business_ref: str | None = None,
):
    from services.email_gateway.models import canonical_digest
    from services.email_gateway.operations import InboxCommandAuthority

    actor = actor or _actor()
    return InboxCommandAuthority(
        schema_version="1.0",
        command=command,
        actor_ref_digest=canonical_digest({"site_id": actor.site_id, "user_ref": actor.actor_ref}),
        actor_roles=actor.roles,
        actor_team_refs=actor.team_refs,
        actor_eligibility_revision="sha256:" + "a" * 64,
        inbox_item_ref=inbox_ref or _item().inbox_item_ref,
        expected_inbox_revision=expected_revision,
        target_user_ref_digest=(
            None
            if target_user_ref is None
            else canonical_digest({"site_id": actor.site_id, "user_ref": target_user_ref})
        ),
        target_team_refs=actor.team_refs if target_user_ref else (),
        target_eligibility_revision=("sha256:" + "b" * 64) if target_user_ref else None,
        business_ref=business_ref,
        business_team_ref=actor.team_refs[0] if business_ref else None,
        business_revision=3 if business_ref else None,
    )


def test_authority_receipt_is_closed_revisioned_and_contains_no_caller_booleans() -> None:
    from dataclasses import fields

    from services.email_gateway.operations import InboxCommandAuthority

    names = {field.name for field in fields(InboxCommandAuthority)}
    assert names == {
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
    assert not names & {"actor_enabled", "assignee_enabled", "authority_valid"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_ref_digest", None),
        ("actor_eligibility_revision", "sha256:" + "z" * 64),
        ("expected_inbox_revision", True),
        ("business_revision", True),
        ("business_revision", {"revision": 1}),
    ],
)
def test_authority_wire_receipt_rejects_implicit_scalar_coercion(field, value) -> None:
    from services.email_gateway.models import ValidationError
    from services.email_gateway.operations import InboxCommandAuthority

    receipt = _authority("claim").to_wire()
    receipt[field] = value
    with pytest.raises(ValidationError, match="authority receipt"):
        InboxCommandAuthority.from_wire(receipt)


class _ResponseLossRepository:
    def __init__(self, repository) -> None:
        self.repository = repository
        self.lose_once = True

    def __getattr__(self, name: str):
        return getattr(self.repository, name)

    def apply_inbox_operation(self, *args, **kwargs):
        result = self.repository.apply_inbox_operation(*args, **kwargs)
        if self.lose_once:
            self.lose_once = False
            raise ConnectionError("response lost after commit")
        return result


def test_sales_user_claim_is_same_team_revision_pinned_idempotent_and_audited(scope) -> None:
    service, repository = _service(scope, _item())
    command = dict(
        actor=_actor(),
        authority=_authority("claim"),
        inbox_item_ref=_item().inbox_item_ref,
        expected_revision=1,
        request_id="REQ-CLAIM-01",
        idempotency_key="claim-01",
        now=NOW + timedelta(seconds=1),
    )

    claimed = service.claim(scope, **command)
    replay = service.claim(scope, **command)

    assert claimed == replay
    assert claimed.state == "assigned"
    assert claimed.assignee_user_ref == "sales-01"
    assert claimed.revision == 2
    assert repository.audit_count(scope) == 1


def test_inbox_operation_injected_failure_rolls_back_state_audit_and_receipt(scope) -> None:
    from services.email_gateway.operations import InboxOperations
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    def fail_after_inbox(phase: str) -> None:
        if phase == "after_inbox_write":
            raise RuntimeError("injected transaction failure")

    item = _item()
    repository = InMemoryWorkflowRepository(transaction_failure_injector=fail_after_inbox)
    repository.save_inbox(scope, item)
    service = InboxOperations(repository)
    with pytest.raises(RuntimeError, match="injected"):
        service.claim(
            scope,
            actor=_actor(),
            authority=_authority("claim"),
            inbox_item_ref=item.inbox_item_ref,
            expected_revision=1,
            request_id="REQ-CLAIM-ROLLBACK",
            idempotency_key="claim-rollback",
            now=NOW + timedelta(seconds=1),
        )

    assert repository.get_inbox(scope, item.inbox_item_ref) == item
    assert repository.audit_count(scope) == 0
    assert repository.replay(scope, "claim-rollback", "sha256:" + "0" * 64) is None


def test_inbox_operation_response_loss_replays_one_stable_result(scope) -> None:
    from services.email_gateway.operations import InboxOperations
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    item = _item()
    durable = InMemoryWorkflowRepository()
    durable.save_inbox(scope, item)
    repository = _ResponseLossRepository(durable)
    service = InboxOperations(repository)
    command = dict(
        actor=_actor(),
        authority=_authority("claim"),
        inbox_item_ref=item.inbox_item_ref,
        expected_revision=1,
        request_id="REQ-CLAIM-LOSS",
        idempotency_key="claim-loss",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ConnectionError, match="response lost"):
        service.claim(scope, **command)

    replay = service.claim(scope, **command)
    assert replay == durable.get_inbox(scope, item.inbox_item_ref)
    assert replay.revision == 2
    assert durable.audit_count(scope) == 1


@pytest.mark.parametrize(
    ("actor", "error"),
    [
        (_actor(team="TEM-OTHER"), "team"),
        (_actor(role="AI Assistant"), "Sales User"),
    ],
)
def test_claim_rejects_cross_team_or_non_sales_actor(scope, actor, error: str) -> None:
    from services.email_gateway.models import AuthorizationError, ScopeViolation

    item = _item()
    service, _ = _service(scope, item)
    with pytest.raises((AuthorizationError, ScopeViolation), match=error):
        service.claim(
            scope,
            actor=actor,
            authority=_authority("claim", actor=actor),
            inbox_item_ref=item.inbox_item_ref,
            expected_revision=1,
            request_id="REQ-CLAIM-02",
            idempotency_key="claim-02",
            now=NOW + timedelta(seconds=1),
        )


def test_stale_revision_and_idempotency_payload_drift_fail_closed(scope) -> None:
    from services.email_gateway.models import IdempotencyConflict, RevisionConflict

    item = _item()
    service, _ = _service(scope, item)
    base = dict(
        actor=_actor(),
        inbox_item_ref=item.inbox_item_ref,
        request_id="REQ-CLAIM-03",
        idempotency_key="claim-03",
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(RevisionConflict):
        service.claim(
            scope,
            authority=_authority("claim", expected_revision=9),
            expected_revision=9,
            **base,
        )
    service.claim(scope, authority=_authority("claim"), expected_revision=1, **base)
    with pytest.raises(IdempotencyConflict):
        service.claim(
            scope,
            authority=_authority("claim", expected_revision=2),
            expected_revision=2,
            **base,
        )


def test_idempotent_replay_rejects_authority_revision_drift(scope) -> None:
    from services.email_gateway.models import IdempotencyConflict

    item = _item()
    service, _ = _service(scope, item)
    authority = _authority("claim")
    command = dict(
        actor=_actor(),
        inbox_item_ref=item.inbox_item_ref,
        expected_revision=1,
        request_id="REQ-CLAIM-AUTHORITY-DRIFT",
        idempotency_key="claim-authority-drift",
        now=NOW + timedelta(seconds=1),
    )
    service.claim(scope, authority=authority, **command)
    with pytest.raises(IdempotencyConflict, match="drift"):
        service.claim(
            scope,
            authority=replace(
                authority,
                actor_eligibility_revision="sha256:" + "c" * 64,
            ),
            **command,
        )


def test_idempotent_replay_rechecks_current_team_authorization(scope) -> None:
    from services.email_gateway.models import ScopeViolation

    item = _item()
    service, _ = _service(scope, item)
    command = dict(
        inbox_item_ref=item.inbox_item_ref,
        expected_revision=1,
        request_id="REQ-CLAIM-REPLAY",
        idempotency_key="claim-replay",
        now=NOW + timedelta(seconds=1),
    )
    service.claim(scope, actor=_actor(), authority=_authority("claim"), **command)
    with pytest.raises(ScopeViolation, match="team"):
        other = _actor(team="TEM-OTHER")
        service.claim(scope, actor=other, authority=_authority("claim", actor=other), **command)


def test_manager_reassign_and_reopen_are_closed_role_transitions(scope) -> None:
    manager = _actor(role="Sales Manager", ref="manager-01")
    assigned = _item(state="assigned", assignee="sales-01")
    service, _ = _service(scope, assigned)
    reassigned = service.reassign(
        scope,
        actor=manager,
        authority=_authority("reassign", actor=manager, target_user_ref="sales-02"),
        inbox_item_ref=assigned.inbox_item_ref,
        assignee_user_ref="sales-02",
        expected_revision=1,
        request_id="REQ-REASSIGN-01",
        idempotency_key="reassign-01",
        now=NOW + timedelta(seconds=1),
    )
    assert (reassigned.state, reassigned.assignee_user_ref) == ("assigned", "sales-02")

    unassigned = service.reassign(
        scope,
        actor=manager,
        authority=_authority(
            "reassign",
            actor=manager,
            inbox_ref=assigned.inbox_item_ref,
            expected_revision=2,
        ),
        inbox_item_ref=assigned.inbox_item_ref,
        assignee_user_ref=None,
        expected_revision=2,
        request_id="REQ-UNASSIGN-01",
        idempotency_key="unassign-01",
        now=NOW + timedelta(seconds=2),
    )
    assert (unassigned.state, unassigned.assignee_user_ref) == ("unassigned", None)

    closed = replace(unassigned, state="closed", revision=4)
    service.repository.save_inbox(scope, closed)
    reopened = service.reopen(
        scope,
        actor=manager,
        actor_enabled=True,
        inbox_item_ref=closed.inbox_item_ref,
        target_state="unassigned",
        assignee_user_ref=None,
        expected_revision=4,
        request_id="REQ-REOPEN-01",
        idempotency_key="reopen-01",
        now=NOW + timedelta(seconds=3),
    )
    assert (reopened.state, reopened.assignee_user_ref) == ("unassigned", None)


def test_sales_user_can_only_edit_owned_draft_or_request_human_terminal_states(scope) -> None:
    item = _item(state="assigned", assignee="sales-01")
    service, _ = _service(scope, item)
    drafted = service.transition(
        scope,
        actor=_actor(),
        actor_enabled=True,
        inbox_item_ref=item.inbox_item_ref,
        target_state="draft",
        expected_revision=1,
        request_id="REQ-DRAFT-01",
        idempotency_key="draft-state-01",
        now=NOW + timedelta(seconds=1),
    )
    waiting = service.transition(
        scope,
        actor=_actor(),
        actor_enabled=True,
        inbox_item_ref=item.inbox_item_ref,
        target_state="waiting_internal",
        expected_revision=drafted.revision,
        request_id="REQ-WAIT-01",
        idempotency_key="wait-01",
        now=NOW + timedelta(seconds=2),
    )
    assert waiting.state == "waiting_internal"


@pytest.mark.parametrize(
    "source,target",
    [
        ("unassigned", "draft"),
        ("assigned", "send_queued"),
        ("draft", "send_uncertain"),
        ("assigned", "waiting_customer"),
        ("quarantined", "closed"),
        ("closed", "assigned"),
    ],
)
def test_sales_user_cannot_enter_impossible_reserved_quarantine_or_reopen_states(
    scope, source: str, target: str
) -> None:
    from services.email_gateway.models import AuthorizationError

    assignee = "sales-01" if source in {"assigned", "draft"} else None
    item = _item(state=source, assignee=assignee)
    service, _ = _service(scope, item)
    with pytest.raises(AuthorizationError):
        service.transition(
            scope,
            actor=_actor(),
            actor_enabled=True,
            inbox_item_ref=item.inbox_item_ref,
            target_state=target,
            expected_revision=1,
            request_id="REQ-BLOCK-01",
            idempotency_key=f"block-{source}-{target}",
            now=NOW + timedelta(seconds=1),
        )


def test_identity_and_routing_workers_have_only_the_frozen_publication_transition(scope) -> None:
    from services.email_gateway.models import AuthorizationError

    item = _item(state="identity_pending")
    service, _ = _service(scope, item)
    routed = service.apply_identity_route(
        scope,
        worker_kind="routing_worker",
        inbox_item_ref=item.inbox_item_ref,
        target_state="assigned",
        assignee_user_ref="sales-01",
        assignee_team_ref="TEM-01",
        assignee_enabled=True,
        expected_revision=1,
        request_id="REQ-ROUTE-01",
        idempotency_key="route-01",
        now=NOW + timedelta(seconds=1),
    )
    assert routed.state == "assigned"
    with pytest.raises(AuthorizationError):
        service.apply_identity_route(
            scope,
            worker_kind="ai_worker",
            inbox_item_ref=item.inbox_item_ref,
            target_state="unassigned",
            assignee_user_ref=None,
            assignee_team_ref=None,
            assignee_enabled=False,
            expected_revision=2,
            request_id="REQ-ROUTE-02",
            idempotency_key="route-02",
            now=NOW + timedelta(seconds=2),
        )


def test_business_link_requires_closed_frappe_authority_and_never_creates_crm(scope) -> None:
    from services.email_gateway.models import AuthorizationError

    item = _item(state="assigned", assignee="sales-01")
    service, _ = _service(scope, item)
    linked = service.link_business(
        scope,
        actor=_actor(),
        authority=_authority("link_business", business_ref="CRM-DEAL-01"),
        inbox_item_ref=item.inbox_item_ref,
        business_ref="CRM-DEAL-01",
        expected_revision=1,
        request_id="REQ-LINK-01",
        idempotency_key="link-01",
        now=NOW + timedelta(seconds=1),
    )
    assert linked.business_links == ("CRM-DEAL-01",)
    with pytest.raises(AuthorizationError):
        service.link_business(
            scope,
            actor=_actor(),
            authority=_authority("link_business", business_ref="CRM-DEAL-01", expected_revision=2),
            inbox_item_ref=item.inbox_item_ref,
            business_ref="CRM-DEAL-02",
            expected_revision=2,
            request_id="REQ-LINK-02",
            idempotency_key="link-02",
            now=NOW + timedelta(seconds=2),
        )
