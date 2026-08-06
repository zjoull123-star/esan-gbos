from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import StoredObject, TenantScope
from .protocols import EvidenceStore


class LifecycleError(ValueError):
    pass


class LegalHoldError(LifecycleError):
    pass


class EvidenceNotFound(LifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class DeletionTombstone:
    site_id: str
    evidence_id: str
    sha256: str
    retention_class: str
    deleted_at: datetime
    reason: str


@dataclass(slots=True)
class _LifecycleRecord:
    scope: TenantScope
    evidence_id: str
    stored: StoredObject
    retention_class: str
    recorded_at: datetime
    withdrawn_at: datetime | None = None
    withdrawal_reason: str | None = None
    hold_id: str | None = None
    tombstone: DeletionTombstone | None = None


_RETENTION_WINDOWS = {
    "R0-ephemeral": timedelta(0),
    "R1-operational": timedelta(days=30),
    "R2-record": None,
    "R3-legal-hold": None,
}


class EvidenceLifecycle:
    def __init__(self, store: EvidenceStore) -> None:
        self._store = store
        self._records: dict[tuple[str, str], _LifecycleRecord] = {}

    def register(
        self,
        *,
        scope: TenantScope,
        evidence_id: str,
        stored: StoredObject,
        retention_class: str,
        recorded_at: datetime,
    ) -> None:
        if retention_class not in _RETENTION_WINDOWS:
            raise ValueError("unknown retention_class")
        key = (scope.site_id, evidence_id)
        if key in self._records:
            raise LifecycleError("evidence lifecycle already registered")
        self._records[key] = _LifecycleRecord(
            scope=scope,
            evidence_id=evidence_id,
            stored=stored,
            retention_class=retention_class,
            recorded_at=recorded_at,
        )

    def retention_due_at(self, scope: TenantScope, evidence_id: str) -> datetime | None:
        record = self._get(scope, evidence_id)
        window = _RETENTION_WINDOWS[record.retention_class]
        return None if window is None else record.recorded_at + window

    def withdraw(
        self,
        scope: TenantScope,
        evidence_id: str,
        *,
        at: datetime,
        reason: str,
    ) -> None:
        if not reason.strip():
            raise ValueError("withdrawal reason is required")
        record = self._get(scope, evidence_id)
        record.withdrawn_at = at
        record.withdrawal_reason = reason

    def place_legal_hold(
        self,
        scope: TenantScope,
        evidence_id: str,
        *,
        hold_id: str,
        at: datetime,
    ) -> None:
        del at
        if not hold_id:
            raise ValueError("hold_id is required")
        record = self._get(scope, evidence_id)
        if record.tombstone is not None:
            raise LifecycleError("cannot hold deleted evidence")
        if record.hold_id is not None and record.hold_id != hold_id:
            raise LegalHoldError("another legal hold is already active")
        record.hold_id = hold_id

    def release_legal_hold(
        self,
        scope: TenantScope,
        evidence_id: str,
        *,
        hold_id: str,
        at: datetime,
    ) -> None:
        del at
        record = self._get(scope, evidence_id)
        if record.hold_id != hold_id:
            raise LegalHoldError("legal hold identity mismatch")
        record.hold_id = None

    def delete(
        self,
        scope: TenantScope,
        evidence_id: str,
        *,
        at: datetime,
        reason: str | None = None,
    ) -> DeletionTombstone:
        record = self._get(scope, evidence_id)
        if record.tombstone is not None:
            return record.tombstone
        if record.hold_id is not None:
            raise LegalHoldError("legal hold blocks deletion")
        due_at = self.retention_due_at(scope, evidence_id)
        if record.withdrawn_at is None and (due_at is None or at < due_at):
            raise LifecycleError("evidence is not eligible for deletion")

        deletion_reason = reason or record.withdrawal_reason or "retention expired"
        self._store.delete(scope, record.stored.object_ref)
        tombstone = DeletionTombstone(
            site_id=scope.site_id,
            evidence_id=evidence_id,
            sha256=record.stored.sha256,
            retention_class=record.retention_class,
            deleted_at=at,
            reason=deletion_reason,
        )
        record.tombstone = tombstone
        return tombstone

    def _get(self, scope: TenantScope, evidence_id: str) -> _LifecycleRecord:
        record = self._records.get((scope.site_id, evidence_id))
        if record is None or record.scope.processing_purpose != scope.processing_purpose:
            raise EvidenceNotFound("evidence lifecycle not found in tenant scope")
        return record
