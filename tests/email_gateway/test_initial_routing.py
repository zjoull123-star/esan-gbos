from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.email_gateway.models import (
    AuthorityRoute,
    IdentityProjection,
    InboxItem,
    TenantScope,
)

SITE = "alpha.example"
PURPOSE = "sales_follow_up"
OPAQUE = "extid:v1:email:" + "A" * 43
TEAM = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAPPING = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
NOW = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _projection(*, revision: int = 3, status: str = "confirmed") -> IdentityProjection:
    return IdentityProjection(
        site_id=SITE,
        processing_purpose=PURPOSE,
        opaque_address_ref=OPAQUE,
        external_identity_ref=MAPPING,
        external_identity_revision=revision,
        identity_type="Party",
        team_ref=TEAM,
        status=status,
        projection_receipt_ref=f"IPR-{revision}",
        observed_at=NOW,
        payload_digest=DIGEST,
    )


class _Transport:
    def __init__(self, response: tuple[int, dict[str, Any]] | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, **kwargs: object) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _assigned_wire() -> dict[str, object]:
    return {
        "route_status": "assigned",
        "party_ref": "PTY-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "party_revision": 7,
        "team_ref": TEAM,
        "team_revision": 4,
        "owner_user_ref": "owner@example.invalid",
        "owner_eligibility_revision": "sha256:" + "b" * 64,
        "resolved_at": NOW.isoformat(),
    }


def test_frappe_initial_route_client_posts_exact_closed_request_and_unwraps_message() -> None:
    from services.email_gateway.initial_routing import (
        FRAPPE_INITIAL_ROUTE_URL,
        FrappeInitialRouteClient,
    )

    transport = _Transport((200, {"message": {"route_authority": _assigned_wire()}}))
    client = FrappeInitialRouteClient(
        transport=transport,
        api_key="k" * 16,
        api_secret="s" * 16,
        auth_ref="email-gateway-authority-v1",
    )

    result = client.resolve(projection=_projection(), request_id="IRQ-REQUEST")

    assert result == AuthorityRoute.from_wire(_assigned_wire())
    call = transport.calls[0]
    assert call["url"] == FRAPPE_INITIAL_ROUTE_URL
    assert call["payload"] == {
        "payload": {
            "site_id": SITE,
            "processing_purpose": "email_gateway_authority",
            "request_id": "IRQ-REQUEST",
            "auth_ref": "email-gateway-authority-v1",
            "mapping_ref": MAPPING,
            "expected_mapping_revision": 3,
            "expected_team_ref": TEAM,
        }
    }
    assert call["headers"] == {
        "Accept": "application/json",
        "Authorization": "token " + "k" * 16 + ":" + "s" * 16,
        "Content-Type": "application/json",
        "Host": SITE,
        "X-GBOS-Frappe-Auth-Ref": "email-gateway-authority-v1",
        "X-Processing-Purpose": "email_gateway_authority",
        "X-Request-ID": "IRQ-REQUEST",
        "X-Site-ID": SITE,
    }
    assert OPAQUE not in repr(call)


@pytest.mark.parametrize("status", [403, 404, 409, 422])
def test_frappe_initial_route_client_permanently_rejects_non_retryable_status(status: int) -> None:
    from services.email_gateway.initial_routing import (
        FrappeInitialRouteClient,
        PermanentInitialRouteError,
    )

    client = FrappeInitialRouteClient(
        transport=_Transport((status, {"message": {"private": "must-not-leak"}})),
        api_key="k" * 16,
        api_secret="s" * 16,
        auth_ref="email-gateway-authority-v1",
    )
    with pytest.raises(PermanentInitialRouteError) as caught:
        client.resolve(projection=_projection(), request_id="IRQ-REQUEST")
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_frappe_initial_route_client_only_retries_rate_limit_and_server_errors(
    status: int,
) -> None:
    from services.email_gateway.initial_routing import (
        FrappeInitialRouteClient,
        RetryableInitialRouteError,
    )

    client = FrappeInitialRouteClient(
        transport=_Transport((status, {})),
        api_key="k" * 16,
        api_secret="s" * 16,
        auth_ref="email-gateway-authority-v1",
    )
    with pytest.raises(RetryableInitialRouteError):
        client.resolve(projection=_projection(), request_id="IRQ-REQUEST")


def test_projection_persistence_enqueues_once_and_newer_generation_supersedes_old() -> None:
    from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository

    scope = TenantScope(SITE, PURPOSE)
    repository = InMemoryIdentityProjectionRepository()
    assert repository.apply(scope, _projection()) == _projection()
    assert repository.apply(scope, _projection()) == _projection()
    first = repository.route_work_repository.list(scope)
    assert len(first) == 1
    assert first[0].status == "queued"
    assert first[0].mapping_revision == 3

    newer = replace(
        _projection(),
        external_identity_revision=4,
        status="revoked",
        projection_receipt_ref="IPR-4",
        payload_digest="sha256:" + "c" * 64,
    )
    repository.apply(scope, newer)
    work = repository.route_work_repository.list(scope)
    assert [(item.mapping_revision, item.status) for item in work] == [
        (3, "superseded"),
        (4, "queued"),
    ]


def test_processor_applies_only_server_route_and_never_falls_back_to_rules() -> None:
    from services.email_gateway.initial_routing import InitialRouteProcessor, InitialRouteStatus
    from services.email_gateway.repositories.identity_route_work import IdentityRouteWorkClaim
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    scope = TenantScope(SITE, PURPOSE)
    inbox = InboxItem.new(
        site_id=SITE,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref=TEAM,
        received_at=NOW,
    )
    workflow = InMemoryWorkflowRepository()
    workflow.save_inbox(scope, inbox)
    claim = IdentityRouteWorkClaim(
        work_ref="IRW-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SITE,
        processing_purpose=PURPOSE,
        opaque_address_ref=OPAQUE,
        mapping_ref=MAPPING,
        mapping_revision=3,
        expected_team_ref=TEAM,
        projection_receipt_ref="IPR-3",
        projection_payload_digest=DIGEST,
        worker_id="initial-route-1",
        attempt=1,
        generation=1,
        fence_token="v1:" + "f" * 64,
        lease_expires_at=NOW + timedelta(seconds=30),
    )

    class Work:
        terminal: str | None = None

        def claim(self, *_args: object, **_kwargs: object) -> object:
            return claim

        def projection_state(self, *_args: object, **_kwargs: object) -> str:
            return "current_routeable"

        def list_candidate_refs(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
            return (inbox.inbox_item_ref,)

        def load_candidate(self, *_args: object, **_kwargs: object) -> object:
            return type(
                "Candidate",
                (),
                {
                    "inbox": workflow.get_inbox(scope, inbox.inbox_item_ref),
                    "projection": _projection(),
                },
            )()

        def workflow_for(self, *_args: object, **_kwargs: object) -> object:
            return workflow

        def complete(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "completed"

        def retry(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "retry"

        def reject(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "dead_letter"

        def supersede(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "superseded"

        def continue_work(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "continued"

    class Authority:
        calls: list[str] = []

        def resolve(self, *, projection: IdentityProjection, request_id: str) -> AuthorityRoute:
            self.calls.append(request_id)
            return AuthorityRoute.unassigned("owner_unavailable", NOW)

    work = Work()
    authority = Authority()
    result = InitialRouteProcessor(
        repository=work,  # type: ignore[arg-type]
        authority=authority,  # type: ignore[arg-type]
        worker_id="initial-route-1",
        clock=lambda: NOW,
    ).run_once(scope)

    routed = workflow.get_inbox(scope, inbox.inbox_item_ref)
    assert result.status is InitialRouteStatus.COMPLETED
    assert routed is not None and routed.state == "unassigned"
    assert routed.assignee_user_ref is None
    assert work.terminal == "completed"
    assert authority.calls == [authority.calls[0]]


def test_processor_requeues_a_full_batch_and_does_not_repeat_applied_inboxes() -> None:
    from services.email_gateway.initial_routing import InitialRouteProcessor, InitialRouteStatus
    from services.email_gateway.repositories.identity_route_work import (
        IdentityRouteCandidate,
        IdentityRouteWorkClaim,
    )
    from services.email_gateway.repositories.workflow import InMemoryWorkflowRepository

    scope = TenantScope(SITE, PURPOSE)
    workflow = InMemoryWorkflowRepository()
    inboxes = tuple(
        InboxItem.new(
            site_id=SITE,
            mailbox_ref="MBX-01",
            message_ref=f"MSG-{index}",
            team_ref=TEAM,
            received_at=NOW + timedelta(seconds=index),
        )
        for index in (1, 2)
    )
    for inbox in inboxes:
        workflow.save_inbox(scope, inbox)
    claim = IdentityRouteWorkClaim(
        work_ref="IRW-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SITE,
        processing_purpose=PURPOSE,
        opaque_address_ref=OPAQUE,
        mapping_ref=MAPPING,
        mapping_revision=3,
        expected_team_ref=TEAM,
        projection_receipt_ref="IPR-3",
        projection_payload_digest=DIGEST,
        worker_id="initial-route-batch",
        attempt=1,
        generation=1,
        fence_token="v1:" + "f" * 64,
        lease_expires_at=NOW + timedelta(minutes=1),
    )

    class Work:
        terminal: str | None = None

        def claim(self, *_args: object, **_kwargs: object) -> object:
            return claim

        def projection_state(self, *_args: object, **_kwargs: object) -> str:
            return "current_routeable"

        def list_candidate_refs(
            self, *_args: object, limit: int, **_kwargs: object
        ) -> tuple[str, ...]:
            pending = tuple(
                inbox.inbox_item_ref
                for inbox in inboxes
                if workflow.get_inbox(scope, inbox.inbox_item_ref).state == "identity_pending"
            )
            return pending[:limit]

        def load_candidate(
            self, _scope: object, _claim: object, inbox_ref: str, **_kwargs: object
        ) -> IdentityRouteCandidate | None:
            inbox = workflow.get_inbox(scope, inbox_ref)
            if inbox is None or inbox.state != "identity_pending":
                return None
            return IdentityRouteCandidate(inbox, _projection())

        def workflow_for(self, *_args: object, **_kwargs: object) -> object:
            return workflow

        def complete(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "completed"

        def continue_work(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "continued"

        def retry(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "retry"

        def reject(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "dead_letter"

        def supersede(self, *_args: object, **_kwargs: object) -> None:
            self.terminal = "superseded"

    class Authority:
        calls: list[str] = []

        def resolve(self, *, projection: IdentityProjection, request_id: str) -> AuthorityRoute:
            self.calls.append(request_id)
            return AuthorityRoute.unassigned("owner_unavailable", NOW)

    work = Work()
    authority = Authority()
    processor = InitialRouteProcessor(
        repository=work,  # type: ignore[arg-type]
        authority=authority,
        worker_id="initial-route-batch",
        clock=lambda: NOW + timedelta(seconds=10),
        batch_limit=1,
    )

    first = processor.run_once(scope)
    assert first.status is InitialRouteStatus.CONTINUED
    assert work.terminal == "continued"
    assert [workflow.get_inbox(scope, inbox.inbox_item_ref).state for inbox in inboxes] == [
        "unassigned",
        "identity_pending",
    ]

    second = processor.run_once(scope)
    assert second.status is InitialRouteStatus.COMPLETED
    assert work.terminal == "completed"
    assert [workflow.get_inbox(scope, inbox.inbox_item_ref).state for inbox in inboxes] == [
        "unassigned",
        "unassigned",
    ]
    assert len(authority.calls) == 2
    assert len(set(authority.calls)) == 2


def test_candidate_sql_applies_purpose_authorization_before_limit() -> None:
    from services.email_gateway.repositories.identity_route_work import CANDIDATE_REFS_SQL

    normalized = " ".join(CANDIDATE_REFS_SQL.lower().split())
    scoped = normalized[normalized.index(" where ") : normalized.index(" limit ")]
    for predicate in (
        "processing_purpose",
        "business_purpose",
        "default_team_ref",
        "inbox.team_ref",
        "participant.role",
        "participant.identity_ref",
        "identity_pending",
    ):
        assert predicate in scoped
