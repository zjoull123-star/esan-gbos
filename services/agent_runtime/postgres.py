from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol

from .models import (
    AgentTaskMetadata,
    AgentTaskSubmission,
    DeadLetterMetadata,
    FailureClassification,
    IdempotencyConflict,
    LeaseConflict,
    TaskStatus,
    TimelineEventMetadata,
    ValidationError,
    thaw_json,
)

_METADATA_COLUMNS = """
    task_id, site_id, processing_purpose, idempotency_key, payload_digest,
    agent_type, subject_type, subject_ref, status, due_at, priority, attempt,
    max_attempts, causation_id, correlation_id, parent_task_id,
    output_artifact_refs, failure_classification, lease_owner, lease_expires_at,
    created_at, updated_at
"""


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresAgentTaskRepository:
    """PostgreSQL Agent Task repository with transactional site-scoped leases."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def enqueue(
        self,
        submission: AgentTaskSubmission,
        *,
        now: datetime,
    ) -> AgentTaskMetadata:
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, submission.site_id)
            if submission.parent_task_id is not None:
                cursor.execute(
                    """
                    SELECT 1
                    FROM agent_runtime.agent_tasks
                    WHERE site_id = %s AND task_id = %s
                    """,
                    (submission.site_id, submission.parent_task_id),
                )
                if cursor.fetchone() is None:
                    raise ValidationError("parent task does not exist in this site")
            cursor.execute(
                f"""
                INSERT INTO agent_runtime.agent_tasks (
                    site_id, task_id, processing_purpose, idempotency_key,
                    payload_digest, payload, agent_type, subject_type, subject_ref,
                    status, due_at, priority, attempt, max_attempts,
                    causation_id, correlation_id, parent_task_id,
                    output_artifact_refs, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s,
                    'queued', %s, %s, 0, %s, %s, %s, %s, '[]'::jsonb, %s, %s
                )
                ON CONFLICT (site_id, idempotency_key) DO NOTHING
                RETURNING {_METADATA_COLUMNS}
                """,
                (
                    submission.site_id,
                    submission.task_id,
                    submission.processing_purpose,
                    submission.idempotency_key,
                    submission.payload_digest,
                    json.dumps(
                        thaw_json(submission.payload),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    submission.agent_type,
                    submission.subject_type,
                    submission.subject_ref,
                    submission.due_at,
                    submission.priority,
                    submission.max_attempts,
                    submission.causation_id,
                    submission.correlation_id,
                    submission.parent_task_id,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""
                    SELECT {_METADATA_COLUMNS}
                    FROM agent_runtime.agent_tasks
                    WHERE site_id = %s AND idempotency_key = %s
                    """,
                    (submission.site_id, submission.idempotency_key),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValidationError("task_id already exists with another request")
                existing = _metadata_from_row(row)
                if existing.payload_digest != submission.payload_digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different submission"
                    )
                return existing
            task = _metadata_from_row(row)
            self._append_timeline(
                cursor,
                task=task,
                event_type="created",
                occurred_at=now,
                actor_type="system",
                actor_ref=None,
            )
            return task

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> AgentTaskMetadata | None:
        _require_aware(now, "now")
        if not site_id or not worker_id:
            raise ValidationError("site_id and worker_id are required")
        if lease_duration <= timedelta(0):
            raise ValidationError("lease_duration must be positive")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            self._reap_expired(cursor, site_id=site_id, now=now)
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT site_id, task_id
                    FROM agent_runtime.agent_tasks
                    WHERE site_id = %s
                      AND attempt < max_attempts
                      AND (
                          (status IN ('queued', 'recheck') AND due_at <= %s)
                          OR
                          (
                              status IN ('leased', 'running')
                              AND lease_expires_at <= %s
                          )
                      )
                    ORDER BY priority DESC, due_at ASC, created_at ASC, task_id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE agent_runtime.agent_tasks AS task
                SET status = 'leased',
                    attempt = task.attempt + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    updated_at = %s
                FROM candidate
                WHERE task.site_id = candidate.site_id
                  AND task.task_id = candidate.task_id
                RETURNING {_qualified_metadata_columns("task")}
                """,
                (
                    site_id,
                    now,
                    now,
                    worker_id,
                    now + lease_duration,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            task = _metadata_from_row(row)
            self._append_timeline(
                cursor,
                task=task,
                event_type="leased",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return task

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
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE agent_runtime.agent_tasks
                SET lease_expires_at = %s,
                    updated_at = %s
                WHERE site_id = %s
                  AND task_id = %s
                  AND lease_owner = %s
                  AND lease_expires_at > %s
                  AND status IN ('leased', 'running')
                RETURNING {_METADATA_COLUMNS}
                """,
                (
                    now + lease_duration,
                    now,
                    site_id,
                    task_id,
                    worker_id,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict("worker does not own a live lease")
            return _metadata_from_row(row)

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
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE agent_runtime.agent_tasks
                SET status = 'succeeded',
                    output_artifact_refs = %s::jsonb,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = %s
                WHERE site_id = %s
                  AND task_id = %s
                  AND lease_owner = %s
                  AND lease_expires_at > %s
                  AND status IN ('leased', 'running')
                RETURNING {_METADATA_COLUMNS}
                """,
                (
                    json.dumps(output_artifact_refs),
                    now,
                    site_id,
                    task_id,
                    worker_id,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict("worker does not own a live lease")
            task = _metadata_from_row(row)
            self._append_timeline(
                cursor,
                task=task,
                event_type="succeeded",
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return task

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
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE agent_runtime.agent_tasks
                SET status = CASE
                        WHEN attempt < max_attempts THEN 'recheck'
                        ELSE 'dead_letter'
                    END,
                    due_at = CASE
                        WHEN attempt < max_attempts THEN %s
                        ELSE due_at
                    END,
                    recheck_at = CASE
                        WHEN attempt < max_attempts THEN %s
                        ELSE recheck_at
                    END,
                    failure_classification = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = %s
                WHERE site_id = %s
                  AND task_id = %s
                  AND lease_owner = %s
                  AND lease_expires_at > %s
                  AND status IN ('leased', 'running')
                RETURNING {_METADATA_COLUMNS}
                """,
                (
                    retry_at,
                    retry_at,
                    classification.value,
                    now,
                    site_id,
                    task_id,
                    worker_id,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseConflict("worker does not own a live lease")
            task = _metadata_from_row(row)
            event_type = "recheck_scheduled"
            if task.status is TaskStatus.DEAD_LETTER:
                cursor.execute(
                    """
                    INSERT INTO agent_runtime.dead_letter (
                        site_id, task_id, dead_letter_id, attempts,
                        failure_classification, reason_code, dead_lettered_at,
                        causation_id, correlation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (site_id, task_id) DO NOTHING
                    """,
                    (
                        task.site_id,
                        task.task_id,
                        f"dead-letter:{task.task_id}",
                        task.attempt,
                        classification.value,
                        "max_attempts_exhausted",
                        now,
                        task.causation_id,
                        task.correlation_id,
                    ),
                )
                event_type = "dead_lettered"
            self._append_timeline(
                cursor,
                task=task,
                event_type=event_type,
                occurred_at=now,
                actor_type="worker",
                actor_ref=worker_id,
            )
            return task

    def get(self, site_id: str, task_id: str) -> AgentTaskMetadata | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_METADATA_COLUMNS}
                FROM agent_runtime.agent_tasks
                WHERE site_id = %s AND task_id = %s
                """,
                (site_id, task_id),
            )
            row = cursor.fetchone()
            return None if row is None else _metadata_from_row(row)

    def timeline(self, site_id: str, task_id: str) -> tuple[TimelineEventMetadata, ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT task_id, site_id, sequence, event_type, occurred_at,
                       actor_type, actor_ref, causation_id, correlation_id
                FROM agent_runtime.timeline
                WHERE site_id = %s AND task_id = %s
                ORDER BY sequence ASC
                """,
                (site_id, task_id),
            )
            return tuple(
                TimelineEventMetadata(
                    task_id=str(row[0]),
                    site_id=str(row[1]),
                    sequence=int(row[2]),
                    event_type=str(row[3]),
                    occurred_at=row[4],
                    actor_type=str(row[5]),
                    actor_ref=None if row[6] is None else str(row[6]),
                    causation_id=str(row[7]),
                    correlation_id=str(row[8]),
                )
                for row in cursor.fetchall()
            )

    def dead_letter(self, site_id: str, task_id: str) -> DeadLetterMetadata | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                SELECT task_id, site_id, attempts, failure_classification,
                       reason_code, dead_lettered_at, causation_id, correlation_id
                FROM agent_runtime.dead_letter
                WHERE site_id = %s AND task_id = %s
                """,
                (site_id, task_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return DeadLetterMetadata(
                task_id=str(row[0]),
                site_id=str(row[1]),
                attempts=int(row[2]),
                failure_classification=FailureClassification(str(row[3])),
                reason_code=str(row[4]),
                dead_lettered_at=row[5],
                causation_id=str(row[6]),
                correlation_id=str(row[7]),
            )

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))

    @staticmethod
    def _reap_expired(cursor: Cursor, *, site_id: str, now: datetime) -> None:
        cursor.execute(
            """
            WITH expired AS (
                SELECT site_id, task_id
                FROM agent_runtime.agent_tasks
                WHERE site_id = %s
                  AND status IN ('leased', 'running')
                  AND lease_expires_at <= %s
                  AND attempt >= max_attempts
                ORDER BY task_id ASC
                FOR UPDATE SKIP LOCKED
            ),
            terminal AS (
                UPDATE agent_runtime.agent_tasks AS task
                SET status = 'dead_letter',
                    failure_classification = 'internal',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = %s
                FROM expired
                WHERE task.site_id = expired.site_id
                  AND task.task_id = expired.task_id
                RETURNING task.*
            ),
            stored AS (
                INSERT INTO agent_runtime.dead_letter (
                    site_id, task_id, dead_letter_id, attempts,
                    failure_classification, reason_code, dead_lettered_at,
                    causation_id, correlation_id
                )
                SELECT site_id, task_id, 'dead-letter:' || task_id, attempt,
                       'internal', 'lease_expired_max_attempts', %s,
                       causation_id, correlation_id
                FROM terminal
                ON CONFLICT (site_id, task_id) DO NOTHING
            )
            INSERT INTO agent_runtime.timeline (
                site_id, task_id, sequence, timeline_event_id, event_type,
                occurred_at, actor_type, actor_ref, causation_id, correlation_id
            )
            SELECT terminal.site_id,
                   terminal.task_id,
                   COALESCE((
                       SELECT MAX(existing.sequence) + 1
                       FROM agent_runtime.timeline AS existing
                       WHERE existing.site_id = terminal.site_id
                         AND existing.task_id = terminal.task_id
                   ), 0),
                   terminal.task_id || ':dead_lettered:' || terminal.attempt,
                   'dead_lettered',
                   %s,
                   'system',
                   NULL,
                   terminal.causation_id,
                   terminal.correlation_id
            FROM terminal
            """,
            (site_id, now, now, now, now),
        )

    @staticmethod
    def _append_timeline(
        cursor: Cursor,
        *,
        task: AgentTaskMetadata,
        event_type: str,
        occurred_at: datetime,
        actor_type: str,
        actor_ref: str | None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO agent_runtime.timeline (
                site_id, task_id, sequence, timeline_event_id, event_type,
                occurred_at, actor_type, actor_ref, causation_id, correlation_id
            )
            SELECT %s,
                   %s,
                   COALESCE(MAX(sequence) + 1, 0),
                   %s || ':' || COALESCE(MAX(sequence) + 1, 0)::text,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s,
                   %s
            FROM agent_runtime.timeline
            WHERE site_id = %s AND task_id = %s
            """,
            (
                task.site_id,
                task.task_id,
                task.task_id,
                event_type,
                occurred_at,
                actor_type,
                actor_ref,
                task.causation_id,
                task.correlation_id,
                task.site_id,
                task.task_id,
            ),
        )


def _qualified_metadata_columns(alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in _METADATA_COLUMNS.split(","))


def _metadata_from_row(row: tuple[Any, ...]) -> AgentTaskMetadata:
    output_refs_value = row[16]
    if isinstance(output_refs_value, str):
        output_refs_value = json.loads(output_refs_value)
    output_refs = tuple(str(value) for value in output_refs_value)
    failure = None if row[17] is None else FailureClassification(str(row[17]))
    return AgentTaskMetadata(
        task_id=str(row[0]),
        site_id=str(row[1]),
        processing_purpose=str(row[2]),
        idempotency_key=str(row[3]),
        payload_digest=str(row[4]),
        agent_type=str(row[5]),
        subject_type=str(row[6]),
        subject_ref=str(row[7]),
        status=TaskStatus(str(row[8])),
        due_at=row[9],
        priority=int(row[10]),
        attempt=int(row[11]),
        max_attempts=int(row[12]),
        causation_id=str(row[13]),
        correlation_id=str(row[14]),
        parent_task_id=None if row[15] is None else str(row[15]),
        output_artifact_refs=output_refs,
        failure_classification=failure,
        lease_owner=None if row[18] is None else str(row[18]),
        lease_expires_at=row[19],
        created_at=row[20],
        updated_at=row[21],
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
