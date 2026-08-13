"""Provider-neutral durable authority for terminal email material retention."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

from .models import IdempotencyConflict, TenantScope, ValidationError

EMAIL_MATERIAL_PURPOSE = "email_draft_material"
_REF = re.compile(r"^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$")
_DRAFT_REF = re.compile(r"^DRF-[0-9A-HJKMNP-TV-Z]{26}$")
_EVIDENCE_REF = re.compile(r"^EVR-[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$")


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _wire_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"invalid {field}") from error
    return _aware(parsed, field)


def _positive_int(value: object, field: str, *, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValidationError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class TerminalMaterialAuthority:
    authority_receipt_ref: str
    site_id: str
    purpose: str
    draft_ref: str
    draft_revision: int
    material_kind: Literal["draft", "final_mime"]
    evidence_ref: str
    evidence_digest: str
    terminal_state: Literal["sent", "discarded"]
    terminal_at: datetime
    not_before: datetime
    source_authority_receipt_ref: str
    payload_digest: str

    def __post_init__(self) -> None:
        terminal_at = _aware(self.terminal_at, "terminal_at")
        not_before = _aware(self.not_before, "not_before")
        if (
            _REF.fullmatch(self.authority_receipt_ref) is None
            or _SITE.fullmatch(self.site_id) is None
            or self.purpose != EMAIL_MATERIAL_PURPOSE
            or _DRAFT_REF.fullmatch(self.draft_ref) is None
            or self.material_kind not in {"draft", "final_mime"}
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or self.terminal_state not in {"sent", "discarded"}
            or (self.terminal_state == "discarded" and self.material_kind != "draft")
            or not_before != terminal_at + timedelta(days=30)
            or _REF.fullmatch(self.source_authority_receipt_ref) is None
            or _DIGEST.fullmatch(self.payload_digest) is None
        ):
            raise ValidationError("invalid terminal material authority")
        _positive_int(self.draft_revision, "draft revision")

    def registration_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": self.site_id,
            "authority_receipt_ref": self.authority_receipt_ref,
        }

    def __repr__(self) -> str:
        return (
            "TerminalMaterialAuthority("
            f"site_id={self.site_id!r}, purpose={self.purpose!r}, "
            f"material_kind={self.material_kind!r}, terminal_state={self.terminal_state!r}, "
            f"draft_revision={self.draft_revision}, identifiers=<redacted>, digests=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class HumanDiscardAuthorityReceipt:
    """Closed receipt supplied by a future human-authority integration seam."""

    authority_receipt_ref: str
    site_id: str
    purpose: str
    draft_ref: str
    draft_revision: int
    evidence_ref: str
    evidence_digest: str
    terminal_at: datetime
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            _REF.fullmatch(self.authority_receipt_ref) is None
            or _SITE.fullmatch(self.site_id) is None
            or self.purpose != EMAIL_MATERIAL_PURPOSE
            or _DRAFT_REF.fullmatch(self.draft_ref) is None
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or _DIGEST.fullmatch(self.payload_digest) is None
        ):
            raise ValidationError("invalid human discard authority receipt")
        _positive_int(self.draft_revision, "draft revision")
        _aware(self.terminal_at, "terminal_at")

    def __repr__(self) -> str:
        return (
            "HumanDiscardAuthorityReceipt("
            f"site_id={self.site_id!r}, purpose={self.purpose!r}, "
            f"draft_revision={self.draft_revision}, identifiers=<redacted>, digests=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class TerminalAuthorityRegistrationLease:
    authority: TerminalMaterialAuthority
    registration_request_ref: str
    worker_id: str
    attempt: int
    lease_generation: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if (
            _REF.fullmatch(self.registration_request_ref) is None
            or not isinstance(self.worker_id, str)
            or not self.worker_id
            or len(self.worker_id) > 256
            or "@" in self.worker_id
        ):
            raise ValidationError("invalid terminal authority registration lease")
        _positive_int(self.attempt, "attempt", maximum=5)
        _positive_int(self.lease_generation, "lease generation")
        _aware(self.lease_expires_at, "lease_expires_at")

    def __repr__(self) -> str:
        return (
            "TerminalAuthorityRegistrationLease("
            f"site_id={self.authority.site_id!r}, material_kind={self.authority.material_kind!r}, "
            f"attempt={self.attempt}, lease_generation={self.lease_generation}, "
            "identifiers=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ObserverRegistrationReceipt:
    site_id: str
    evidence_ref: str
    request_ref: str
    not_before: datetime

    @classmethod
    def from_response(
        cls,
        value: object,
        *,
        lease: TerminalAuthorityRegistrationLease,
    ) -> ObserverRegistrationReceipt:
        fields = {"schema_version", "site_id", "evidence_ref", "request_ref", "not_before"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValidationError("invalid observer registration response")
        receipt = cls(
            site_id=str(value["site_id"]),
            evidence_ref=str(value["evidence_ref"]),
            request_ref=str(value["request_ref"]),
            not_before=_wire_datetime(value["not_before"], "registration not_before"),
        )
        authority = lease.authority
        if (
            value["schema_version"] != "1.0"
            or receipt.site_id != authority.site_id
            or not hmac.compare_digest(receipt.evidence_ref, authority.evidence_ref)
            or receipt.not_before != authority.not_before.astimezone(UTC)
        ):
            raise ValidationError("invalid observer registration response")
        return receipt

    def __post_init__(self) -> None:
        if (
            _SITE.fullmatch(self.site_id) is None
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or _REF.fullmatch(self.request_ref) is None
        ):
            raise ValidationError("invalid observer registration receipt")
        _aware(self.not_before, "not_before")

    def __repr__(self) -> str:
        return f"ObserverRegistrationReceipt(site_id={self.site_id!r}, identifiers=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class EmailMaterialTombstoneCallback:
    site_id: str
    purpose: str
    authority_receipt_ref: str
    evidence_ref: str
    observer_request_ref: str
    tombstone_receipt_ref: str
    deleted_at: datetime
    evidence_digest: str
    callback_payload_digest: str

    @classmethod
    def from_wire(cls, value: object) -> EmailMaterialTombstoneCallback:
        fields = {
            "schema_version",
            "site_id",
            "purpose",
            "authority_receipt_ref",
            "evidence_ref",
            "observer_request_ref",
            "tombstone_receipt_ref",
            "deleted_at",
            "evidence_digest",
            "callback_payload_digest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value.get("schema_version") != "1.0"
        ):
            raise ValidationError("invalid email material callback payload")
        return cls(
            site_id=str(value["site_id"]),
            purpose=str(value["purpose"]),
            authority_receipt_ref=str(value["authority_receipt_ref"]),
            evidence_ref=str(value["evidence_ref"]),
            observer_request_ref=str(value["observer_request_ref"]),
            tombstone_receipt_ref=str(value["tombstone_receipt_ref"]),
            deleted_at=_wire_datetime(value["deleted_at"], "deleted_at"),
            evidence_digest=str(value["evidence_digest"]),
            callback_payload_digest=str(value["callback_payload_digest"]),
        )

    def __post_init__(self) -> None:
        if (
            _SITE.fullmatch(self.site_id) is None
            or self.purpose != EMAIL_MATERIAL_PURPOSE
            or _REF.fullmatch(self.authority_receipt_ref) is None
            or _EVIDENCE_REF.fullmatch(self.evidence_ref) is None
            or _REF.fullmatch(self.observer_request_ref) is None
            or _REF.fullmatch(self.tombstone_receipt_ref) is None
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or _DIGEST.fullmatch(self.callback_payload_digest) is None
        ):
            raise ValidationError("invalid email material callback payload")
        _aware(self.deleted_at, "deleted_at")

    def __repr__(self) -> str:
        return (
            "EmailMaterialTombstoneCallback("
            f"site_id={self.site_id!r}, purpose={self.purpose!r}, "
            "identifiers=<redacted>, digests=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GatewayTombstoneCallbackReceipt:
    callback_receipt_ref: str
    site_id: str
    authority_receipt_ref: str
    tombstone_receipt_ref: str

    def __post_init__(self) -> None:
        if (
            _REF.fullmatch(self.callback_receipt_ref) is None
            or _SITE.fullmatch(self.site_id) is None
            or _REF.fullmatch(self.authority_receipt_ref) is None
            or _REF.fullmatch(self.tombstone_receipt_ref) is None
        ):
            raise ValidationError("invalid gateway callback receipt")

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "site_id": self.site_id,
            "authority_receipt_ref": self.authority_receipt_ref,
            "tombstone_receipt_ref": self.tombstone_receipt_ref,
            "callback_receipt_ref": self.callback_receipt_ref,
            "accepted": True,
        }

    def __repr__(self) -> str:
        return f"GatewayTombstoneCallbackReceipt(site_id={self.site_id!r}, identifiers=<redacted>)"


class TerminalRetentionRepository(Protocol):
    def create_sent_authorities(
        self, scope: TenantScope, *, provider_receipt_record_ref: str
    ) -> tuple[TerminalMaterialAuthority, ...]: ...

    def create_discard_authority(
        self, scope: TenantScope, *, receipt: HumanDiscardAuthorityReceipt
    ) -> TerminalMaterialAuthority: ...

    def resolve_terminal(
        self, scope: TenantScope, authority_receipt_ref: str
    ) -> TerminalMaterialAuthority: ...

    def claim_registration(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> TerminalAuthorityRegistrationLease | None: ...

    def heartbeat_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> datetime: ...

    def ack_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        receipt: ObserverRegistrationReceipt,
        now: datetime,
    ) -> ObserverRegistrationReceipt: ...

    def fail_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        safe_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None: ...

    def accept_tombstone_callback(
        self,
        scope: TenantScope,
        *,
        callback: EmailMaterialTombstoneCallback,
        now: datetime,
    ) -> GatewayTombstoneCallbackReceipt: ...


class EmailMaterialTerminalRetentionService:
    """Closed core commands for authority creation, registration, and callback writeback."""

    def __init__(
        self,
        *,
        repository: TerminalRetentionRepository,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValidationError("invalid terminal retention clock")
        self._repository = repository
        self._clock = clock

    def __repr__(self) -> str:
        return "EmailMaterialTerminalRetentionService(dependencies=<redacted>)"

    def record_provider_outcome(
        self,
        scope: TenantScope,
        *,
        provider_receipt_record_ref: str,
    ) -> tuple[TerminalMaterialAuthority, ...]:
        if _REF.fullmatch(provider_receipt_record_ref) is None:
            raise ValidationError("invalid provider receipt record ref")
        authorities = self._repository.create_sent_authorities(
            scope,
            provider_receipt_record_ref=provider_receipt_record_ref,
        )
        if not authorities:
            return ()
        if tuple(item.material_kind for item in authorities) != ("draft", "final_mime"):
            raise IdempotencyConflict("sent terminal authority set conflict")
        if any(
            item.site_id != scope.site_id
            or item.purpose != EMAIL_MATERIAL_PURPOSE
            or item.terminal_state != "sent"
            for item in authorities
        ):
            raise IdempotencyConflict("sent terminal authority scope conflict")
        if len({item.authority_receipt_ref for item in authorities}) != 2:
            raise IdempotencyConflict("sent terminal authority identity conflict")
        return authorities

    def discard(
        self,
        scope: TenantScope,
        *,
        receipt: HumanDiscardAuthorityReceipt,
    ) -> TerminalMaterialAuthority:
        if not isinstance(receipt, HumanDiscardAuthorityReceipt):
            raise TypeError("receipt must be a closed human discard authority receipt")
        if receipt.site_id != scope.site_id:
            raise ValidationError("discard authority scope mismatch")
        authority = self._repository.create_discard_authority(scope, receipt=receipt)
        if (
            authority.site_id != scope.site_id
            or authority.material_kind != "draft"
            or authority.terminal_state != "discarded"
            or authority.draft_ref != receipt.draft_ref
            or authority.draft_revision != receipt.draft_revision
            or authority.evidence_ref != receipt.evidence_ref
            or authority.evidence_digest != receipt.evidence_digest
        ):
            raise IdempotencyConflict("discard terminal authority conflict")
        return authority

    def resolve_terminal(
        self,
        scope: TenantScope,
        authority_receipt_ref: str,
    ) -> TerminalMaterialAuthority:
        if _REF.fullmatch(authority_receipt_ref) is None:
            raise ValidationError("invalid terminal authority receipt")
        authority = self._repository.resolve_terminal(scope, authority_receipt_ref)
        if authority.site_id != scope.site_id or not hmac.compare_digest(
            authority.authority_receipt_ref,
            authority_receipt_ref,
        ):
            raise ValidationError("terminal authority resolution conflict")
        return authority

    def claim_registration(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> TerminalAuthorityRegistrationLease | None:
        now = _aware(self._clock(), "clock")
        if (
            not worker_id
            or len(worker_id) > 256
            or "@" in worker_id
            or not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=5)
        ):
            raise ValidationError("invalid registration claim")
        lease = self._repository.claim_registration(
            scope,
            worker_id=worker_id,
            now=now,
            lease_until=now + lease_duration,
        )
        if lease is not None and (
            lease.authority.site_id != scope.site_id
            or lease.worker_id != worker_id
            or lease.lease_expires_at <= now
        ):
            raise IdempotencyConflict("registration lease conflict")
        return lease

    def heartbeat_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> datetime:
        now = _aware(self._clock(), "clock")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=5):
            raise ValidationError("invalid registration heartbeat")
        return self._repository.heartbeat_registration(
            scope,
            lease,
            now=now,
            lease_until=now + lease_duration,
        )

    def ack_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        response: object,
    ) -> ObserverRegistrationReceipt:
        receipt = ObserverRegistrationReceipt.from_response(response, lease=lease)
        return self._repository.ack_registration(
            scope,
            lease,
            receipt=receipt,
            now=_aware(self._clock(), "clock"),
        )

    def fail_registration(
        self,
        scope: TenantScope,
        lease: TerminalAuthorityRegistrationLease,
        *,
        safe_code: str,
    ) -> None:
        if not isinstance(safe_code, str) or _SAFE_CODE.fullmatch(safe_code) is None:
            raise ValidationError("invalid registration safe code")
        now = _aware(self._clock(), "clock")
        delay = timedelta(seconds=min(300, 2**lease.attempt))
        self._repository.fail_registration(
            scope,
            lease,
            safe_code=safe_code,
            next_attempt_at=now + delay,
            now=now,
        )

    def accept_tombstone_callback(
        self,
        scope: TenantScope,
        *,
        payload: object,
    ) -> GatewayTombstoneCallbackReceipt:
        callback = EmailMaterialTombstoneCallback.from_wire(payload)
        if callback.site_id != scope.site_id:
            raise ValidationError("email material callback scope mismatch")
        return self._repository.accept_tombstone_callback(
            scope,
            callback=callback,
            now=_aware(self._clock(), "clock"),
        )


def terminal_authority_from_row(row: tuple[object, ...]) -> TerminalMaterialAuthority:
    if len(row) != 13:
        raise ValidationError("invalid terminal material authority row")
    return TerminalMaterialAuthority(
        authority_receipt_ref=str(row[0]),
        site_id=str(row[1]),
        purpose=str(row[2]),
        draft_ref=str(row[3]),
        draft_revision=_positive_int(row[4], "draft revision"),
        material_kind=cast(Literal["draft", "final_mime"], str(row[5])),
        evidence_ref=str(row[6]),
        evidence_digest=str(row[7]),
        terminal_state=cast(Literal["sent", "discarded"], str(row[8])),
        terminal_at=cast(datetime, row[9]),
        not_before=cast(datetime, row[10]),
        source_authority_receipt_ref=str(row[11]),
        payload_digest=str(row[12]),
    )


__all__ = [
    "EMAIL_MATERIAL_PURPOSE",
    "EmailMaterialTerminalRetentionService",
    "EmailMaterialTombstoneCallback",
    "GatewayTombstoneCallbackReceipt",
    "HumanDiscardAuthorityReceipt",
    "ObserverRegistrationReceipt",
    "TerminalAuthorityRegistrationLease",
    "TerminalMaterialAuthority",
    "TerminalRetentionRepository",
    "terminal_authority_from_row",
]
