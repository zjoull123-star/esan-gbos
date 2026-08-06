from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol, runtime_checkable

from .models import (
    AgentTaskMetadata,
    AgentTaskSubmission,
    DeadLetterMetadata,
    FailureClassification,
    IdempotencyConflict,
    LeaseConflict,
    TaskNotFound,
    TaskStatus,
    TimelineEventMetadata,
    ValidationError,
)


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

    def heartbeat(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata: ...

    def succeed(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        now: datetime,
        output_artifact_refs: tuple[str, ...] = (),
    ) -> AgentTaskMetadata: ...

    def fail(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        now: datetime,
        retry_at: datetime,
        classification: FailureClassification,
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

    def heartbeat(
        self,
        site_id: str,
        task_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        if lease_duration <= timedelta(0):
            raise ValidationError("lease_duration must be positive")
        with self._lock:
            current = self._require_live_lease(site_id, task_id, worker_id, now)
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
        now: datetime,
        output_artifact_refs: tuple[str, ...] = (),
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        if len(output_artifact_refs) != len(set(output_artifact_refs)):
            raise ValidationError("output_artifact_refs must be unique")
        with self._lock:
            current = self._require_live_lease(site_id, task_id, worker_id, now)
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
        now: datetime,
        retry_at: datetime,
        classification: FailureClassification,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        _require_aware(retry_at, "retry_at")
        if retry_at <= now:
            raise ValidationError("retry_at must be later than now")
        with self._lock:
            current = self._require_live_lease(site_id, task_id, worker_id, now)
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

    def get(self, site_id: str, task_id: str) -> AgentTaskMetadata | None:
        task = self._tasks.get((site_id, task_id))
        return None if task is None else replace(task)

    def timeline(self, site_id: str, task_id: str) -> tuple[TimelineEventMetadata, ...]:
        return tuple(self._timeline.get((site_id, task_id), ()))

    def dead_letter(self, site_id: str, task_id: str) -> DeadLetterMetadata | None:
        record = self._dead_letters.get((site_id, task_id))
        return None if record is None else replace(record)

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
        ):
            raise LeaseConflict("worker does not own a live lease")
        return task

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
