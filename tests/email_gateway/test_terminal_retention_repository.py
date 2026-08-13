from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.email_gateway.models import IdempotencyConflict, TenantScope
from services.email_gateway.repositories.terminal_retention import (
    PostgresTerminalRetentionRepository,
)
from services.email_gateway.terminal_retention import (
    EmailMaterialTombstoneCallback,
    HumanDiscardAuthorityReceipt,
    ObserverRegistrationReceipt,
    TerminalAuthorityRegistrationLease,
    terminal_authority_from_row,
)

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "audit_compliance")


def _authority_row(kind: str = "draft") -> tuple[object, ...]:
    return (
        "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV" if kind == "draft" else "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAW",
        SCOPE.site_id,
        "email_draft_material",
        "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        4,
        kind,
        "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV" if kind == "draft" else "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "sha256:" + ("1" if kind == "draft" else "2") * 64,
        "sent",
        NOW,
        NOW + timedelta(days=30),
        "PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "sha256:" + "3" * 64,
    )


class _Cursor:
    def __init__(self, responses: dict[str, list[tuple[object, ...]]]) -> None:
        self.responses = responses
        self.rows: list[tuple[object, ...]] = []
        self.calls: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = ()) -> None:
        self.calls.append((query, params))
        self.rows = []
        for marker, rows in self.responses.items():
            if marker in query:
                self.rows = list(rows)
                break

    def fetchone(self) -> tuple[object, ...] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self.rows = self.rows, []
        return rows

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, responses: dict[str, list[tuple[object, ...]]]) -> None:
        self.query_cursor = _Cursor(responses)

    def cursor(self) -> _Cursor:
        return self.query_cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _query_call(connection: _Connection, marker: str) -> tuple[str, object]:
    return next(call for call in connection.query_cursor.calls if marker in call[0])


def _repository(
    responses: dict[str, list[tuple[object, ...]]],
    *,
    role: str,
    database_role: str | None = None,
) -> tuple[PostgresTerminalRetentionRepository, _Connection]:
    connection = _Connection(
        {
            "SELECT current_user": [(database_role or role,)],
            **responses,
        }
    )
    return PostgresTerminalRetentionRepository(connection, actual_database_role=role), connection


def test_sent_provider_receipt_returns_exact_two_authorities_from_one_database_function() -> None:
    repository, connection = _repository(
        {
            "create_sent_email_material_authorities": [
                _authority_row(),
                _authority_row("final_mime"),
            ]
        },
        role="gbos_email_send_worker",
    )

    result = repository.create_sent_authorities(
        SCOPE,
        provider_receipt_record_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )

    assert tuple(item.material_kind for item in result) == ("draft", "final_mime")
    _query, params = _query_call(connection, "create_sent_email_material_authorities")
    assert params == (SCOPE.site_id, "PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV")


def test_noneligible_provider_receipt_returns_no_authority() -> None:
    repository, _connection = _repository(
        {"create_sent_email_material_authorities": []},
        role="gbos_email_send_worker",
    )
    assert (
        repository.create_sent_authorities(
            SCOPE,
            provider_receipt_record_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        == ()
    )


def test_discard_passes_closed_receipt_and_no_boolean_authority() -> None:
    discarded_row = (*_authority_row()[:8], "discarded", *_authority_row()[9:])
    repository, connection = _repository(
        {"create_discarded_email_material_authority": [discarded_row]},
        role="gbos_email_gateway_app",
    )
    receipt = HumanDiscardAuthorityReceipt(
        authority_receipt_ref="HDR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose="email_draft_material",
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=4,
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_digest="sha256:" + "1" * 64,
        terminal_at=NOW,
        payload_digest="sha256:" + "3" * 64,
    )

    result = repository.create_discard_authority(SCOPE, receipt=receipt)

    assert result.terminal_state == "discarded"
    query, params = _query_call(connection, "create_discarded_email_material_authority")
    assert "approved" not in query.lower()
    assert params == (
        SCOPE.site_id,
        receipt.authority_receipt_ref,
        receipt.draft_ref,
        receipt.draft_revision,
        receipt.evidence_ref,
        receipt.evidence_digest,
        receipt.terminal_at,
        receipt.payload_digest,
    )


def test_claim_and_ack_use_attempt_and_generation_fence() -> None:
    lease_row = (
        *_authority_row(),
        "ETR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "gateway-retention-1",
        2,
        3,
        NOW + timedelta(minutes=5),
    )
    repository, connection = _repository(
        {
            "claim_email_material_authority_registration": [lease_row],
            "ack_email_material_authority_registration": [(True,)],
        },
        role="gbos_email_gateway_retention_worker",
    )
    lease = repository.claim_registration(
        SCOPE,
        worker_id="gateway-retention-1",
        now=NOW,
        lease_until=NOW + timedelta(minutes=5),
    )
    assert lease == TerminalAuthorityRegistrationLease(
        authority=terminal_authority_from_row(_authority_row()),
        registration_request_ref="ETR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        worker_id="gateway-retention-1",
        attempt=2,
        lease_generation=3,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert lease is not None
    receipt = ObserverRegistrationReceipt(
        site_id=SCOPE.site_id,
        evidence_ref=lease.authority.evidence_ref,
        request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        not_before=lease.authority.not_before,
    )
    repository.ack_registration(SCOPE, lease, receipt=receipt, now=NOW)
    _query, params = _query_call(connection, "ack_email_material_authority_registration")
    assert params[2:5] == (lease.worker_id, lease.attempt, lease.lease_generation)


def test_stale_registration_generation_fails_closed() -> None:
    lease = TerminalAuthorityRegistrationLease(
        authority=terminal_authority_from_row(_authority_row()),
        registration_request_ref="ETR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        worker_id="gateway-retention-1",
        attempt=2,
        lease_generation=2,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    repository, _connection = _repository(
        {"ack_email_material_authority_registration": [(False,)]},
        role="gbos_email_gateway_retention_worker",
    )
    receipt = ObserverRegistrationReceipt(
        site_id=SCOPE.site_id,
        evidence_ref=lease.authority.evidence_ref,
        request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        not_before=lease.authority.not_before,
    )
    with pytest.raises(IdempotencyConflict, match="fence"):
        repository.ack_registration(SCOPE, lease, receipt=receipt, now=NOW)


def test_callback_acceptance_returns_database_receipt_and_passes_only_closed_fields() -> None:
    repository, connection = _repository(
        {"accept_email_material_tombstone_callback": [("GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",)]},
        role="gbos_email_gateway_retention_worker",
    )
    callback = EmailMaterialTombstoneCallback(
        site_id=SCOPE.site_id,
        purpose="email_draft_material",
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        deleted_at=NOW + timedelta(days=30),
        evidence_digest="sha256:" + "1" * 64,
        callback_payload_digest="sha256:" + "4" * 64,
    )

    result = repository.accept_tombstone_callback(SCOPE, callback=callback, now=NOW)

    assert result.callback_receipt_ref == "GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    _query, params = _query_call(connection, "accept_email_material_tombstone_callback")
    assert all("obs:v1:" not in str(value) for value in params)
    assert "@" not in repr(repository)


def test_spoofed_constructor_role_cannot_execute_under_an_app_database_session() -> None:
    repository, connection = _repository(
        {
            "create_sent_email_material_authorities": [
                _authority_row(),
                _authority_row("final_mime"),
            ]
        },
        role="gbos_email_send_worker",
        database_role="gbos_email_gateway_app",
    )

    with pytest.raises(Exception, match="database role binding rejected"):
        repository.create_sent_authorities(
            SCOPE,
            provider_receipt_record_ref="PRC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )

    assert not any(
        "create_sent_email_material_authorities" in query
        for query, _params in connection.query_cursor.calls
    )
