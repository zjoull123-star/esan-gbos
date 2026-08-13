"""Provider-neutral outbound protocol with no concrete network implementation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProviderOutcome(StrEnum):
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    PERMANENTLY_REJECTED = "permanently_rejected"
    UNCERTAIN = "uncertain"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"


class ProviderSubmissionUncertain(RuntimeError):
    """The provider may have accepted the request; blind retry is forbidden."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSubmission:
    stable_provider_request_id: str
    send_outbox_ref: str
    final_mime_evidence_ref: str
    final_mime_digest: str
    participant_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSubmissionResult:
    outcome: ProviderOutcome
    safe_code: str
    provider_receipt_ref: str | None

    def __post_init__(self) -> None:
        if not self.safe_code or "@" in self.safe_code or len(self.safe_code) > 80:
            raise ValueError("invalid provider safe code")
        if self.outcome is ProviderOutcome.NOT_SUBMITTED and self.provider_receipt_ref is not None:
            raise ValueError("non-submission cannot have a provider receipt")


class EmailProvider(Protocol):
    def submit(self, submission: ProviderSubmission) -> ProviderSubmissionResult: ...

    def lookup(self, stable_provider_request_id: str) -> ProviderSubmissionResult: ...


__all__ = [
    "EmailProvider",
    "ProviderOutcome",
    "ProviderSubmission",
    "ProviderSubmissionResult",
    "ProviderSubmissionUncertain",
]
