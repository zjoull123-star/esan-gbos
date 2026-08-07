"""Site-scoped, content-free read projections for the local Agent runtime."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from .postgres import Connection

_MODEL: Literal["deepseek-v4-flash"] = "deepseek-v4-flash"
_SOFT_LIMIT_USD = Decimal("50")
_HARD_LIMIT_USD = Decimal("100")
_PERIOD_PATTERN = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_DRAFT_KINDS: dict[str, Literal["Work Item", "Review Case", "CEO Informal Observation"]] = {
    "internal.work_item.propose": "Work Item",
    "internal.review_case.propose": "Review Case",
    "internal.ai_draft.propose": "CEO Informal Observation",
}
_DRAFT_COLUMNS = """
    proposal.proposal_id,
    proposal.action_type,
    proposal.review_status,
    proposal.origin,
    proposal.subject_ref,
    proposal.subject_revision,
    proposal.evidence_refs,
    proposal.created_at,
    COALESCE(
        (
            SELECT COALESCE(invocation.observed_model, invocation.requested_model)
            FROM jsonb_array_elements_text(proposal.invocation_ids) AS ref(invocation_id)
            JOIN agent_runtime.model_invocations AS invocation
              ON invocation.site_id = proposal.site_id
             AND invocation.invocation_id = ref.invocation_id
             AND invocation.requested_model = 'deepseek-v4-flash'
            ORDER BY invocation.started_at ASC, invocation.invocation_id ASC
            LIMIT 1
        ),
        'unknown'
    ) AS model_version
"""

AggregateState = Literal["known", "partial", "unknown"]
BudgetState = Literal["normal", "soft_limit", "hard_limit", "unknown"]
DraftKind = Literal["Work Item", "Review Case", "CEO Informal Observation"]


@dataclass(frozen=True, slots=True)
class UsageCost:
    currency: Literal["USD"]
    amount: Decimal | None
    state: AggregateState

    def to_wire(self) -> dict[str, str | float | None]:
        return {
            "currency": self.currency,
            "amount": None if self.amount is None else float(self.amount),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: Literal["deepseek-v4-flash"]
    period: str
    tokens: int | None
    token_state: AggregateState
    cost: UsageCost
    soft_limit_usd: Decimal
    hard_limit_usd: Decimal
    state: BudgetState

    def to_wire(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "period": self.period,
            "tokens": self.tokens,
            "token_state": self.token_state,
            "cost": self.cost.to_wire(),
            "soft_limit_usd": float(self.soft_limit_usd),
            "hard_limit_usd": float(self.hard_limit_usd),
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class AiDraft:
    draft_id: str
    kind: DraftKind
    status: Literal["AI Draft"]
    origin: Literal["AI"]
    subject: str
    evidence: tuple[dict[str, str], ...]
    model_version: str
    revision: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "kind": self.kind,
            "status": self.status,
            "origin": self.origin,
            "subject": self.subject,
            "evidence": list(self.evidence),
            "model": {"name": _MODEL, "version": self.model_version},
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class AiDraftPage:
    drafts: tuple[AiDraft, ...]
    next_cursor: str | None


class PostgresAgentReadService:
    """Bounded PostgreSQL reads that always set the current RLS site."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresAgentReadService(connection=<redacted>)"

    def get_usage(self, site_id: str, period: str) -> ModelUsage:
        _require_site(site_id)
        start, end = _period_bounds(period)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT token_usage_status, total_tokens,
                       cost_status, cost_amount, cost_currency,
                       price_catalog_version
                FROM agent_runtime.model_invocations
                WHERE site_id = %s
                  AND requested_model = %s
                  AND started_at >= %s
                  AND started_at < %s
                ORDER BY started_at ASC, invocation_id ASC
                """,
                (site_id, _MODEL, start, end),
            )
            rows = cursor.fetchall()
        token_values = [int(row[1]) for row in rows if row[0] == "known" and row[1] is not None]
        token_state = _aggregate_state(len(token_values), len(rows))
        tokens = sum(token_values) if token_values else None
        cost_values = [
            Decimal(str(row[3]))
            for row in rows
            if (row[2] == "known" and row[3] is not None and row[4] == "USD" and row[5] is not None)
        ]
        cost_state = _aggregate_state(len(cost_values), len(rows))
        amount = sum(cost_values, Decimal(0)) if cost_values else None
        budget_state = _budget_state(amount, cost_state)
        return ModelUsage(
            model=_MODEL,
            period=period,
            tokens=tokens,
            token_state=token_state,
            cost=UsageCost(currency="USD", amount=amount, state=cost_state),
            soft_limit_usd=_SOFT_LIMIT_USD,
            hard_limit_usd=_HARD_LIMIT_USD,
            state=budget_state,
        )

    def list_drafts(
        self,
        site_id: str,
        *,
        cursor: str | None = None,
        page_size: int = 20,
        status: Literal["AI Draft", "Pending"] | None = None,
    ) -> AiDraftPage:
        _require_site(site_id)
        if isinstance(page_size, bool) or not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")
        if status not in {None, "AI Draft", "Pending"}:
            raise ValueError("unsupported draft status")
        cursor_value = _decode_cursor(cursor) if cursor is not None else None
        predicates = [
            "proposal.site_id = %s",
            "proposal.action_type = ANY(%s)",
        ]
        params: list[Any] = [site_id, list(_DRAFT_KINDS)]
        if status is not None:
            predicates.append("proposal.review_status = %s")
            params.append(status)
        if cursor_value is not None:
            predicates.append(
                """
                (
                    proposal.created_at < %s
                    OR (
                        proposal.created_at = %s
                        AND proposal.proposal_id < %s
                    )
                )
                """
            )
            params.extend(
                [
                    cursor_value[0],
                    cursor_value[0],
                    cursor_value[1],
                ]
            )
        params.append(page_size + 1)
        with self._connection.transaction(), self._connection.cursor() as db_cursor:
            _set_site(db_cursor, site_id)
            db_cursor.execute(
                f"""
                SELECT {_DRAFT_COLUMNS}
                FROM agent_runtime.action_proposals AS proposal
                WHERE {" AND ".join(predicates)}
                ORDER BY proposal.created_at DESC, proposal.proposal_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = db_cursor.fetchall()
        has_more = len(rows) > page_size
        visible_rows = rows[:page_size]
        drafts = tuple(_draft_from_row(row) for row in visible_rows)
        next_cursor = None
        if has_more and visible_rows:
            next_cursor = _encode_cursor(visible_rows[-1][7], str(visible_rows[-1][0]))
        return AiDraftPage(drafts=drafts, next_cursor=next_cursor)

    def get_draft(self, site_id: str, draft_id: str) -> AiDraft | None:
        _require_site(site_id)
        if not draft_id or len(draft_id) > 256:
            raise ValueError("draft_id must be non-empty and at most 256 characters")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_DRAFT_COLUMNS}
                FROM agent_runtime.action_proposals AS proposal
                WHERE proposal.site_id = %s
                  AND proposal.proposal_id = %s
                  AND proposal.action_type = ANY(%s)
                """,
                (site_id, draft_id, list(_DRAFT_KINDS)),
            )
            row = cursor.fetchone()
        return None if row is None else _draft_from_row(row)


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    match = _PERIOD_PATTERN.fullmatch(period)
    if match is None:
        raise ValueError("period must use YYYY-MM")
    year = int(match["year"])
    month = int(match["month"])
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        return start, datetime(year + 1, 1, 1, tzinfo=UTC)
    return start, datetime(year, month + 1, 1, tzinfo=UTC)


def _require_site(site_id: str) -> None:
    if not site_id or len(site_id) > 140:
        raise ValueError("site_id must be non-empty and at most 140 characters")


def _aggregate_state(known_count: int, total_count: int) -> AggregateState:
    if total_count == 0 or known_count == 0:
        return "unknown"
    if known_count == total_count:
        return "known"
    return "partial"


def _budget_state(amount: Decimal | None, cost_state: AggregateState) -> BudgetState:
    if amount is not None and amount >= _HARD_LIMIT_USD:
        return "hard_limit"
    if amount is not None and amount >= _SOFT_LIMIT_USD:
        return "soft_limit"
    if cost_state != "known":
        return "unknown"
    return "normal"


def _draft_from_row(row: tuple[Any, ...]) -> AiDraft:
    action_type = str(row[1])
    try:
        kind = _DRAFT_KINDS[action_type]
    except KeyError as exc:
        raise ValueError("unsupported action proposal type") from exc
    evidence_refs = row[6]
    if isinstance(evidence_refs, str):
        evidence_refs = json.loads(evidence_refs)
    if not isinstance(evidence_refs, list) or not all(
        isinstance(value, str) for value in evidence_refs
    ):
        raise ValueError("invalid proposal evidence references")
    return AiDraft(
        draft_id=str(row[0]),
        kind=kind,
        status="AI Draft",
        origin="AI",
        subject=str(row[4]),
        evidence=tuple({"ref": value, "locator": f"evidence://{value}"} for value in evidence_refs),
        model_version=str(row[8]),
        revision=int(row[5]),
    )


def _encode_cursor(created_at: datetime, proposal_id: str) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "proposal_id": proposal_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    if not value or len(value) > 2048:
        raise ValueError("invalid draft cursor")
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode())
        payload = json.loads(decoded)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"created_at", "proposal_id"}
            or not isinstance(payload["created_at"], str)
            or not isinstance(payload["proposal_id"], str)
            or not payload["proposal_id"]
        ):
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid draft cursor") from exc
    return created_at, payload["proposal_id"]


def _set_site(cursor: Any, site_id: str) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))


__all__ = [
    "AiDraft",
    "AiDraftPage",
    "ModelUsage",
    "PostgresAgentReadService",
    "UsageCost",
]
