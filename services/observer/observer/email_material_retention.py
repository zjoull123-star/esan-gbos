"""Observer-owned retention for terminal email draft and final-MIME CAS material."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

from .models import TenantScope, stable_ulid

_EMAIL_MATERIAL_PURPOSE = "email_draft_material"
_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")
_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_DRAFT_REF = re.compile(r"^DRF-[0-9A-HJKMNP-TV-Z]{26}$")
_OBJECT_REF = re.compile(r"^obs:v1:[a-f0-9]{32}:sha256:[a-f0-9]{64}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _row_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {field_name}")
    return value


def _row_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"invalid {field_name}")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class TerminalMaterialAuthority:
    """Terminal state resolved by an injected authoritative registrar."""

    authority_receipt_ref: str
    site_id: str
    purpose: str
    evidence_ref: str
    terminal_state: Literal["sent", "discarded"]
    terminal_at: datetime
    draft_ref: str
    draft_revision: int

    def __post_init__(self) -> None:
        if (
            _REF.fullmatch(self.authority_receipt_ref) is None
            or self.purpose != _EMAIL_MATERIAL_PURPOSE
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or self.terminal_state not in {"sent", "discarded"}
            or _DRAFT_REF.fullmatch(self.draft_ref) is None
            or isinstance(self.draft_revision, bool)
            or not 1 <= self.draft_revision <= 2_147_483_647
        ):
            raise ValueError("invalid terminal email material authority")
        _aware(self.terminal_at, "terminal_at")

    def __repr__(self) -> str:
        return (
            "TerminalMaterialAuthority("
            f"site_id={self.site_id!r}, purpose={self.purpose!r}, "
            f"terminal_state={self.terminal_state!r}, "
            f"draft_revision={self.draft_revision}, identifiers=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialRetentionRequest:
    request_ref: str
    site_id: str
    purpose: str
    evidence_ref: str
    material_kind: Literal["draft", "final_mime"]
    draft_ref: str
    draft_revision: int
    object_ref: str
    digest: str
    terminal_state: Literal["sent", "discarded"]
    terminal_at: datetime
    not_before: datetime
    authority_receipt_ref: str

    def __post_init__(self) -> None:
        terminal_at = _aware(self.terminal_at, "terminal_at")
        not_before = _aware(self.not_before, "not_before")
        if (
            _REF.fullmatch(self.request_ref) is None
            or self.purpose != _EMAIL_MATERIAL_PURPOSE
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or self.material_kind not in {"draft", "final_mime"}
            or _DRAFT_REF.fullmatch(self.draft_ref) is None
            or isinstance(self.draft_revision, bool)
            or not 1 <= self.draft_revision <= 2_147_483_647
            or _OBJECT_REF.fullmatch(self.object_ref) is None
            or _DIGEST.fullmatch(self.digest) is None
            or not self.object_ref.endswith(self.digest)
            or self.terminal_state not in {"sent", "discarded"}
            or _REF.fullmatch(self.authority_receipt_ref) is None
            or not_before != terminal_at + timedelta(days=30)
        ):
            raise ValueError("invalid email material retention request")

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> EmailMaterialRetentionRequest:
        if len(row) != 13:
            raise ValueError("invalid email material retention request row")
        return cls(
            request_ref=str(row[0]),
            site_id=str(row[1]),
            purpose=str(row[2]),
            evidence_ref=str(row[3]),
            material_kind=cast(Literal["draft", "final_mime"], str(row[4])),
            draft_ref=str(row[5]),
            draft_revision=_row_int(row[6], "draft_revision"),
            object_ref=str(row[7]),
            digest=str(row[8]),
            terminal_state=cast(Literal["sent", "discarded"], str(row[9])),
            terminal_at=_row_datetime(row[10], "terminal_at"),
            not_before=_row_datetime(row[11], "not_before"),
            authority_receipt_ref=str(row[12]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "request_ref": self.request_ref,
            "site_id": self.site_id,
            "purpose": self.purpose,
            "evidence_ref": self.evidence_ref,
            "material_kind": self.material_kind,
            "draft_ref": self.draft_ref,
            "draft_revision": self.draft_revision,
            "object_ref": self.object_ref,
            "digest": self.digest,
            "terminal_state": self.terminal_state,
            "terminal_at": self.terminal_at,
            "not_before": self.not_before,
            "authority_receipt_ref": self.authority_receipt_ref,
        }

    def registration_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": self.site_id,
            "evidence_ref": self.evidence_ref,
            "request_ref": self.request_ref,
            "not_before": self.not_before.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }

    def __repr__(self) -> str:
        return (
            "EmailMaterialRetentionRequest("
            f"site_id={self.site_id!r}, material_kind={self.material_kind!r}, "
            f"draft_revision={self.draft_revision}, terminal_state={self.terminal_state!r}, "
            "identifiers=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialDeletionLease(EmailMaterialRetentionRequest):
    worker_id: str
    lease_generation: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 256
            or isinstance(self.lease_generation, bool)
            or self.lease_generation < 1
        ):
            raise ValueError("invalid email material deletion lease")
        _aware(self.lease_expires_at, "lease_expires_at")

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> EmailMaterialDeletionLease:
        if len(row) != 16:
            raise ValueError("invalid email material deletion lease row")
        request = EmailMaterialRetentionRequest.from_row(row[:13])
        return cls(
            request_ref=request.request_ref,
            site_id=request.site_id,
            purpose=request.purpose,
            evidence_ref=request.evidence_ref,
            material_kind=request.material_kind,
            draft_ref=request.draft_ref,
            draft_revision=request.draft_revision,
            object_ref=request.object_ref,
            digest=request.digest,
            terminal_state=request.terminal_state,
            terminal_at=request.terminal_at,
            not_before=request.not_before,
            authority_receipt_ref=request.authority_receipt_ref,
            worker_id=str(row[13]),
            lease_generation=_row_int(row[14], "lease_generation"),
            lease_expires_at=_row_datetime(row[15], "lease_expires_at"),
        )


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialTombstoneReceipt(EmailMaterialRetentionRequest):
    tombstone_receipt_ref: str
    deleted_at: datetime

    def __post_init__(self) -> None:
        super().__post_init__()
        deleted_at = _aware(self.deleted_at, "deleted_at")
        if _REF.fullmatch(
            self.tombstone_receipt_ref
        ) is None or deleted_at < self.not_before.astimezone(UTC):
            raise ValueError("invalid email material tombstone receipt")

    @classmethod
    def from_lease(
        cls,
        lease: EmailMaterialDeletionLease,
        *,
        receipt_ref: str,
        deleted_at: datetime,
    ) -> EmailMaterialTombstoneReceipt:
        return cls(
            request_ref=lease.request_ref,
            site_id=lease.site_id,
            purpose=lease.purpose,
            evidence_ref=lease.evidence_ref,
            material_kind=lease.material_kind,
            draft_ref=lease.draft_ref,
            draft_revision=lease.draft_revision,
            object_ref=lease.object_ref,
            digest=lease.digest,
            terminal_state=lease.terminal_state,
            terminal_at=lease.terminal_at,
            not_before=lease.not_before,
            authority_receipt_ref=lease.authority_receipt_ref,
            tombstone_receipt_ref=receipt_ref,
            deleted_at=deleted_at,
        )

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> EmailMaterialTombstoneReceipt:
        if len(row) != 15:
            raise ValueError("invalid email material tombstone receipt row")
        request = EmailMaterialRetentionRequest.from_row(row[1:14])
        return cls(
            request_ref=request.request_ref,
            site_id=request.site_id,
            purpose=request.purpose,
            evidence_ref=request.evidence_ref,
            material_kind=request.material_kind,
            draft_ref=request.draft_ref,
            draft_revision=request.draft_revision,
            object_ref=request.object_ref,
            digest=request.digest,
            terminal_state=request.terminal_state,
            terminal_at=request.terminal_at,
            not_before=request.not_before,
            authority_receipt_ref=request.authority_receipt_ref,
            tombstone_receipt_ref=str(row[0]),
            deleted_at=_row_datetime(row[14], "deleted_at"),
        )


class AuthoritativeTerminalRegistrar(Protocol):
    def resolve_terminal(
        self,
        scope: TenantScope,
        authority_receipt_ref: str,
    ) -> TerminalMaterialAuthority: ...


class EmailMaterialRetentionRepository(Protocol):
    def register(
        self,
        scope: TenantScope,
        *,
        authority: TerminalMaterialAuthority,
        not_before: datetime,
    ) -> EmailMaterialRetentionRequest: ...

    def claim_due(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[EmailMaterialDeletionLease, ...]: ...

    def complete_deletion(
        self,
        scope: TenantScope,
        lease: EmailMaterialDeletionLease,
        *,
        receipt_ref: str,
        deleted_at: datetime,
    ) -> EmailMaterialTombstoneReceipt: ...

    def resolve_receipt(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        tombstone_receipt_ref: str,
    ) -> EmailMaterialTombstoneReceipt | None: ...

    def has_legal_hold(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        checked_at: datetime,
    ) -> bool: ...


class CasDelete(Protocol):
    def delete(self, scope: TenantScope, object_ref: str) -> None: ...


class EmailMaterialRetentionService:
    """Register authority, run bounded fenced deletion, and verify durable receipts."""

    def __init__(
        self,
        *,
        repository: EmailMaterialRetentionRepository,
        cas: CasDelete,
        authoritative_registrar: AuthoritativeTerminalRegistrar | None,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> None:
        if not worker_id or len(worker_id) > 256 or not callable(clock):
            raise ValueError("invalid email material retention dependencies")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(hours=1):
            raise ValueError("invalid email material retention lease duration")
        self._repository = repository
        self._cas = cas
        self._authoritative_registrar = authoritative_registrar
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration

    def __repr__(self) -> str:
        return "EmailMaterialRetentionService(dependencies=<redacted>)"

    def register_terminal(
        self,
        scope: TenantScope,
        *,
        authority_receipt_ref: str,
    ) -> EmailMaterialRetentionRequest:
        if self._authoritative_registrar is None:
            raise PermissionError("authoritative registrar is unavailable")
        if _REF.fullmatch(authority_receipt_ref) is None:
            raise ValueError("invalid terminal authority receipt")
        authority = self._authoritative_registrar.resolve_terminal(
            scope,
            authority_receipt_ref,
        )
        if (
            authority.site_id != scope.site_id
            or authority.purpose != _EMAIL_MATERIAL_PURPOSE
            or not hmac.compare_digest(
                authority.authority_receipt_ref,
                authority_receipt_ref,
            )
        ):
            raise PermissionError("terminal authority scope mismatch")
        not_before = _aware(authority.terminal_at, "terminal_at") + timedelta(days=30)
        return self._repository.register(
            scope,
            authority=authority,
            not_before=not_before,
        )

    def run_once(
        self,
        scope: TenantScope,
        *,
        batch_size: int,
    ) -> tuple[EmailMaterialTombstoneReceipt, ...]:
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
            raise ValueError("email material retention batch size is outside bounds")
        now = _aware(self._clock(), "clock")
        leases = self._repository.claim_due(
            scope,
            worker_id=self._worker_id,
            now=now,
            lease_until=now + self._lease_duration,
            limit=batch_size,
        )
        completed: list[EmailMaterialTombstoneReceipt] = []
        for lease in leases:
            if lease.site_id != scope.site_id or lease.lease_expires_at <= now:
                raise PermissionError("email material retention lease mismatch")
            self._cas.delete(scope, lease.object_ref)
            receipt_ref = "TMB-" + stable_ulid(
                "email-material-tombstone",
                scope.site_id,
                lease.request_ref,
                lease.object_ref,
                lease.digest,
                now.isoformat(),
            )
            completed.append(
                self._repository.complete_deletion(
                    scope,
                    lease,
                    receipt_ref=receipt_ref,
                    deleted_at=now,
                )
            )
        return tuple(completed)

    def verify_tombstone(
        self,
        scope: TenantScope,
        *,
        evidence_ref: str,
        tombstone_receipt_ref: str,
        checked_at: datetime,
    ) -> bool:
        checked = _aware(checked_at, "checked_at")
        if (
            _EVIDENCE_REF.fullmatch(evidence_ref) is None
            or _REF.fullmatch(tombstone_receipt_ref) is None
        ):
            return False
        receipt = self._repository.resolve_receipt(
            scope,
            evidence_ref=evidence_ref,
            tombstone_receipt_ref=tombstone_receipt_ref,
        )
        if receipt is None:
            return False
        if (
            receipt.site_id != scope.site_id
            or receipt.purpose != _EMAIL_MATERIAL_PURPOSE
            or not hmac.compare_digest(receipt.evidence_ref, evidence_ref)
            or not hmac.compare_digest(
                receipt.tombstone_receipt_ref,
                tombstone_receipt_ref,
            )
            or receipt.deleted_at.astimezone(UTC) > checked
        ):
            return False
        return not self._repository.has_legal_hold(
            scope,
            evidence_ref=evidence_ref,
            checked_at=checked,
        )


class EmailMaterialRetentionDeletionRunner:
    """Bound one scheduler tick around an injected retention service."""

    def __init__(
        self,
        *,
        service: EmailMaterialRetentionService,
        max_batch_size: int = 100,
    ) -> None:
        if (
            isinstance(max_batch_size, bool)
            or not isinstance(max_batch_size, int)
            or not 1 <= max_batch_size <= 100
        ):
            raise ValueError("invalid email material retention max batch size")
        self._service = service
        self._max_batch_size = max_batch_size

    def __repr__(self) -> str:
        return (
            "EmailMaterialRetentionDeletionRunner("
            f"max_batch_size={self._max_batch_size}, service=<redacted>)"
        )

    def run_once(
        self,
        scope: TenantScope,
        *,
        batch_size: int,
    ) -> tuple[EmailMaterialTombstoneReceipt, ...]:
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= self._max_batch_size
        ):
            raise ValueError("email material retention batch size is outside bounds")
        return self._service.run_once(scope, batch_size=batch_size)


__all__ = [
    "AuthoritativeTerminalRegistrar",
    "EmailMaterialDeletionLease",
    "EmailMaterialRetentionDeletionRunner",
    "EmailMaterialRetentionRepository",
    "EmailMaterialRetentionRequest",
    "EmailMaterialRetentionService",
    "EmailMaterialTombstoneReceipt",
    "TerminalMaterialAuthority",
]
