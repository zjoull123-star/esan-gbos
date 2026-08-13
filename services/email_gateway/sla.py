from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .models import AuthorizationError, RevisionConflict, ValidationError


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class MailboxSlaPolicy:
    policy_ref: str
    revision: int
    first_response_duration_seconds: int
    effective_at: datetime

    def __post_init__(self) -> None:
        if not self.policy_ref or self.policy_ref != self.policy_ref.strip():
            raise ValidationError("invalid SLA policy ref")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValidationError("invalid SLA policy revision")
        duration = self.first_response_duration_seconds
        if (
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 60 <= duration <= 604800
        ):
            raise ValidationError("invalid first response duration")
        _aware(self.effective_at, "SLA policy effective_at")


@dataclass(frozen=True, slots=True)
class SlaClock:
    inbox_item_ref: str
    policy_ref: str
    policy_revision: int
    started_at: datetime | None
    due_at: datetime | None
    status: str
    completed_at: datetime | None = None
    provider_accepted_receipt_ref: str | None = None
    closed_at: datetime | None = None
    closed_outcome: str | None = None
    audit_revision: int = 1

    @classmethod
    def start(
        cls,
        *,
        inbox_item_ref: str,
        received_at: datetime,
        policy: MailboxSlaPolicy,
        quarantined: bool,
    ) -> SlaClock:
        _aware(received_at, "Observer received_at")
        if policy.effective_at > received_at:
            raise ValidationError("SLA policy is not effective at received_at")
        if quarantined:
            return cls(
                inbox_item_ref,
                policy.policy_ref,
                policy.revision,
                None,
                None,
                "not_applicable",
            )
        return cls(
            inbox_item_ref,
            policy.policy_ref,
            policy.revision,
            received_at,
            received_at + timedelta(seconds=policy.first_response_duration_seconds),
            "running",
        )

    def preserve_for_revision(self, inbox_revision: int, *, now: datetime) -> SlaClock:
        if isinstance(inbox_revision, bool) or inbox_revision < 1:
            raise ValidationError("invalid inbox revision")
        self._require_no_regression(now)
        return self

    def complete(
        self,
        *,
        accepted_at: datetime,
        provider_accepted: bool,
        receipt_ref: str,
        policy_revision: int,
    ) -> SlaClock:
        self._require_policy(policy_revision)
        if self.status == "not_applicable":
            raise AuthorizationError("quarantined SLA cannot complete")
        if self.completed_at is not None:
            if not provider_accepted or not receipt_ref:
                raise AuthorizationError("provider-accepted outbound receipt required")
            self._require_no_regression(accepted_at)
            return self
        if not provider_accepted or not receipt_ref:
            raise AuthorizationError("provider-accepted outbound receipt required")
        self._require_no_regression(accepted_at)
        if self.due_at is None:  # pragma: no cover - protected by start
            raise ValidationError("SLA due time missing")
        return replace(
            self,
            status="met" if accepted_at <= self.due_at else "overdue",
            completed_at=accepted_at,
            provider_accepted_receipt_ref=receipt_ref,
            audit_revision=self.audit_revision + 1,
        )

    def close(self, closed_at: datetime, *, policy_revision: int) -> SlaClock:
        self._require_policy(policy_revision)
        if self.status == "not_applicable":
            return self
        self._require_no_regression(closed_at)
        if self.completed_at is not None:
            outcome = "met" if self.status == "met" else "overdue"
        else:
            if self.due_at is None:  # pragma: no cover - protected by start
                raise ValidationError("SLA due time missing")
            outcome = "met" if closed_at <= self.due_at else "overdue"
        return replace(
            self,
            status=f"closed_{outcome}",
            closed_at=closed_at,
            closed_outcome=outcome,
            audit_revision=self.audit_revision + 1,
        )

    def reopen(self, reopened_at: datetime, *, policy_revision: int) -> SlaClock:
        self._require_policy(policy_revision)
        if self.closed_at is None or self.closed_outcome is None:
            raise ValidationError("only a closed SLA may reopen")
        self._require_no_regression(reopened_at)
        if self.completed_at is not None:
            if self.due_at is None:  # pragma: no cover - protected by start
                raise ValidationError("SLA due time missing")
            status = "met" if self.completed_at <= self.due_at else "overdue"
        else:
            status = (
                "overdue" if self.due_at is not None and reopened_at > self.due_at else "running"
            )
        return replace(
            self,
            status=status,
            closed_at=None,
            closed_outcome=None,
            audit_revision=self.audit_revision + 1,
        )

    def _require_policy(self, policy_revision: int) -> None:
        if policy_revision != self.policy_revision:
            raise RevisionConflict("SLA policy revision drift")

    def _require_no_regression(self, value: datetime) -> None:
        _aware(value, "SLA event time")
        floor = max(
            (
                event_at
                for event_at in (self.started_at, self.completed_at, self.closed_at)
                if event_at is not None
            ),
            default=None,
        )
        if floor is not None and value < floor:
            raise ValidationError("SLA clock regression")
