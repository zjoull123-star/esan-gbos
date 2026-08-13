from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from observer.email_material_retention import (
    EmailMaterialDeletionLease,
    TerminalMaterialAuthority,
)
from observer.email_material_retention_repository import (
    PostgresEmailMaterialRetentionRepository,
)
from observer.models import TenantScope

NOW = datetime(2026, 8, 14, 4, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
EVIDENCE_REF = "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
REQUEST_REF = "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
RECEIPT_REF = "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
OBJECT_REF = "obs:v1:" + "a" * 32 + ":sha256:" + "b" * 64
DIGEST = "sha256:" + "b" * 64


class _Cursor:
    def __init__(self, rowsets: list[list[tuple[object, ...]]]) -> None:
        self.rowsets = iter(rowsets)
        self.current: list[tuple[object, ...]] = []
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self.calls.append((statement, params))
        self.current = list(next(self.rowsets, []))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.current.pop(0) if self.current else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows = self.current
        self.current = []
        return rows


class _Connection:
    def __init__(self, rowsets: list[list[tuple[object, ...]]]) -> None:
        self.query_cursor = _Cursor(rowsets)

    def transaction(self) -> _Connection:
        return self

    def cursor(self) -> _Cursor:
        return self.query_cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _authority() -> TerminalMaterialAuthority:
    return TerminalMaterialAuthority(
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose="email_draft_material",
        evidence_ref=EVIDENCE_REF,
        terminal_state="sent",
        terminal_at=NOW - timedelta(days=31),
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=3,
    )


def _request_row() -> tuple[object, ...]:
    authority = _authority()
    return (
        REQUEST_REF,
        SCOPE.site_id,
        authority.purpose,
        EVIDENCE_REF,
        "draft",
        authority.draft_ref,
        authority.draft_revision,
        OBJECT_REF,
        DIGEST,
        authority.terminal_state,
        authority.terminal_at,
        authority.terminal_at + timedelta(days=30),
        authority.authority_receipt_ref,
    )


def _lease_row() -> tuple[object, ...]:
    return (
        *_request_row(),
        "observer-retention-1",
        2,
        NOW + timedelta(minutes=5),
    )


def test_register_calls_database_resolver_and_returns_revision_pinned_request() -> None:
    connection = _Connection([[], [_request_row()]])
    repository = PostgresEmailMaterialRetentionRepository(connection)

    result = repository.register(
        SCOPE,
        authority=_authority(),
        not_before=_authority().terminal_at + timedelta(days=30),
    )

    assert result.request_ref == REQUEST_REF
    assert result.object_ref == OBJECT_REF
    assert result.digest == DIGEST
    assert result.draft_revision == 3
    statement, params = connection.query_cursor.calls[1]
    assert "register_email_material_retention" in statement
    assert params == (
        SCOPE.site_id,
        "email_draft_material",
        EVIDENCE_REF,
        "sent",
        _authority().terminal_at,
        _authority().terminal_at + timedelta(days=30),
        _authority().authority_receipt_ref,
        _authority().draft_ref,
        3,
    )


def test_claim_is_bounded_and_returns_fenced_exact_cas_binding() -> None:
    connection = _Connection([[], [_lease_row()]])
    repository = PostgresEmailMaterialRetentionRepository(connection)

    result = repository.claim_due(
        SCOPE,
        worker_id="observer-retention-1",
        now=NOW,
        lease_until=NOW + timedelta(minutes=5),
        limit=10,
    )

    assert result == (EmailMaterialDeletionLease.from_row(_lease_row()),)
    statement, params = connection.query_cursor.calls[1]
    assert "claim_email_material_retention" in statement
    assert params == (
        SCOPE.site_id,
        "observer-retention-1",
        NOW,
        NOW + timedelta(minutes=5),
        10,
    )


def test_complete_returns_only_database_created_immutable_receipt() -> None:
    lease = EmailMaterialDeletionLease.from_row(_lease_row())
    receipt_row = (
        RECEIPT_REF,
        *_request_row(),
        NOW,
    )
    connection = _Connection([[], [receipt_row]])
    repository = PostgresEmailMaterialRetentionRepository(connection)

    result = repository.complete_deletion(
        SCOPE,
        lease,
        receipt_ref=RECEIPT_REF,
        deleted_at=NOW,
    )

    assert result.tombstone_receipt_ref == RECEIPT_REF
    assert result.object_ref == OBJECT_REF
    assert result.digest == DIGEST
    statement, params = connection.query_cursor.calls[1]
    assert "complete_email_material_retention" in statement
    assert params == (
        SCOPE.site_id,
        REQUEST_REF,
        "observer-retention-1",
        2,
        RECEIPT_REF,
        NOW,
    )


def test_receipt_resolution_and_legal_hold_are_observer_owned_queries() -> None:
    receipt_row = (RECEIPT_REF, *_request_row(), NOW)
    receipt_repo = PostgresEmailMaterialRetentionRepository(_Connection([[], [receipt_row]]))
    receipt = receipt_repo.resolve_receipt(
        SCOPE,
        evidence_ref=EVIDENCE_REF,
        tombstone_receipt_ref=RECEIPT_REF,
    )
    assert receipt is not None
    assert receipt.digest == DIGEST
    assert "resolve_email_material_tombstone" in receipt_repo._connection.query_cursor.calls[1][0]

    held_repo = PostgresEmailMaterialRetentionRepository(_Connection([[], [(True,)]]))
    assert held_repo.has_legal_hold(
        SCOPE,
        evidence_ref=EVIDENCE_REF,
        checked_at=NOW,
    )
    assert "email_material_has_legal_hold" in held_repo._connection.query_cursor.calls[1][0]


@pytest.mark.parametrize(
    "row,succeeds",
    [
        ((True, True, True, True, False, False), True),
        ((False, True, True, True, False, False), False),
        ((True, True, True, True, True, False), False),
    ],
)
def test_preflight_requires_all_tables_forced_rls_and_no_direct_mutation_grants(
    row: tuple[bool, ...], succeeds: bool
) -> None:
    repository = PostgresEmailMaterialRetentionRepository(_Connection([[row]]))
    if succeeds:
        repository.preflight()
    else:
        with pytest.raises(ValueError, match="preflight"):
            repository.preflight()
