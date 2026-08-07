from datetime import UTC, datetime, timedelta

import pytest

from services.agent_runtime import (
    AgentTaskSubmission,
    FailureClassification,
    IdempotencyConflict,
    InMemoryAgentTaskRepository,
    LeaseConflict,
    LocalPilotTaskPayload,
    TaskStatus,
)

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def submission(
    *,
    site_id: str = "site-a",
    task_id: str = "task-1",
    idempotency_key: str = "request-1",
    payload: dict[str, str] | None = None,
    priority: int = 50,
    max_attempts: int = 3,
    **overrides: object,
) -> AgentTaskSubmission:
    values: dict[str, object] = {
        "task_id": task_id,
        "site_id": site_id,
        "processing_purpose": "business_operations",
        "idempotency_key": idempotency_key,
        "agent_type": "sales",
        "subject_type": "customer",
        "subject_ref": "customer-1",
        "due_at": NOW,
        "priority": priority,
        "max_attempts": max_attempts,
        "causation_id": "cause-1",
        "correlation_id": "correlation-1",
        "payload": payload or {"instruction": "prepare metadata"},
    }
    values.update(overrides)
    return AgentTaskSubmission(**values)  # type: ignore[arg-type]


def test_enqueue_returns_metadata_and_created_timeline_event() -> None:
    repository = InMemoryAgentTaskRepository()

    task = repository.enqueue(submission(), now=NOW)

    assert task.task_id == "task-1"
    assert task.status is TaskStatus.QUEUED
    assert task.attempt == 0
    assert not hasattr(task, "payload")
    timeline = repository.timeline("site-a", "task-1")
    assert [(event.sequence, event.event_type) for event in timeline] == [(0, "created")]
    assert timeline[0].causation_id == "cause-1"
    assert timeline[0].correlation_id == "correlation-1"


def test_enqueue_is_idempotent_per_site_and_rejects_payload_conflicts() -> None:
    repository = InMemoryAgentTaskRepository()
    first = repository.enqueue(submission(), now=NOW)

    replay = repository.enqueue(
        submission(task_id="ignored-on-replay"),
        now=NOW,
    )

    assert replay == first
    with pytest.raises(IdempotencyConflict):
        repository.enqueue(
            submission(task_id="task-2", payload={"instruction": "different"}),
            now=NOW,
        )

    other_site = repository.enqueue(
        submission(site_id="site-b", task_id="task-1"),
        now=NOW,
    )
    assert other_site.site_id == "site-b"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("processing_purpose", "sales_follow_up"),
        ("agent_type", "purchase"),
        ("subject_type", "supplier"),
        ("subject_ref", "supplier-9"),
        ("due_at", NOW + timedelta(hours=1)),
        ("priority", 90),
        ("max_attempts", 1),
        ("causation_id", "cause-2"),
        ("correlation_id", "correlation-2"),
    ],
)
def test_enqueue_rejects_same_idempotency_key_with_changed_command_metadata(
    field: str,
    changed: object,
) -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)

    with pytest.raises(IdempotencyConflict):
        repository.enqueue(
            submission(task_id="retry-task", **{field: changed}),
            now=NOW,
        )


def test_parent_task_must_exist_in_same_site() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(
        submission(task_id="parent", idempotency_key="parent-key"),
        now=NOW,
    )

    repository.enqueue(
        submission(
            task_id="child",
            idempotency_key="child-key",
            parent_task_id="parent",
        ),
        now=NOW,
    )
    with pytest.raises(ValueError, match="parent"):
        repository.enqueue(
            submission(
                site_id="site-b",
                task_id="cross-site-child",
                idempotency_key="cross-site-child-key",
                parent_task_id="parent",
            ),
            now=NOW,
        )


def test_claim_is_deterministic_and_does_not_steal_a_live_lease() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)
    repository.enqueue(
        submission(task_id="task-2", idempotency_key="request-2", priority=90),
        now=NOW,
    )

    high = repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    low = repository.claim(
        "site-a",
        worker_id="worker-2",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    none_left = repository.claim(
        "site-a",
        worker_id="worker-3",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    assert high is not None
    assert (high.task_id, high.status, high.attempt) == ("task-2", TaskStatus.LEASED, 1)
    assert low is not None
    assert low.task_id == "task-1"
    assert none_left is None


def test_claim_recovers_an_expired_lease_and_increments_attempt() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)
    first = repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    recovered = repository.claim(
        "site-a",
        worker_id="worker-2",
        now=NOW + timedelta(seconds=31),
        lease_duration=timedelta(seconds=30),
    )

    assert first is not None
    assert recovered is not None
    assert recovered.task_id == first.task_id
    assert recovered.lease_owner == "worker-2"
    assert recovered.attempt == 2
    assert [event.sequence for event in repository.timeline("site-a", "task-1")] == [0, 1, 2]


def test_heartbeat_requires_owner_and_a_live_lease() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)
    repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    heartbeat = repository.heartbeat(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=10),
        lease_duration=timedelta(seconds=60),
    )

    assert heartbeat.lease_expires_at == NOW + timedelta(seconds=70)
    with pytest.raises(LeaseConflict):
        repository.heartbeat(
            "site-a",
            "task-1",
            worker_id="worker-2",
            expected_attempt=1,
            now=NOW + timedelta(seconds=20),
            lease_duration=timedelta(seconds=60),
        )
    with pytest.raises(LeaseConflict):
        repository.heartbeat(
            "site-a",
            "task-1",
            worker_id="worker-1",
            expected_attempt=1,
            now=NOW + timedelta(seconds=71),
            lease_duration=timedelta(seconds=60),
        )


def test_succeed_clears_lease_and_appends_monotonic_lineage() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)
    repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    completed = repository.succeed(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=10),
        output_artifact_refs=("artifact-1",),
    )

    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.lease_owner is None
    assert completed.output_artifact_refs == ("artifact-1",)
    events = repository.timeline("site-a", "task-1")
    assert [(event.sequence, event.event_type) for event in events] == [
        (0, "created"),
        (1, "leased"),
        (2, "succeeded"),
    ]
    assert {(event.causation_id, event.correlation_id) for event in events} == {
        ("cause-1", "correlation-1")
    }


def test_failure_retries_then_dead_letters_deterministically() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(max_attempts=2), now=NOW)
    repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    retry = repository.fail(
        "site-a",
        "task-1",
        worker_id="worker-1",
        expected_attempt=1,
        now=NOW + timedelta(seconds=10),
        retry_at=NOW + timedelta(minutes=5),
        classification=FailureClassification.TOOL_FAILURE,
    )

    assert retry.status is TaskStatus.RECHECK
    assert retry.due_at == NOW + timedelta(minutes=5)
    assert retry.lease_owner is None
    second = repository.claim(
        "site-a",
        worker_id="worker-2",
        now=NOW + timedelta(minutes=5),
        lease_duration=timedelta(seconds=30),
    )
    assert second is not None
    assert second.attempt == 2

    terminal = repository.fail(
        "site-a",
        "task-1",
        worker_id="worker-2",
        expected_attempt=2,
        now=NOW + timedelta(minutes=5, seconds=10),
        retry_at=NOW + timedelta(minutes=10),
        classification=FailureClassification.TOOL_FAILURE,
    )

    assert terminal.status is TaskStatus.DEAD_LETTER
    dead_letter = repository.dead_letter("site-a", "task-1")
    assert dead_letter is not None
    assert dead_letter.attempts == 2
    assert dead_letter.failure_classification is FailureClassification.TOOL_FAILURE
    assert [event.event_type for event in repository.timeline("site-a", "task-1")] == [
        "created",
        "leased",
        "recheck_scheduled",
        "leased",
        "dead_lettered",
    ]


def test_expired_final_attempt_is_dead_lettered_instead_of_reclaimed() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(max_attempts=1), now=NOW)
    repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    claimed = repository.claim(
        "site-a",
        worker_id="worker-2",
        now=NOW + timedelta(seconds=31),
        lease_duration=timedelta(seconds=30),
    )

    assert claimed is None
    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.status is TaskStatus.DEAD_LETTER
    dead_letter = repository.dead_letter("site-a", "task-1")
    assert dead_letter is not None
    assert dead_letter.failure_classification is FailureClassification.INTERNAL


def test_fail_rejects_nonfuture_retry_even_on_final_attempt() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(max_attempts=1), now=NOW)
    repository.claim(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    with pytest.raises(ValueError, match="retry_at"):
        repository.fail(
            "site-a",
            "task-1",
            worker_id="worker-1",
            expected_attempt=1,
            now=NOW + timedelta(seconds=1),
            retry_at=NOW,
            classification=FailureClassification.TOOL_FAILURE,
        )


def test_same_worker_cannot_reuse_stale_attempt_after_reclaim() -> None:
    repository = InMemoryAgentTaskRepository()
    repository.enqueue(submission(), now=NOW)
    first = repository.claim(
        "site-a",
        worker_id="same-worker",
        now=NOW,
        lease_duration=timedelta(seconds=10),
    )
    recovered = repository.claim(
        "site-a",
        worker_id="same-worker",
        now=NOW + timedelta(seconds=11),
        lease_duration=timedelta(seconds=30),
    )

    assert first is not None
    assert recovered is not None
    assert recovered.attempt == 2
    for transition in (
        lambda: repository.start(
            "site-a",
            "task-1",
            worker_id="same-worker",
            expected_attempt=first.attempt,
            now=NOW + timedelta(seconds=12),
        ),
        lambda: repository.heartbeat(
            "site-a",
            "task-1",
            worker_id="same-worker",
            expected_attempt=first.attempt,
            now=NOW + timedelta(seconds=12),
            lease_duration=timedelta(seconds=30),
        ),
        lambda: repository.succeed(
            "site-a",
            "task-1",
            worker_id="same-worker",
            expected_attempt=first.attempt,
            now=NOW + timedelta(seconds=12),
        ),
        lambda: repository.fail(
            "site-a",
            "task-1",
            worker_id="same-worker",
            expected_attempt=first.attempt,
            now=NOW + timedelta(seconds=12),
            retry_at=NOW + timedelta(minutes=5),
            classification=FailureClassification.INTERNAL,
        ),
    ):
        with pytest.raises(LeaseConflict, match="attempt"):
            transition()


def test_claim_for_execution_atomically_returns_redacted_refs_payload_and_running_task() -> None:
    repository = InMemoryAgentTaskRepository()
    task_payload = LocalPilotTaskPayload.from_mapping(
        {
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
    )
    repository.enqueue(
        submission(
            processing_purpose="sales_follow_up",
            subject_type="CRM Deal",
            payload=task_payload.to_mapping(),  # type: ignore[arg-type]
        ),
        now=NOW,
    )

    claimed = repository.claim_for_execution(
        "site-a",
        worker_id="worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )

    assert claimed is not None
    assert claimed.metadata.status is TaskStatus.RUNNING
    assert claimed.payload == task_payload
    assert "evidence-1" not in repr(claimed)
    assert [event.event_type for event in repository.timeline("site-a", "task-1")] == [
        "created",
        "leased",
        "running",
    ]
