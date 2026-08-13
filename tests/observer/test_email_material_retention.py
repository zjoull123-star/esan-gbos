from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from observer.email_material_retention import (
    EmailMaterialDeletionLease,
    EmailMaterialRetentionRequest,
    EmailMaterialRetentionService,
    EmailMaterialTombstoneReceipt,
    TerminalMaterialAuthority,
)
from observer.models import TenantScope

NOW = datetime(2026, 8, 14, 4, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
EVIDENCE_REF = "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
REQUEST_REF = "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV"
RECEIPT_REF = "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV"
OBJECT_REF = "obs:v1:" + "a" * 32 + ":sha256:" + "b" * 64
DIGEST = "sha256:" + "b" * 64


def _authority() -> TerminalMaterialAuthority:
    terminal_at = NOW - timedelta(days=31)
    return TerminalMaterialAuthority(
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id=SCOPE.site_id,
        purpose="email_draft_material",
        evidence_ref=EVIDENCE_REF,
        terminal_state="sent",
        terminal_at=terminal_at,
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=3,
    )


def _request() -> EmailMaterialRetentionRequest:
    authority = _authority()
    return EmailMaterialRetentionRequest(
        request_ref=REQUEST_REF,
        site_id=authority.site_id,
        purpose=authority.purpose,
        evidence_ref=authority.evidence_ref,
        material_kind="draft",
        draft_ref=authority.draft_ref,
        draft_revision=authority.draft_revision,
        object_ref=OBJECT_REF,
        digest=DIGEST,
        terminal_state=authority.terminal_state,
        terminal_at=authority.terminal_at,
        not_before=authority.terminal_at + timedelta(days=30),
        authority_receipt_ref=authority.authority_receipt_ref,
    )


def _lease() -> EmailMaterialDeletionLease:
    request = _request()
    return EmailMaterialDeletionLease(
        **request.as_dict(),
        worker_id="observer-retention-1",
        lease_generation=2,
        lease_expires_at=NOW + timedelta(minutes=5),
    )


class _Registrar:
    def __init__(self, authority: TerminalMaterialAuthority | None = None) -> None:
        self.authority = authority or _authority()
        self.calls: list[tuple[TenantScope, str]] = []

    def resolve_terminal(
        self, scope: TenantScope, authority_receipt_ref: str
    ) -> TerminalMaterialAuthority:
        self.calls.append((scope, authority_receipt_ref))
        return self.authority


class _Repository:
    def __init__(self) -> None:
        self.registered: list[tuple[TerminalMaterialAuthority, datetime]] = []
        self.leases: tuple[EmailMaterialDeletionLease, ...] = ()
        self.completed: list[tuple[EmailMaterialDeletionLease, str, datetime]] = []
        self.receipt: EmailMaterialTombstoneReceipt | None = None
        self.held = False

    def register(
        self,
        scope: TenantScope,
        *,
        authority: TerminalMaterialAuthority,
        not_before: datetime,
    ) -> EmailMaterialRetentionRequest:
        assert scope == SCOPE
        self.registered.append((authority, not_before))
        return _request()

    def claim_due(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[EmailMaterialDeletionLease, ...]:
        assert scope == SCOPE
        assert worker_id == "observer-retention-1"
        assert now == NOW
        assert lease_until == NOW + timedelta(minutes=5)
        assert limit == 10
        return self.leases

    def complete_deletion(
        self,
        scope: TenantScope,
        lease: EmailMaterialDeletionLease,
        *,
        receipt_ref: str,
        deleted_at: datetime,
    ) -> EmailMaterialTombstoneReceipt:
        assert scope == SCOPE
        self.completed.append((lease, receipt_ref, deleted_at))
        return EmailMaterialTombstoneReceipt.from_lease(
            lease,
            receipt_ref=receipt_ref,
            deleted_at=deleted_at,
        )

    def resolve_receipt(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        tombstone_receipt_ref: str,
    ) -> EmailMaterialTombstoneReceipt | None:
        assert scope == SCOPE
        assert evidence_ref == EVIDENCE_REF
        assert tombstone_receipt_ref == RECEIPT_REF
        return self.receipt

    def has_legal_hold(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        checked_at: datetime,
    ) -> bool:
        assert scope == SCOPE
        assert evidence_ref == EVIDENCE_REF
        assert checked_at >= NOW
        return self.held


class _FakeCas:
    def __init__(self) -> None:
        self.deleted: list[tuple[TenantScope, str]] = []
        self.failure: Exception | None = None

    def delete(self, scope: TenantScope, object_ref: str) -> None:
        self.deleted.append((scope, object_ref))
        if self.failure is not None:
            raise self.failure


def _service(
    repository: _Repository,
    cas: _FakeCas,
    *,
    registrar: _Registrar | None,
) -> EmailMaterialRetentionService:
    return EmailMaterialRetentionService(
        repository=repository,
        cas=cas,
        authoritative_registrar=registrar,
        worker_id="observer-retention-1",
        clock=lambda: NOW,
    )


def test_registration_uses_authoritative_resolution_and_exact_thirty_day_boundary() -> None:
    repository = _Repository()
    registrar = _Registrar()
    service = _service(repository, _FakeCas(), registrar=registrar)

    result = service.register_terminal(
        SCOPE,
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )

    assert result == _request()
    assert registrar.calls == [(SCOPE, "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV")]
    assert repository.registered == [(_authority(), _authority().terminal_at + timedelta(days=30))]


def test_registration_is_fail_closed_without_an_authoritative_registrar() -> None:
    repository = _Repository()
    service = _service(repository, _FakeCas(), registrar=None)

    with pytest.raises(PermissionError, match="authoritative registrar"):
        service.register_terminal(
            SCOPE,
            authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )

    assert repository.registered == []


def test_bounded_worker_completes_receipt_only_after_injected_cas_delete() -> None:
    repository = _Repository()
    repository.leases = (_lease(),)
    cas = _FakeCas()
    service = _service(repository, cas, registrar=None)

    receipts = service.run_once(SCOPE, batch_size=10)

    assert cas.deleted == [(SCOPE, OBJECT_REF)]
    assert len(repository.completed) == 1
    assert repository.completed[0][0] == _lease()
    assert repository.completed[0][2] == NOW
    assert receipts == (
        EmailMaterialTombstoneReceipt.from_lease(
            _lease(),
            receipt_ref=repository.completed[0][1],
            deleted_at=NOW,
        ),
    )


def test_failed_cas_delete_never_completes_or_issues_receipt() -> None:
    repository = _Repository()
    repository.leases = (_lease(),)
    cas = _FakeCas()
    cas.failure = OSError("injected")
    service = _service(repository, cas, registrar=None)

    with pytest.raises(OSError, match="injected"):
        service.run_once(SCOPE, batch_size=10)

    assert repository.completed == []


def test_verify_uses_immutable_receipt_and_rejects_premature_or_held_checks() -> None:
    repository = _Repository()
    repository.receipt = EmailMaterialTombstoneReceipt.from_lease(
        _lease(), receipt_ref=RECEIPT_REF, deleted_at=NOW
    )
    service = _service(repository, _FakeCas(), registrar=None)

    assert service.verify_tombstone(
        SCOPE,
        evidence_ref=EVIDENCE_REF,
        tombstone_receipt_ref=RECEIPT_REF,
        checked_at=NOW + timedelta(seconds=1),
    )
    assert (
        service.verify_tombstone(
            SCOPE,
            evidence_ref=EVIDENCE_REF,
            tombstone_receipt_ref=RECEIPT_REF,
            checked_at=NOW - timedelta(seconds=1),
        )
        is False
    )
    repository.held = True
    assert (
        service.verify_tombstone(
            SCOPE,
            evidence_ref=EVIDENCE_REF,
            tombstone_receipt_ref=RECEIPT_REF,
            checked_at=NOW + timedelta(seconds=1),
        )
        is False
    )


def test_verify_rejects_missing_receipt_and_cross_site_receipt_drift() -> None:
    repository = _Repository()
    service = _service(repository, _FakeCas(), registrar=None)
    assert (
        service.verify_tombstone(
            SCOPE,
            evidence_ref=EVIDENCE_REF,
            tombstone_receipt_ref=RECEIPT_REF,
            checked_at=NOW,
        )
        is False
    )

    repository.receipt = replace(
        EmailMaterialTombstoneReceipt.from_lease(_lease(), receipt_ref=RECEIPT_REF, deleted_at=NOW),
        site_id="other.example",
    )
    assert (
        service.verify_tombstone(
            SCOPE,
            evidence_ref=EVIDENCE_REF,
            tombstone_receipt_ref=RECEIPT_REF,
            checked_at=NOW,
        )
        is False
    )
