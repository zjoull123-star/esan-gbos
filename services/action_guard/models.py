from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class EvaluationPhase(StrEnum):
    PRE_TOOL = "pre_tool"
    POST_RESULT = "post_result"


class GuardOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_HUMAN = "require_human"
    DENY = "deny"


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionRequest:
    request_id: str
    site_id: str
    processing_purpose: str
    action_type: str
    requested_by: str
    target_ref: str
    target_revision: int
    evidence_refs: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    correlation_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.target_revision < 0:
            raise ValueError("target_revision must be non-negative")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        if len(self.granted_scopes) != len(set(self.granted_scopes)):
            raise ValueError("granted_scopes must be unique")
        detached = deepcopy(dict(self.payload))
        object.__setattr__(self, "payload", MappingProxyType(detached))


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardDecision:
    guard_decision_id: str
    request_id: str
    site_id: str
    processing_purpose: str
    action_type: str
    target_revision: int
    evaluation_phase: EvaluationPhase
    outcome: GuardOutcome
    policy_version: str
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    evaluated_at: datetime
    correlation_id: str


@dataclass(frozen=True, slots=True)
class VerifiedEmailSendCommand:
    """Minimal capability produced only after specialized email-send verification."""

    command_ref: str
    idempotency_key: str
    stable_client_request_id: str
    payload_digest: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class VerifiedEmailSendOutboxReceipt:
    """Closed receipt proving only the immutable Send Outbox transaction."""

    command_receipt_ref: str
    send_outbox_ref: str
    payload_digest: str
