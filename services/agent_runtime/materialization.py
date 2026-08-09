"""Trusted, idempotent delivery of internal AI Draft intents to Frappe."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from .models import (
    IdempotencyConflict,
    LeaseConflict,
    ValidationError,
    canonical_payload_digest,
    thaw_json,
)
from .proposals import (
    MaterializationContext,
    MaterializationEnvelope,
    MaterializationIntent,
    TrustedMaterializer,
)

_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True, repr=False)
class MaterializationClaim:
    materialization_id: str
    proposal_id: str
    site_id: str
    attempt: int
    max_attempts: int
    lease_owner: str
    lease_expires_at: datetime
    envelope: MaterializationEnvelope = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("materialization_id", "proposal_id", "site_id"):
            value = getattr(self, name)
            if not value or len(value) > 256:
                raise ValidationError(f"{name} is invalid")
        if self.attempt < 0 or self.attempt > self.max_attempts or self.max_attempts < 1:
            raise ValidationError("materialization attempt is invalid")
        if self.attempt > 0 and not self.lease_owner:
            raise ValidationError("claimed materialization requires a lease owner")
        if self.lease_expires_at.tzinfo is None or self.lease_expires_at.utcoffset() is None:
            raise ValidationError("materialization lease expiry must be timezone-aware")

    def __repr__(self) -> str:
        return (
            "MaterializationClaim("
            f"materialization_id={self.materialization_id!r}, "
            f"proposal_id={self.proposal_id!r}, site_id={self.site_id!r}, "
            f"attempt={self.attempt}, max_attempts={self.max_attempts}, "
            f"lease_owner={self.lease_owner!r}, "
            f"lease_expires_at={self.lease_expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class FrappeDraftReceipt:
    doctype: str
    name: str
    revision: int
    request_id: str
    request_digest: str

    def __post_init__(self) -> None:
        for field_name in ("doctype", "name", "request_id"):
            value = getattr(self, field_name)
            if not value or len(value) > 256:
                raise ValidationError(f"receipt {field_name} is invalid")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 0
        ):
            raise ValidationError("receipt revision must be a non-negative integer")
        if _DIGEST_PATTERN.fullmatch(self.request_digest) is None:
            raise ValidationError("receipt request_digest must be SHA-256")


class FrappeDraftClient(Protocol):
    """Idempotent Frappe boundary; implementations key writes by request_id."""

    def apply(
        self,
        intent: MaterializationIntent,
        *,
        request_id: str,
        request_digest: str,
        processing_purpose: str | None = None,
    ) -> FrappeDraftReceipt: ...


@dataclass(frozen=True, slots=True, repr=False)
class MaterializationContextRequest:
    """Closed metadata used to resolve controlled Frappe scope, never provider output."""

    site_id: str
    task_id: str
    processing_purpose: str
    proposal_id: str
    subject_type: str
    subject_ref: str
    subject_revision: int

    def __post_init__(self) -> None:
        for field_name, maximum in (
            ("site_id", 140),
            ("task_id", 256),
            ("processing_purpose", 80),
            ("proposal_id", 256),
            ("subject_type", 140),
            ("subject_ref", 256),
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValidationError(f"materialization context {field_name} is invalid")
        if (
            not isinstance(self.subject_revision, int)
            or isinstance(self.subject_revision, bool)
            or self.subject_revision < 0
        ):
            raise ValidationError("materialization context subject_revision is invalid")

    def __repr__(self) -> str:
        return (
            "MaterializationContextRequest("
            f"site_id={self.site_id!r}, task_id={self.task_id!r}, "
            f"processing_purpose={self.processing_purpose!r}, "
            f"proposal_id={self.proposal_id!r}, subject_type={self.subject_type!r}, "
            f"subject_ref=<redacted>, subject_revision={self.subject_revision})"
        )


class MaterializationContextResolver(Protocol):
    """Maps trusted task scope metadata to the Frappe team/review context."""

    def resolve(
        self,
        request: MaterializationContextRequest,
    ) -> MaterializationContext | None: ...


class MaterializationRepository(Protocol):
    def claim_materialization(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MaterializationClaim | None: ...

    def acknowledge_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        receipt: FrappeDraftReceipt,
    ) -> FrappeDraftReceipt: ...

    def heartbeat_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> None: ...

    def fail_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"] | None: ...


@dataclass(frozen=True, slots=True)
class MaterializationRunResult:
    status: Literal["idle", "succeeded", "retry", "dead_letter", "lease_lost"]
    materialization_id: str | None
    attempt: int | None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializationHealth:
    pending: int
    running: int
    retry: int
    dead_letter: int

    @property
    def ready(self) -> bool:
        return self.dead_letter == 0

    def to_wire(self) -> dict[str, int | bool]:
        return {
            "ready": self.ready,
            "pending": self.pending,
            "running": self.running,
            "retry": self.retry,
            "dead_letter": self.dead_letter,
        }


class MaterializationWorker:
    """Leased worker whose only side effect is an injected Frappe draft client."""

    __slots__ = (
        "_client",
        "_clock",
        "_lease_duration",
        "_materializer",
        "_context_resolver",
        "_repository",
        "_retry_delay",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        repository: MaterializationRepository,
        client: FrappeDraftClient,
        materializer: TrustedMaterializer,
        context_resolver: MaterializationContextResolver,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if lease_duration <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("materialization durations must be positive")
        self._repository = repository
        self._client = client
        self._materializer = materializer
        self._context_resolver = context_resolver
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay

    def __repr__(self) -> str:
        return (
            "MaterializationWorker("
            f"worker_id={self._worker_id!r}, "
            "repository=<redacted>, client=<redacted>)"
        )

    def run_once(self, site_id: str) -> MaterializationRunResult:
        now = self._clock()
        claim = self._repository.claim_materialization(
            site_id,
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return MaterializationRunResult(
                status="idle",
                materialization_id=None,
                attempt=None,
            )
        try:
            self._repository.heartbeat_materialization(
                claim.site_id,
                claim.materialization_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                now=self._clock(),
                lease_duration=self._lease_duration,
            )
            context = None
            if claim.envelope.action_type != "internal.work_item.transition.propose":
                context = self._context_resolver.resolve(
                    MaterializationContextRequest(
                        site_id=claim.site_id,
                        task_id=claim.envelope.task_id,
                        processing_purpose=claim.envelope.processing_purpose,
                        proposal_id=claim.proposal_id,
                        subject_type=claim.envelope.subject_type,
                        subject_ref=claim.envelope.subject_ref,
                        subject_revision=claim.envelope.subject_revision,
                    )
                )
                if context is None:
                    raise ValidationError("controlled materialization team context is unavailable")
            intent = self._materializer.materialize(
                claim.envelope,
                context=context,
            )
            request_digest = _intent_digest(intent)
            self._repository.heartbeat_materialization(
                claim.site_id,
                claim.materialization_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                now=self._clock(),
                lease_duration=self._lease_duration,
            )
            receipt = self._client.apply(
                intent,
                request_id=claim.materialization_id,
                request_digest=request_digest,
                processing_purpose=claim.envelope.processing_purpose,
            )
            _validate_receipt(
                receipt,
                claim=claim,
                intent=intent,
                request_digest=request_digest,
            )
            self._repository.acknowledge_materialization(
                claim.site_id,
                claim.materialization_id,
                worker_id=self._worker_id,
                expected_attempt=claim.attempt,
                now=self._clock(),
                receipt=receipt,
            )
        except LeaseConflict:
            return MaterializationRunResult(
                status="lease_lost",
                materialization_id=claim.materialization_id,
                attempt=claim.attempt,
                error_code="lease_lost",
            )
        except Exception as exc:
            error_code = (
                "frappe_body_conflict"
                if isinstance(exc, IdempotencyConflict)
                else "materialization_failed"
            )
            try:
                failure_state = self._repository.fail_materialization(
                    claim.site_id,
                    claim.materialization_id,
                    worker_id=self._worker_id,
                    expected_attempt=claim.attempt,
                    now=self._clock(),
                    retry_at=self._clock() + self._retry_delay,
                    error_code=error_code,
                )
            except LeaseConflict:
                return MaterializationRunResult(
                    status="lease_lost",
                    materialization_id=claim.materialization_id,
                    attempt=claim.attempt,
                    error_code="lease_lost",
                )
            result_status: Literal["retry", "dead_letter"] = (
                "dead_letter" if failure_state == "dead_letter" else "retry"
            )
            return MaterializationRunResult(
                status=result_status,
                materialization_id=claim.materialization_id,
                attempt=claim.attempt,
                error_code=error_code,
            )
        return MaterializationRunResult(
            status="succeeded",
            materialization_id=claim.materialization_id,
            attempt=claim.attempt,
        )


def _intent_digest(intent: MaterializationIntent) -> str:
    values = thaw_json(intent.values)
    if not isinstance(values, dict):
        raise ValidationError("materialization intent values must be an object")
    return canonical_payload_digest(
        {
            "operation": intent.operation,
            "doctype": intent.doctype,
            "values": values,
        }
    )


def _validate_receipt(
    receipt: FrappeDraftReceipt,
    *,
    claim: MaterializationClaim,
    intent: MaterializationIntent,
    request_digest: str,
) -> None:
    if (
        receipt.request_id != claim.materialization_id
        or receipt.request_digest != request_digest
        or receipt.doctype != intent.doctype
    ):
        raise IdempotencyConflict("Frappe receipt does not match materialization request")


__all__ = [
    "FrappeDraftClient",
    "FrappeDraftReceipt",
    "MaterializationClaim",
    "MaterializationContextRequest",
    "MaterializationContextResolver",
    "MaterializationHealth",
    "MaterializationRepository",
    "MaterializationRunResult",
    "MaterializationWorker",
]
