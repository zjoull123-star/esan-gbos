from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from services.agent_runtime.read_service import PostgresAgentReadService


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self._connection.executed.append((sql, params))
        if "set_config" not in sql:
            self._rows = self._connection.results.pop(0)

    def fetchone(self) -> tuple[Any, ...] | None:
        return None if not self._rows else self._rows.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = self._rows
        self._rows = []
        return rows


class _Connection:
    def __init__(self, *results: list[tuple[Any, ...]]) -> None:
        self.results = list(results)
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def transaction(self):
        return nullcontext()

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def test_usage_preserves_partial_tokens_and_unknown_pricing_for_exact_model() -> None:
    connection = _Connection(
        [
            ("known", 125, "known", Decimal("49.50"), "USD", "catalog-v1"),
            ("unknown", None, "unknown", None, None, None),
        ]
    )

    usage = PostgresAgentReadService(connection).get_usage("site-a", "2026-08")

    assert usage.model == "deepseek-v4-flash"
    assert usage.period == "2026-08"
    assert usage.tokens == 125
    assert usage.token_state == "partial"
    assert usage.cost.amount == Decimal("49.50")
    assert usage.cost.state == "partial"
    assert usage.state == "unknown"
    select_sql, params = connection.executed[-1]
    assert "requested_model = %s" in select_sql
    assert params is not None and params[:2] == ("site-a", "deepseek-v4-flash")
    assert connection.executed[0][1] == ("site-a",)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("49.99"), "normal"),
        (Decimal("50"), "soft_limit"),
        (Decimal("99.99"), "soft_limit"),
        (Decimal("100"), "hard_limit"),
    ],
)
def test_usage_thresholds_are_inclusive(amount: Decimal, expected: str) -> None:
    connection = _Connection([("known", 10, "known", amount, "USD", "catalog-v1")])

    usage = PostgresAgentReadService(connection).get_usage("site-a", "2026-08")

    assert usage.soft_limit_usd == Decimal("50")
    assert usage.hard_limit_usd == Decimal("100")
    assert usage.state == expected


def test_usage_with_no_ledger_rows_is_unknown_instead_of_zero() -> None:
    usage = PostgresAgentReadService(_Connection([])).get_usage("site-a", "2026-08")

    assert usage.tokens is None
    assert usage.token_state == "unknown"
    assert usage.cost.amount is None
    assert usage.cost.state == "unknown"
    assert usage.state == "unknown"


@pytest.mark.parametrize("period", ["2026-8", "2026-13", "August", ""])
def test_usage_rejects_unbounded_or_invalid_periods(period: str) -> None:
    with pytest.raises(ValueError, match="period"):
        PostgresAgentReadService(_Connection()).get_usage("site-a", period)


def _draft_row(
    proposal_id: str,
    action_type: str,
    created_at: datetime,
) -> tuple[Any, ...]:
    return (
        proposal_id,
        action_type,
        "AI Draft",
        "AI",
        "subject-1",
        3,
        ["evidence-1"],
        created_at,
        "deepseek-v4-flash",
    )


def test_draft_list_maps_three_kinds_with_stable_site_scoped_cursor() -> None:
    at = datetime(2026, 8, 7, 12, tzinfo=UTC)
    first_connection = _Connection(
        [
            _draft_row("proposal-3", "internal.work_item.propose", at),
            _draft_row("proposal-2", "internal.review_case.propose", at),
            _draft_row("proposal-1", "internal.ai_draft.propose", at),
        ]
    )

    first = PostgresAgentReadService(first_connection).list_drafts(
        "site-a",
        page_size=2,
    )

    assert [draft.kind for draft in first.drafts] == ["Work Item", "Review Case"]
    assert first.next_cursor is not None
    assert first.drafts[0].evidence == ({"ref": "evidence-1", "locator": "evidence://evidence-1"},)
    assert "document" not in first_connection.executed[-1][0].casefold()
    assert first_connection.executed[0][1] == ("site-a",)

    second_connection = _Connection([_draft_row("proposal-1", "internal.ai_draft.propose", at)])
    second = PostgresAgentReadService(second_connection).list_drafts(
        "site-a",
        cursor=first.next_cursor,
        page_size=2,
    )
    assert [draft.kind for draft in second.drafts] == ["CEO Informal Observation"]
    assert second.next_cursor is None
    assert second_connection.executed[-1][1] is not None
    assert "proposal-2" in second_connection.executed[-1][1]


def test_get_draft_is_site_scoped_and_exposes_only_v4_summary_fields() -> None:
    row = _draft_row(
        "proposal-1",
        "internal.ai_draft.propose",
        datetime(2026, 8, 7, 12, tzinfo=UTC),
    )
    connection = _Connection([row])

    draft = PostgresAgentReadService(connection).get_draft("site-a", "proposal-1")

    assert draft is not None
    assert draft.to_wire() == {
        "draft_id": "proposal-1",
        "kind": "CEO Informal Observation",
        "status": "AI Draft",
        "origin": "AI",
        "subject": "subject-1",
        "evidence": [{"ref": "evidence-1", "locator": "evidence://evidence-1"}],
        "model": {
            "name": "deepseek-v4-flash",
            "version": "deepseek-v4-flash",
        },
        "revision": 3,
    }
    assert connection.executed[0][1] == ("site-a",)
    params = connection.executed[-1][1]
    assert params is not None and params[:2] == ("site-a", "proposal-1")
