from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .invocations import ModelInvocationRecord
from .models import (
    AgentTaskClaim,
    AgentTaskMetadata,
    AgentTaskSubmission,
    DeadLetterMetadata,
    FailureClassification,
    IdempotencyConflict,
    LeaseConflict,
    LocalPilotTaskPayload,
    TaskNotFound,
    TaskStatus,
    TimelineEventMetadata,
    ValidationError,
)
from .proposals import ActionProposalRecord, MaterializationOutboxRecord

if TYPE_CHECKING:
    from .agents import AgentExecutionResult


@runtime_checkable
class AgentTaskRepository(Protocol):
    def enqueue(
        self,
        submission: AgentTaskSubmission,
        *,
        now: datetime,
    ) -> AgentTaskMetadata: ...

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata | None: ...

    def claim_for_execution(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskClaim | None: ...

    def start(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
    ) -> AgentTaskMetadata: ...

    def heartbeat(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata: ...

    def succeed(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        output_artifact_refs: tuple[str, ...] = (),
    ) -> AgentTaskMetadata: ...

    def fail(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        classification: FailureClassification,
    ) -> AgentTaskMetadata: ...

    def complete_with_proposal(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        result: AgentExecutionResult,
    ) -> AgentTaskMetadata: ...

    def get(self, site_id: str, task_id: str) -> AgentTaskMetadata | None: ...

    def timeline(self, site_id: str, task_id: str) -> tuple[TimelineEventMetadata, ...]: ...

    def dead_letter(self, site_id: str, task_id: str) -> DeadLetterMetadata | None: ...


class InMemoryAgentTaskRepository:
    """Deterministic process-local Agent Task repository for unit tests."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], AgentTaskMetadata] = {}
        self._payloads: dict[tuple[str, str], object] = {}
        self._timeline: dict[tuple[str, str], list[TimelineEventMetadata]] = {}
        self._idempotency: dict[tuple[str, str], AgentTaskMetadata] = {}
        self._dead_letters: dict[tuple[str, str], DeadLetterMetadata] = {}
        self._proposals: dict[tuple[str, str, int], ActionProposalRecord] = {}
        self._proposal_idempotency: dict[tuple[str, str], ActionProposalRecord] = {}
        self._materializations: dict[tuple[str, str, int], MaterializationOutboxRecord] = {}
        self._invocations: dict[tuple[str, str], ModelInvocationRecord] = {}
        self._invocation_idempotency: dict[tuple[str, str], ModelInvocationRecord] = {}
        self._lock = RLock()

    def enqueue(
        self,
        submission: AgentTaskSubmission,
        *,
        now: datetime,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        key = (submission.site_id, submission.task_id)
        idempotency_key = (submission.site_id, submission.idempotency_key)
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                if existing.payload_digest != submission.payload_digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different submission"
                    )
                return replace(existing)
            if (
                submission.parent_task_id is not None
                and (
                    submission.site_id,
                    submission.parent_task_id,
                )
                not in self._tasks
            ):
                raise ValidationError("parent task does not exist in this site")
            if key in self._tasks:
                raise ValidationError("task_id already exists with another request")
            task = AgentTaskMetadata(
                task_id=submission.task_id,
                site_id=submission.site_id,
                processing_purpose=submission.processing_purpose,
                idempotency_key=submission.idempotency_key,
                payload_digest=submission.payload_digest,
                agent_type=submission.agent_type,
                subject_type=submission.subject_type,
                subject_ref=submission.subject_ref,
                status=TaskStatus.QUEUED,
                due_at=submission.due_at,
                priority=submission.priority,
                attempt=0,
                max_attempts=submission.max_attempts,
                causation_id=submission.causation_id,
                correlation_id=submission.correlation_id,
                parent_task_id=submission.parent_task_id,
                output_artifact_refs=(),
                failure_classification=None,
                lease_owner=None,
                lease_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            self._tasks[key] = task
            self._payloads[key] = submission.payload
            self._idempotency[idempotency_key] = task
            self._timeline[key] = [
                TimelineEventMetadata(
                    task_id=submission.task_id,
                    site_id=submission.site_id,
                    sequence=0,
                    event_type="created",
                    occurred_at=now,
                    actor_type="system",
                    actor_ref=None,
                    causation_id=submission.causation_id,
                    correlation_id=submission.correlation_id,
                )
            ]
            return replace(task)

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata | None:
        _require_aware(now, "now")
        if not worker_id:
            raise ValidationError("worker_id is required")
        if lease_duration <= timedelta(0):
            raise ValidationError("lease_duration must be positive")
        with self._lock:
            exhausted = [
                task
                for task in self._tasks.values()
                if task.site_id == site_id
                and task.status in {TaskStatus.LEASED, TaskStatus.RUNNING}
                and task.lease_expires_at is not None
                and task.lease_expires_at <= now
                and task.attempt >= task.max_attempts
            ]
            for task in sorted(exhausted, key=lambda item: item.task_id):
                self._move_to_dead_letter(
                    task,
                    now=now,
                    classification=FailureClassification.INTERNAL,
                    reason_code="lease_expired_max_attempts",
                    actor_ref=None,
                )
            candidates = [
                task
                for task in self._tasks.values()
                if task.site_id == site_id
                and (
                    (task.status in {TaskStatus.QUEUED, TaskStatus.RECHECK} and task.due_at <= now)
                    or (
                        task.status in {TaskStatus.LEASED, TaskStatus.RUNNING}
                        and task.lease_expires_at is not None
                        and task.lease_expires_at <= now
                    )
                )
            ]
            if not candidates:
                return None
            current = min(
                candidates,
                key=lambda task: (
                    -task.priority,
                    task.due_at,
                    task.created_at,
                    task.task_id,
                ),
            )
            claimed = replace(
                current,
                status=TaskStatus.LEASED,
                attempt=current.attempt + 1,
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
                updated_at=now,
            )
            self._store_task(claimed)
            self._append_event(
                claimed,
                event_type="leased",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return replace(claimed)

    def claim_for_execution(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskClaim | None:
        _require_aware(now, "now")
        if not worker_id:
            raise ValidationError("worker_id is required")
        if lease_duration <= timedelta(0):
            raise ValidationError("lease_duration must be positive")
        with self._lock:
            self._reap_expired(site_id=site_id, now=now)
            candidates = self._claim_candidates(site_id=site_id, now=now)
            if not candidates:
                return None
            current = min(
                candidates,
                key=lambda task: (
                    -task.priority,
                    task.due_at,
                    task.created_at,
                    task.task_id,
                ),
            )
            payload_value = self._payloads[(current.site_id, current.task_id)]
            if not isinstance(payload_value, Mapping):
                raise ValidationError("task payload is unavailable")
            payload = LocalPilotTaskPayload.from_mapping(payload_value)
            running = replace(
                current,
                status=TaskStatus.RUNNING,
                attempt=current.attempt + 1,
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
                updated_at=now,
            )
            self._store_task(running)
            self._append_event(
                running,
                event_type="leased",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            self._append_event(
                running,
                event_type="running",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return AgentTaskClaim(metadata=replace(running), payload=payload)

    def start(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        with self._lock:
            current = self._require_live_lease(
                site_id,
                task_id,
                worker_id,
                expected_attempt,
                now,
            )
            if current.status is not TaskStatus.LEASED:
                raise LeaseConflict("task must be leased before it can start")
            running = replace(current, status=TaskStatus.RUNNING, updated_at=now)
            self._store_task(running)
            self._append_event(
                running,
                event_type="running",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return replace(running)

    def heartbeat(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        if lease_duration <= timedelta(0):
            raise ValidationError("lease_duration must be positive")
        with self._lock:
            current = self._require_live_lease(
                site_id,
                task_id,
                worker_id,
                expected_attempt,
                now,
            )
            updated = replace(
                current,
                lease_expires_at=now + lease_duration,
                updated_at=now,
            )
            self._store_task(updated)
            return replace(updated)

    def succeed(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        output_artifact_refs: tuple[str, ...] = (),
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        if len(output_artifact_refs) != len(set(output_artifact_refs)):
            raise ValidationError("output_artifact_refs must be unique")
        with self._lock:
            current = self._require_live_lease(
                site_id,
                task_id,
                worker_id,
                expected_attempt,
                now,
            )
            completed = replace(
                current,
                status=TaskStatus.SUCCEEDED,
                output_artifact_refs=output_artifact_refs,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
            self._store_task(completed)
            self._append_event(
                completed,
                event_type="succeeded",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return replace(completed)

    def fail(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        classification: FailureClassification,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        _require_aware(retry_at, "retry_at")
        if retry_at <= now:
            raise ValidationError("retry_at must be later than now")
        with self._lock:
            current = self._require_live_lease(
                site_id,
                task_id,
                worker_id,
                expected_attempt,
                now,
            )
            if current.attempt >= current.max_attempts:
                return self._move_to_dead_letter(
                    current,
                    now=now,
                    classification=classification,
                    reason_code="max_attempts_exhausted",
                    actor_ref=worker_id,
                )
            retry = replace(
                current,
                status=TaskStatus.RECHECK,
                due_at=retry_at,
                failure_classification=classification,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
            self._store_task(retry)
            self._append_event(
                retry,
                event_type="recheck_scheduled",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return replace(retry)

    def complete_with_proposal(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        result: AgentExecutionResult,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        with self._lock:
            task = self._tasks.get((site_id, task_id))
            if task is None:
                raise TaskNotFound("task does not exist in this site")
            if task.attempt != expected_attempt:
                raise LeaseConflict("worker does not own a live lease for the expected attempt")
            proposal = ActionProposalRecord.from_execution(task, result)
            proposal_key = (site_id, task_id, expected_attempt)
            existing = self._proposals.get(proposal_key)
            if existing is not None:
                if existing.bundle_digest != proposal.bundle_digest:
                    raise IdempotencyConflict(
                        "completed task attempt was replayed with a different proposal bundle"
                    )
                self._validate_replayed_invocations(result.invocations)
                materialization = self._materializations.get(proposal_key)
                if (
                    task.status is not TaskStatus.SUCCEEDED
                    or task.attempt != expected_attempt
                    or task.output_artifact_refs != (proposal.proposal_id,)
                    or materialization is None
                    or materialization.origin != "AI"
                    or materialization.review_status != "AI Draft"
                ):
                    raise IdempotencyConflict("proposal exists without its completed task fence")
                return replace(task)
            current = self._require_live_lease(
                site_id,
                task_id,
                worker_id,
                expected_attempt,
                now,
            )
            if current.status is not TaskStatus.RUNNING:
                raise LeaseConflict("proposal completion requires a running task")
            tasks_before = self._tasks.copy()
            idempotency_before = self._idempotency.copy()
            timeline_before = deepcopy(self._timeline)
            proposals_before = self._proposals.copy()
            proposal_idempotency_before = self._proposal_idempotency.copy()
            materializations_before = self._materializations.copy()
            invocations_before = self._invocations.copy()
            invocation_idempotency_before = self._invocation_idempotency.copy()
            try:
                for invocation in result.invocations:
                    self._append_invocation(current, proposal, invocation)
                self._append_proposal(proposal)
                materialization = MaterializationOutboxRecord.from_proposal(
                    proposal,
                    created_at=now,
                )
                self._materializations[proposal_key] = materialization
                completed = replace(
                    current,
                    status=TaskStatus.SUCCEEDED,
                    output_artifact_refs=(proposal.proposal_id,),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
                self._store_task(completed)
                self._append_event(
                    completed,
                    event_type="succeeded",
                    occurred_at=now,
                    actor_type="worker",
                    actor_ref=worker_id,
                )
                return replace(completed)
            except BaseException:
                self._tasks = tasks_before
                self._idempotency = idempotency_before
                self._timeline = timeline_before
                self._proposals = proposals_before
                self._proposal_idempotency = proposal_idempotency_before
                self._materializations = materializations_before
                self._invocations = invocations_before
                self._invocation_idempotency = invocation_idempotency_before
                raise

    def get(self, site_id: str, task_id: str) -> AgentTaskMetadata | None:
        task = self._tasks.get((site_id, task_id))
        return None if task is None else replace(task)

    def timeline(self, site_id: str, task_id: str) -> tuple[TimelineEventMetadata, ...]:
        return tuple(self._timeline.get((site_id, task_id), ()))

    def dead_letter(self, site_id: str, task_id: str) -> DeadLetterMetadata | None:
        record = self._dead_letters.get((site_id, task_id))
        return None if record is None else replace(record)

    def get_proposal(
        self,
        site_id: str,
        task_id: str,
        *,
        attempt: int,
    ) -> ActionProposalRecord | None:
        return self._proposals.get((site_id, task_id, attempt))

    def get_materialization(
        self,
        site_id: str,
        task_id: str,
        *,
        attempt: int,
    ) -> MaterializationOutboxRecord | None:
        return self._materializations.get((site_id, task_id, attempt))

    def invocations(self, site_id: str) -> tuple[ModelInvocationRecord, ...]:
        return tuple(
            sorted(
                (record for record in self._invocations.values() if record.site_id == site_id),
                key=lambda record: (record.started_at, record.invocation_id),
            )
        )

    def _store_task(self, task: AgentTaskMetadata) -> None:
        self._tasks[(task.site_id, task.task_id)] = task
        self._idempotency[(task.site_id, task.idempotency_key)] = task

    def _append_event(
        self,
        task: AgentTaskMetadata,
        *,
        event_type: str,
        occurred_at: datetime,
        actor_type: str,
        actor_ref: str | None,
    ) -> None:
        key = (task.site_id, task.task_id)
        events = self._timeline[key]
        events.append(
            TimelineEventMetadata(
                task_id=task.task_id,
                site_id=task.site_id,
                sequence=len(events),
                event_type=event_type,
                occurred_at=occurred_at,
                actor_type=actor_type,
                actor_ref=actor_ref,
                causation_id=task.causation_id,
                correlation_id=task.correlation_id,
            )
        )

    def _require_live_lease(
        self,
        site_id: str,
        task_id: str,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
    ) -> AgentTaskMetadata:
        task = self._tasks.get((site_id, task_id))
        if task is None:
            raise TaskNotFound("task does not exist in this site")
        if (
            task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}
            or task.lease_owner != worker_id
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
            or task.attempt != expected_attempt
        ):
            raise LeaseConflict("worker does not own a live lease for the expected attempt")
        return task

    def _claim_candidates(
        self,
        *,
        site_id: str,
        now: datetime,
    ) -> list[AgentTaskMetadata]:
        return [
            task
            for task in self._tasks.values()
            if task.site_id == site_id
            and (
                (task.status in {TaskStatus.QUEUED, TaskStatus.RECHECK} and task.due_at <= now)
                or (
                    task.status in {TaskStatus.LEASED, TaskStatus.RUNNING}
                    and task.lease_expires_at is not None
                    and task.lease_expires_at <= now
                )
            )
        ]

    def _reap_expired(self, *, site_id: str, now: datetime) -> None:
        exhausted = [
            task
            for task in self._tasks.values()
            if task.site_id == site_id
            and task.status in {TaskStatus.LEASED, TaskStatus.RUNNING}
            and task.lease_expires_at is not None
            and task.lease_expires_at <= now
            and task.attempt >= task.max_attempts
        ]
        for task in sorted(exhausted, key=lambda item: item.task_id):
            self._move_to_dead_letter(
                task,
                now=now,
                classification=FailureClassification.INTERNAL,
                reason_code="lease_expired_max_attempts",
                actor_ref=None,
            )

    def _append_invocation(
        self,
        task: AgentTaskMetadata,
        proposal: ActionProposalRecord,
        record: ModelInvocationRecord,
    ) -> None:
        if (
            record.site_id != task.site_id
            or record.request_id != task.task_id
            or record.references.evidence_refs != proposal.evidence_refs
            or record.external_send_count != 0
            or record.tool_call_count != 0
        ):
            raise ValidationError("model invocation is not bound to the task proposal")
        idempotency_key = (record.site_id, record.idempotency_key)
        existing = self._invocation_idempotency.get(idempotency_key)
        if existing is not None:
            if existing != record:
                raise IdempotencyConflict(
                    "model invocation idempotency key was reused with different metadata"
                )
            return
        primary_key = (record.site_id, record.invocation_id)
        if primary_key in self._invocations:
            raise IdempotencyConflict("model invocation id was reused with different metadata")
        self._invocations[primary_key] = record
        self._invocation_idempotency[idempotency_key] = record

    def _append_proposal(self, proposal: ActionProposalRecord) -> None:
        idempotency_key = (proposal.site_id, proposal.idempotency_key)
        existing = self._proposal_idempotency.get(idempotency_key)
        if existing is not None:
            if existing != proposal:
                raise IdempotencyConflict(
                    "proposal idempotency key was reused with different metadata"
                )
            return
        if any(
            item.site_id == proposal.site_id and item.proposal_id == proposal.proposal_id
            for item in self._proposals.values()
        ):
            raise IdempotencyConflict("proposal id was reused with different metadata")
        self._proposals[(proposal.site_id, proposal.task_id, proposal.task_attempt)] = proposal
        self._proposal_idempotency[idempotency_key] = proposal

    def _validate_replayed_invocations(
        self,
        records: tuple[ModelInvocationRecord, ...],
    ) -> None:
        for record in records:
            existing = self._invocations.get((record.site_id, record.invocation_id))
            if existing != record:
                raise IdempotencyConflict(
                    "completed task attempt was replayed with different invocations"
                )

    def _move_to_dead_letter(
        self,
        task: AgentTaskMetadata,
        *,
        now: datetime,
        classification: FailureClassification,
        reason_code: str,
        actor_ref: str | None,
    ) -> AgentTaskMetadata:
        terminal = replace(
            task,
            status=TaskStatus.DEAD_LETTER,
            failure_classification=classification,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        self._store_task(terminal)
        self._dead_letters[(task.site_id, task.task_id)] = DeadLetterMetadata(
            task_id=task.task_id,
            site_id=task.site_id,
            attempts=task.attempt,
            failure_classification=classification,
            reason_code=reason_code,
            dead_lettered_at=now,
            causation_id=task.causation_id,
            correlation_id=task.correlation_id,
        )
        self._append_event(
            terminal,
            event_type="dead_lettered",
            occurred_at=now,
            actor_type="worker" if actor_ref is not None else "system",
            actor_ref=actor_ref,
        )
        return replace(terminal)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
