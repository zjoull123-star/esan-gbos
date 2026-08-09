from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.agent_runtime import (
    AgentTaskSubmission,
    FailureClassification,
    IdempotencyConflict,
    PostgresAgentTaskRepository,
    TaskStatus,
)

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def metadata_row(
    *,
    status: str = "leased",
    attempt: int = 1,
    lease_owner: str | None = "worker-1",
    lease_expires_at: datetime | None = NOW,
    payload_digest: str = "a" * 64,
    failure_classification: str | None = None,
    output_artifact_refs: list[str] | None = None,
) -> tuple[Any, ...]:
    return (
        "task-1",
        "site-a",
        "business_operations",
        "request-1",
        payload_digest,
        "sales",
        "customer",
        "customer-1",
        status,
        NOW,
        50,
        attempt,
        3,
        "cause-1",
        "correlation-1",
        None,
        output_artifact_refs or [],
        failure_classification,
        lease_owner,
        lease_expires_at,
        NOW,
        NOW,
    )


def submission(*, payload: dict[str, str] | None = None) -> AgentTaskSubmission:
    return AgentTaskSubmission(
        task_id="task-1",
        site_id="site-a",
        processing_purpose="business_operations",
        idempotency_key="request-1",
        agent_type="sales",
        subject_type="customer",
        subject_ref="customer-1",
        due_at=NOW,
        priority=50,
        max_attempts=3,
        causation_id="cause-1",
        correlation_id="correlation-1",
        payload=payload or {"instruction": "prepare metadata"},
    )


class RecordingCursor:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [row for row in self.rows if row is not None]


class RecordingConnection:
    def __init__(self, rows: list[tuple[Any, ...] | None]) -> None:
        self.cursor_instance = RecordingCursor(rows)
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance


def test_postgres_claim_is_transactional_skip_locked_and_metadata_only() -> None:
    connection = RecordingConnection([metadata_row()])
    repository = PostgresAgentTaskRepository(connection)

    task = repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    assert connection.transactions == 1
    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "set_config('app.site_id'" in sql
    assert "attempt < max_attempts" in sql
    assert "ORDER BY priority DESC, due_at ASC, created_at ASC, task_id ASC" in sql
    assert task is not None
    assert task.status is TaskStatus.LEASED
    assert not hasattr(task, "payload")


def test_postgres_enqueue_is_transactional_and_idempotent() -> None:
    request = submission()
    connection = RecordingConnection(
        [
            metadata_row(
                status="queued",
                attempt=0,
                lease_owner=None,
                lease_expires_at=None,
                payload_digest=request.payload_digest,
            )
        ]
    )
    repository = PostgresAgentTaskRepository(connection)

    task = repository.enqueue(request, now=NOW)

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "ON CONFLICT (site_id, idempotency_key) DO NOTHING" in sql
    assert "INSERT INTO agent_runtime.timeline" in sql
    assert task.payload_digest == request.payload_digest
    assert not hasattr(task, "payload")


def test_postgres_enqueue_rejects_idempotency_digest_conflict() -> None:
    connection = RecordingConnection(
        [
            None,
            metadata_row(
                status="queued",
                attempt=0,
                lease_owner=None,
                lease_expires_at=None,
                payload_digest="b" * 64,
            ),
        ]
    )
    repository = PostgresAgentTaskRepository(connection)

    with pytest.raises(IdempotencyConflict):
        repository.enqueue(submission(), now=NOW)


def test_postgres_heartbeat_is_owner_and_live_lease_guarded() -> None:
    connection = RecordingConnection([metadata_row()])
    repository = PostgresAgentTaskRepository(connection)

    task = repository.heartbeat(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "lease_owner = %s" in sql
    assert "lease_expires_at > %s" in sql
    assert "attempt = %s" in sql
    assert "status IN ('leased', 'running')" in sql
    assert task.lease_owner == "worker-1"


def test_postgres_start_is_attempt_fenced_and_transitions_only_leased_tasks() -> None:
    connection = RecordingConnection([metadata_row(status="running")])
    repository = PostgresAgentTaskRepository(connection)

    task = repository.start(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW,
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "attempt = %s" in sql
    assert "status = 'leased'" in sql
    assert task.status is TaskStatus.RUNNING


def test_postgres_failure_uses_attempt_count_for_retry_or_dead_letter() -> None:
    retry_connection = RecordingConnection(
        [
            metadata_row(
                status="recheck",
                lease_owner=None,
                lease_expires_at=None,
                failure_classification="tool_failure",
            )
        ]
    )
    retry_repository = PostgresAgentTaskRepository(retry_connection)

    retry = retry_repository.fail(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW,
        retry_at=NOW + timedelta(minutes=5),
        classification=FailureClassification.TOOL_FAILURE,
    )

    retry_sql = "\n".join(statement for statement, _ in retry_connection.cursor_instance.executed)
    assert "attempt < max_attempts" in retry_sql
    assert retry.status is TaskStatus.RECHECK

    dead_connection = RecordingConnection(
        [
            metadata_row(
                status="dead_letter",
                attempt=3,
                lease_owner=None,
                lease_expires_at=None,
                failure_classification="tool_failure",
            )
        ]
    )
    dead_repository = PostgresAgentTaskRepository(dead_connection)
    terminal = dead_repository.fail(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=3,
        now=NOW,
        retry_at=NOW + timedelta(minutes=5),
        classification=FailureClassification.TOOL_FAILURE,
    )

    dead_sql = "\n".join(statement for statement, _ in dead_connection.cursor_instance.executed)
    assert "INSERT INTO agent_runtime.dead_letter" in dead_sql
    assert terminal.status is TaskStatus.DEAD_LETTER


def test_postgres_claim_for_execution_is_single_transaction_running_and_payload_redacted() -> None:
    payload = {
        "schema_version": "local-pilot-agent-task-v1",
        "evidence_refs": ["evidence-1"],
        "fact_version_refs": [{"fact_id": "fact-1", "fact_version": 1}],
        "subject": {"revision": 1},
        "request": {
            "requested_by": "sales-agent",
            "decision_ref": "decision-1",
            "expected_action_type": "internal.work_item.propose",
            "candidate_refs": [],
        },
    }
    connection = RecordingConnection([metadata_row(status="running") + (payload,)])
    repository = PostgresAgentTaskRepository(connection)

    claimed = repository.claim_for_execution(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert connection.transactions == 1
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "status = 'running'" in sql
    assert "task.payload" in sql
    assert claimed is not None
    assert claimed.metadata.status is TaskStatus.RUNNING
    assert "evidence-1" not in repr(claimed)
