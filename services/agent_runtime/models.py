from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ValidationError(ValueError):
    """An Agent Task request or state transition is invalid."""


class IdempotencyConflict(ValidationError):
    """A site-local idempotency key was reused for different task input."""


class LeaseConflict(ValidationError):
    """A worker attempted a transition without owning a live task lease."""


class TaskNotFound(ValidationError):
    """The site-scoped task does not exist."""


def canonical_payload_digest(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("payload must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return deepcopy(value)


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return deepcopy(value)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RECHECK = "recheck"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class FailureClassification(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_DENIED = "policy_denied"
    TOOL_FAILURE = "tool_failure"
    INVALID_OUTPUT = "invalid_output"
    DEPENDENCY = "dependency"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentTaskSubmission:
    task_id: str
    site_id: str
    processing_purpose: str
    idempotency_key: str
    agent_type: str
    subject_type: str
    subject_ref: str
    due_at: datetime
    priority: int
    max_attempts: int
    causation_id: str
    correlation_id: str
    payload: Mapping[str, Any]
    parent_task_id: str | None = None
    payload_digest: str = field(init=False)

    def __post_init__(self) -> None:
        required = {
            "task_id": self.task_id,
            "site_id": self.site_id,
            "processing_purpose": self.processing_purpose,
            "idempotency_key": self.idempotency_key,
            "agent_type": self.agent_type,
            "subject_type": self.subject_type,
            "subject_ref": self.subject_ref,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }
        for name, value in required.items():
            if not value:
                raise ValidationError(f"{name} is required")
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValidationError("due_at must be timezone-aware")
        if not 0 <= self.priority <= 100:
            raise ValidationError("priority must be between 0 and 100")
        if not 1 <= self.max_attempts <= 100:
            raise ValidationError("max_attempts must be between 1 and 100")
        if self.parent_task_id == self.task_id:
            raise ValidationError("parent_task_id cannot reference the task itself")
        detached_payload = deepcopy(dict(self.payload))
        canonical_payload_digest(detached_payload)
        object.__setattr__(
            self,
            "payload",
            freeze_json(detached_payload),
        )
        idempotency_document = {
            "digest_version": "gate4-agent-task-v1",
            "site_id": self.site_id,
            "processing_purpose": self.processing_purpose,
            "idempotency_key": self.idempotency_key,
            "agent_type": self.agent_type,
            "subject_type": self.subject_type,
            "subject_ref": self.subject_ref,
            "due_at": self.due_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "priority": self.priority,
            "max_attempts": self.max_attempts,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "parent_task_id": self.parent_task_id,
            "payload": detached_payload,
        }
        object.__setattr__(
            self,
            "payload_digest",
            canonical_payload_digest(idempotency_document),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentTaskMetadata:
    task_id: str
    site_id: str
    processing_purpose: str
    idempotency_key: str
    payload_digest: str
    agent_type: str
    subject_type: str
    subject_ref: str
    status: TaskStatus
    due_at: datetime
    priority: int
    attempt: int
    max_attempts: int
    causation_id: str
    correlation_id: str
    parent_task_id: str | None
    output_artifact_refs: tuple[str, ...]
    failure_classification: FailureClassification | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineEventMetadata:
    task_id: str
    site_id: str
    sequence: int
    event_type: str
    occurred_at: datetime
    actor_type: str
    actor_ref: str | None
    causation_id: str
    correlation_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeadLetterMetadata:
    task_id: str
    site_id: str
    attempts: int
    failure_classification: FailureClassification
    reason_code: str
    dead_lettered_at: datetime
    causation_id: str
    correlation_id: str
