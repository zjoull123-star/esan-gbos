from __future__ import annotations

import hashlib
import json
import re
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


_DIRECT_PII_PATTERN = re.compile(
    r"(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})|"
    r"(?:\+\d[\d ()-]{7,}\d)|"
    r"(?:\b\d{3}[\s()]\d[\d ()-]{5,}\d\b)|"
    r"(?:\b1[3-9]\d{9}\b)",
    re.IGNORECASE,
)
_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_LOCAL_PILOT_ROOT_KEYS = {
    "schema_version",
    "evidence_refs",
    "fact_version_refs",
    "subject",
    "request",
}
_LOCAL_PILOT_REQUEST_KEYS = {
    "requested_by",
    "decision_ref",
    "expected_action_type",
    "candidate_refs",
}


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


@dataclass(frozen=True, slots=True)
class LocalPilotFactVersionRef:
    fact_id: str
    fact_version: int

    def __post_init__(self) -> None:
        _require_ref(self.fact_id, "fact_id")
        if (
            not isinstance(self.fact_version, int)
            or isinstance(self.fact_version, bool)
            or self.fact_version < 1
        ):
            raise ValidationError("fact_version must be a positive integer")


@dataclass(frozen=True, slots=True, repr=False)
class LocalPilotTaskPayload:
    """Closed refs-only task input; resolved message content never enters the queue."""

    evidence_refs: tuple[str, ...]
    fact_version_refs: tuple[LocalPilotFactVersionRef, ...]
    subject_revision: int
    requested_by: str
    decision_ref: str
    expected_action_type: str
    candidate_refs: tuple[str, ...] = ()
    schema_version: str = "local-pilot-agent-task-v1"

    def __post_init__(self) -> None:
        if self.schema_version != "local-pilot-agent-task-v1":
            raise ValidationError("unsupported local-pilot task payload schema")
        if (
            not isinstance(self.subject_revision, int)
            or isinstance(self.subject_revision, bool)
            or self.subject_revision < 0
        ):
            raise ValidationError("subject revision must be a non-negative integer")
        _require_unique_refs(self.evidence_refs, "evidence_refs", required=True)
        if not self.fact_version_refs or len(self.fact_version_refs) != len(
            set(self.fact_version_refs)
        ):
            raise ValidationError("fact_version_refs must be non-empty and unique")
        _require_ref(self.requested_by, "requested_by")
        _require_ref(self.decision_ref, "decision_ref")
        _require_ref(self.expected_action_type, "expected_action_type")
        _require_unique_refs(self.candidate_refs, "candidate_refs", required=False)

    def __repr__(self) -> str:
        return "<LocalPilotTaskPayload redacted>"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LocalPilotTaskPayload:
        detached = thaw_json(freeze_json(value))
        if not isinstance(detached, dict) or set(detached) != _LOCAL_PILOT_ROOT_KEYS:
            raise ValidationError("local-pilot task payload must use the closed refs-only schema")
        if _contains_direct_pii(detached):
            raise ValidationError("local-pilot task payload cannot contain direct PII")
        subject = detached.get("subject")
        request = detached.get("request")
        fact_refs = detached.get("fact_version_refs")
        evidence_refs = detached.get("evidence_refs")
        if not isinstance(subject, dict) or set(subject) != {"revision"}:
            raise ValidationError("subject metadata must contain only revision")
        if not isinstance(request, dict) or set(request) != _LOCAL_PILOT_REQUEST_KEYS:
            raise ValidationError("request metadata must use the closed refs-only schema")
        if not isinstance(evidence_refs, list) or not all(
            isinstance(item, str) for item in evidence_refs
        ):
            raise ValidationError("evidence_refs must be a list of references")
        if not isinstance(fact_refs, list):
            raise ValidationError("fact_version_refs must be a list")
        parsed_fact_refs: list[LocalPilotFactVersionRef] = []
        for item in fact_refs:
            if not isinstance(item, dict) or set(item) != {"fact_id", "fact_version"}:
                raise ValidationError("fact version metadata is invalid")
            fact_id = item["fact_id"]
            fact_version = item["fact_version"]
            if not isinstance(fact_id, str) or not isinstance(fact_version, int):
                raise ValidationError("fact version metadata is invalid")
            parsed_fact_refs.append(LocalPilotFactVersionRef(fact_id, fact_version))
        candidate_refs = request["candidate_refs"]
        if not isinstance(candidate_refs, list) or not all(
            isinstance(item, str) for item in candidate_refs
        ):
            raise ValidationError("candidate_refs must be a list of references")
        requested_by = request["requested_by"]
        decision_ref = request["decision_ref"]
        expected_action_type = request["expected_action_type"]
        subject_revision = subject["revision"]
        if not all(
            isinstance(item, str) for item in (requested_by, decision_ref, expected_action_type)
        ) or not isinstance(subject_revision, int):
            raise ValidationError("local-pilot request metadata is invalid")
        return cls(
            schema_version=str(detached["schema_version"]),
            evidence_refs=tuple(evidence_refs),
            fact_version_refs=tuple(parsed_fact_refs),
            subject_revision=subject_revision,
            requested_by=requested_by,
            decision_ref=decision_ref,
            expected_action_type=expected_action_type,
            candidate_refs=tuple(candidate_refs),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_refs": list(self.evidence_refs),
            "fact_version_refs": [
                {"fact_id": item.fact_id, "fact_version": item.fact_version}
                for item in self.fact_version_refs
            ],
            "subject": {"revision": self.subject_revision},
            "request": {
                "requested_by": self.requested_by,
                "decision_ref": self.decision_ref,
                "expected_action_type": self.expected_action_type,
                "candidate_refs": list(self.candidate_refs),
            },
        }


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
    payload: Mapping[str, Any] = field(repr=False)
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


@dataclass(frozen=True, slots=True)
class AgentTaskClaim:
    metadata: AgentTaskMetadata
    payload: LocalPilotTaskPayload = field(repr=False)


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


def _require_ref(value: str, name: str) -> None:
    if _REFERENCE_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} must be an opaque reference")
    if _DIRECT_PII_PATTERN.search(value):
        raise ValidationError(f"{name} cannot contain direct PII")


def _require_unique_refs(
    values: tuple[str, ...],
    name: str,
    *,
    required: bool,
) -> None:
    if (required and not values) or len(values) != len(set(values)):
        raise ValidationError(f"{name} must be {'non-empty and ' if required else ''}unique")
    for value in values:
        _require_ref(value, name)


def _contains_direct_pii(value: Any) -> bool:
    if isinstance(value, Mapping):
        forbidden_keys = {
            "raw_context",
            "message_body",
            "message_text",
            "email",
            "phone",
            "telephone",
            "prompt",
            "response",
            "tokenized_context",
        }
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            if normalized in forbidden_keys or _contains_direct_pii(nested):
                return True
        return False
    if isinstance(value, list | tuple):
        return any(_contains_direct_pii(item) for item in value)
    return isinstance(value, str) and _DIRECT_PII_PATTERN.search(value) is not None
