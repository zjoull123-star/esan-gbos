from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.email_gateway.api import PostgresGatewayAdminRepository
from services.email_gateway.models import (
    IdempotencyConflict,
    RevisionConflict,
    TenantScope,
    ValidationError,
    stable_ref,
)

SITE = "alpha.example"
MAILBOX = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
POLICY = stable_ref("SLA", SITE, MAILBOX)
SCOPE = TenantScope(SITE, "business_operations")
NOW = datetime(2026, 8, 14, 1, 30, tzinfo=UTC)


class _Cursor:
    def __init__(
        self,
        *,
        one_rows: list[tuple[object, ...] | None] | None = None,
        all_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.one_rows = list(one_rows or [])
        self.all_rows = list(all_rows or [])
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.executions.append((" ".join(query.split()), params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.one_rows.pop(0) if self.one_rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.all_rows)

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.db = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return self.db

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _upsert(
    repository: PostgresGatewayAdminRepository,
    *,
    policy_ref: str = POLICY,
    effective_at: datetime = NOW,
    effective_at_wire: str | None = None,
    expected_revision: int = 0,
    idempotency_key: str = "sla-policy-01",
):
    return repository.upsert_sla_policy(
        SCOPE,
        mailbox_ref=MAILBOX,
        policy_ref=policy_ref,
        first_response_duration_seconds=3600,
        effective_at=effective_at,
        effective_at_wire=(
            effective_at.isoformat().replace("+00:00", "Z")
            if effective_at_wire is None
            else effective_at_wire
        ),
        expected_revision=expected_revision,
        request_id="request-01",
        idempotency_key=idempotency_key,
    )


def test_postgres_sla_upsert_is_append_only_cas_and_replays_original_revision() -> None:
    first_cursor = _Cursor(one_rows=[(MAILBOX,), None, None])
    first = _upsert(PostgresGatewayAdminRepository(_Connection(first_cursor)))  # type: ignore[arg-type]
    insert = next(
        params
        for query, params in first_cursor.executions
        if "INSERT INTO email_gateway.mailbox_sla_policies" in query
    )
    payload_digest = str(insert[-1])

    second_cursor = _Cursor(one_rows=[(MAILBOX,), None, (1, NOW)])
    second = _upsert(
        PostgresGatewayAdminRepository(_Connection(second_cursor)),  # type: ignore[arg-type]
        effective_at=NOW + timedelta(hours=1),
        expected_revision=1,
        idempotency_key="sla-policy-02",
    )
    replay_cursor = _Cursor(
        one_rows=[
            (MAILBOX,),
            (MAILBOX, POLICY, 1, 3600, NOW, payload_digest),
        ]
    )
    replay = _upsert(
        PostgresGatewayAdminRepository(_Connection(replay_cursor))  # type: ignore[arg-type]
    )

    assert (first.revision, second.revision, replay.revision) == (1, 2, 1)
    assert replay == first
    assert all(
        "UPDATE email_gateway.mailbox_sla_policies" not in query
        and "DELETE FROM email_gateway.mailbox_sla_policies" not in query
        for query, _params in first_cursor.executions + second_cursor.executions
    )


def test_postgres_sla_idempotency_digest_preserves_normalized_nanoseconds() -> None:
    effective_at = NOW.replace(microsecond=123456)
    digests: list[str] = []
    for fraction in ("123456788", "123456789"):
        cursor = _Cursor(one_rows=[(MAILBOX,), None, None])
        _upsert(
            PostgresGatewayAdminRepository(_Connection(cursor)),  # type: ignore[arg-type]
            effective_at=effective_at,
            effective_at_wire=f"2026-08-14T01:30:00.{fraction}Z",
        )
        insert = next(
            params
            for query, params in cursor.executions
            if "INSERT INTO email_gateway.mailbox_sla_policies" in query
        )
        digests.append(str(insert[-1]))

    assert digests[0] != digests[1]


def test_postgres_sla_upsert_rejects_replay_drift_stale_revision_and_policy_ref_drift() -> None:
    replay_cursor = _Cursor(
        one_rows=[
            (MAILBOX,),
            (MAILBOX, POLICY, 1, 3600, NOW, "sha256:" + "0" * 64),
        ]
    )
    with pytest.raises(IdempotencyConflict):
        _upsert(
            PostgresGatewayAdminRepository(_Connection(replay_cursor)),  # type: ignore[arg-type]
        )

    stale_cursor = _Cursor(one_rows=[(MAILBOX,), None, (2, NOW)])
    with pytest.raises(RevisionConflict):
        _upsert(
            PostgresGatewayAdminRepository(_Connection(stale_cursor)),  # type: ignore[arg-type]
            effective_at=NOW + timedelta(hours=1),
            expected_revision=1,
        )

    wrong_ref_cursor = _Cursor(one_rows=[(MAILBOX,), None, (1, NOW)])
    with pytest.raises(ValidationError):
        _upsert(
            PostgresGatewayAdminRepository(_Connection(wrong_ref_cursor)),  # type: ignore[arg-type]
            policy_ref=stable_ref("SLA", SITE, "MBX-OTHER"),
            effective_at=NOW + timedelta(hours=1),
            expected_revision=1,
        )


def test_postgres_sla_list_scopes_before_limit_and_uses_opaque_mailbox_cursor() -> None:
    rows = [
        (MAILBOX, POLICY, 3, 1800, NOW + timedelta(hours=2)),
        (MAILBOX, POLICY, 2, 2400, NOW + timedelta(hours=1)),
        (MAILBOX, POLICY, 1, 3600, NOW),
    ]
    first_cursor = _Cursor(all_rows=rows)
    first = PostgresGatewayAdminRepository(  # type: ignore[arg-type]
        _Connection(first_cursor)
    ).list_sla_policies(SITE, MAILBOX, page_size=2, cursor=None)
    policies, cursor = first
    second_cursor = _Cursor(all_rows=[rows[2]])
    second = PostgresGatewayAdminRepository(  # type: ignore[arg-type]
        _Connection(second_cursor)
    ).list_sla_policies(SITE, MAILBOX, page_size=2, cursor=cursor)

    assert [policy.revision for policy in policies] == [3, 2]
    assert cursor is not None and MAILBOX not in cursor
    assert [policy.revision for policy in second[0]] == [1]
    query, params = next(
        (query, params)
        for query, params in second_cursor.executions
        if "FROM email_gateway.mailbox_sla_policies" in query
    )
    assert "WHERE site_id = %s AND mailbox_ref = %s AND revision < %s" in query
    assert "ORDER BY revision DESC LIMIT %s" in query
    assert params == (SITE, MAILBOX, 2, 3)

    with pytest.raises(ValidationError):
        PostgresGatewayAdminRepository(_Connection(_Cursor())).list_sla_policies(  # type: ignore[arg-type]
            SITE,
            "MBX-OTHER",
            page_size=2,
            cursor=cursor,
        )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ((False, False), "sla_policy_required"),
        ((True, True), "sla_backfill_required"),
        ((True, False), None),
    ],
)
def test_postgres_mailbox_enable_readiness_has_no_default_or_backfill(
    row: tuple[bool, bool], expected: str | None
) -> None:
    cursor = _Cursor(one_rows=[row])
    result = PostgresGatewayAdminRepository(  # type: ignore[arg-type]
        _Connection(cursor)
    ).mailbox_enable_blocker(SITE, MAILBOX, activation_at=NOW)

    assert result == expected
    query, _params = next(
        (query, params)
        for query, params in cursor.executions
        if "mailbox_sla_policies AS policy" in query
    )
    assert "policy.effective_at <= %s" in query
    assert "NOT EXISTS" in query
    assert "INSERT" not in query and "UPDATE" not in query
