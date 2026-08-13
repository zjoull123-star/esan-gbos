from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.email_gateway.models import GatewayActorScope, ValidationError
from services.email_gateway.phase1_read import Phase1InboxItem, Phase1Mailbox
from services.email_gateway.repositories.phase1_read import (
    InMemoryPhase1ReadRepository,
    PostgresPhase1ReadRepository,
)


def _mailbox(ref: str, label: str = "Sales") -> Phase1Mailbox:
    return Phase1Mailbox(
        mailbox_ref=ref,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        display_label=label,
        provider_kind="fake",
        business_mode="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01",
        account_owner_user_ref="owner-01",
        inbound_enabled=True,
        outbound_enabled=False,
        status="active",
        config_revision=1,
    )


def _inbox(ref: str, team: str, minute: int) -> Phase1InboxItem:
    return Phase1InboxItem(
        inbox_item_ref=ref,
        mailbox_label="Sales",
        mailbox_role="primary",
        received_at=datetime(2026, 8, 13, 9, minute, tzinfo=UTC),
        state="identity_pending",
        safe_summary="New enquiry",
        team_ref=team,
        assignee_user_ref=None,
        identity_state="unknown",
        revision=1,
    )


def test_in_memory_phase1_reads_page_with_opaque_verified_cursor() -> None:
    repository = InMemoryPhase1ReadRepository(
        mailboxes=(
            _mailbox("MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            _mailbox("MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW"),
        )
    )

    first = repository.list_mailboxes("alpha.example", page_size=1, cursor=None)
    second = repository.list_mailboxes("alpha.example", page_size=1, cursor=first.next_cursor)

    assert [item.mailbox_ref for item in first.items] == ["MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert first.next_cursor and "MBX-" not in first.next_cursor
    assert [item.mailbox_ref for item in second.items] == ["MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW"]
    assert second.next_cursor is None
    with pytest.raises(ValidationError, match="cursor"):
        repository.list_mailboxes("alpha.example", page_size=1, cursor="Zm9yZ2Vk")


def test_in_memory_inbox_applies_team_scope_before_pagination() -> None:
    repository = InMemoryPhase1ReadRepository(
        inbox_items=(
            _inbox("INB-01ARZ3NDEKTSV4RRFFQ69G5FAV", "TEM-OTHER", 3),
            _inbox("INB-01ARZ3NDEKTSV4RRFFQ69G5FAW", "TEM-01", 2),
            _inbox("INB-01ARZ3NDEKTSV4RRFFQ69G5FAX", "TEM-01", 1),
        )
    )
    actor = GatewayActorScope(
        site_id="alpha.example",
        actor_ref="sales-user",
        team_refs=("TEM-01",),
        roles=("Sales User",),
    )

    page = repository.list_inbox(actor, state=None, page_size=1, cursor=None)

    assert [item.team_ref for item in page.items] == ["TEM-01"]
    assert page.next_cursor is not None
    assert repository.get_inbox(actor, "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV") is None


def test_in_memory_wildcard_is_governed_by_actor_role() -> None:
    repository = InMemoryPhase1ReadRepository(
        inbox_items=(_inbox("INB-01ARZ3NDEKTSV4RRFFQ69G5FAV", "TEM-OTHER", 3),)
    )
    ceo = GatewayActorScope(
        site_id="alpha.example",
        actor_ref="ceo-user",
        team_refs=("*",),
        roles=("CEO",),
    )

    assert len(repository.list_inbox(ceo, state=None, page_size=25, cursor=None).items) == 1


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.executions.append((query, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def fetchone(self) -> tuple[object, ...] | None:
        return None

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.db = _Cursor()

    def cursor(self) -> _Cursor:
        return self.db

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def test_postgres_inbox_sql_authorizes_team_before_limit() -> None:
    connection = _Connection()
    repository = PostgresPhase1ReadRepository(
        connection,  # type: ignore[arg-type]
        decrypt_restricted_text=lambda value: value.decode(),
    )
    actor = GatewayActorScope(
        site_id="alpha.example",
        actor_ref="sales-user",
        team_refs=("TEM-01",),
        roles=("Sales User",),
    )

    assert (
        repository.list_inbox(actor, state="identity_pending", page_size=1, cursor=None).items == ()
    )

    query, params = connection.db.executions[-1]
    assert query.index("inbox.team_ref = ANY") < query.rindex("LIMIT")
    assert query.index("inbox.state = %s") < query.rindex("LIMIT")
    assert params[-1] == 2
    assert list(actor.team_refs) in params
