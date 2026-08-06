from datetime import UTC, datetime, timedelta

import pytest

from services.agent_runtime import (
    AgentTaskRepository,
    AgentTaskSubmission,
    InMemoryAgentTaskRepository,
    PostgresAgentTaskRepository,
    ValidationError,
)

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def valid_submission(**overrides: object) -> AgentTaskSubmission:
    values: dict[str, object] = {
        "task_id": "task-1",
        "site_id": "site-a",
        "processing_purpose": "business_operations",
        "idempotency_key": "request-1",
        "agent_type": "sales",
        "subject_type": "customer",
        "subject_ref": "customer-1",
        "due_at": NOW,
        "priority": 50,
        "max_attempts": 3,
        "causation_id": "cause-1",
        "correlation_id": "correlation-1",
        "payload": {"b": 2, "a": 1},
    }
    values.update(overrides)
    return AgentTaskSubmission(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("site_id", ""),
        ("task_id", ""),
        ("idempotency_key", ""),
        ("due_at", datetime(2026, 8, 7, 9, 0)),
        ("priority", -1),
        ("priority", 101),
        ("max_attempts", 0),
        ("max_attempts", 101),
    ],
)
def test_submission_rejects_invalid_queue_metadata(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        valid_submission(**{field: value})


def test_payload_digest_is_canonical_and_payload_is_detached() -> None:
    original = {"a": {"value": 1}, "b": 2}
    request = valid_submission(payload=original)
    same = valid_submission(payload={"b": 2, "a": {"value": 1}})

    original["a"]["value"] = 99  # type: ignore[index]

    assert request.payload_digest == same.payload_digest
    assert request.payload["a"] == {"value": 1}


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("site_id", "site-b"),
        ("processing_purpose", "sales_follow_up"),
        ("idempotency_key", "request-2"),
        ("agent_type", "purchase"),
        ("subject_type", "supplier"),
        ("subject_ref", "supplier-9"),
        ("due_at", NOW + timedelta(hours=1)),
        ("priority", 90),
        ("max_attempts", 1),
        ("causation_id", "cause-2"),
        ("correlation_id", "correlation-2"),
        ("parent_task_id", "parent-2"),
    ],
)
def test_idempotency_digest_binds_every_submission_field_except_task_id(
    field: str,
    changed: object,
) -> None:
    base: dict[str, object] = {"parent_task_id": "parent-1"}
    original = valid_submission(**base)
    base[field] = changed
    changed_submission = valid_submission(**base)

    assert changed_submission.payload_digest != original.payload_digest
    assert (
        valid_submission(
            parent_task_id="parent-1",
            task_id="caller-retry-task-id",
        ).payload_digest
        == original.payload_digest
    )


def test_submission_payload_is_deeply_immutable_after_digesting() -> None:
    request = valid_submission(payload={"command": {"mode": "draft"}, "refs": ["evidence-1"]})

    with pytest.raises(TypeError):
        request.payload["command"]["mode"] = "execute"  # type: ignore[index]
    with pytest.raises(AttributeError):
        request.payload["refs"].append("evidence-2")  # type: ignore[union-attr]


def test_submission_rejects_self_parent_reference() -> None:
    with pytest.raises(ValidationError, match="parent"):
        valid_submission(parent_task_id="task-1")


def test_in_memory_and_postgres_implement_repository_protocol() -> None:
    class UnusedConnection:
        pass

    assert isinstance(InMemoryAgentTaskRepository(), AgentTaskRepository)
    assert isinstance(
        PostgresAgentTaskRepository(UnusedConnection()),  # type: ignore[arg-type]
        AgentTaskRepository,
    )


def test_repository_rejects_naive_transition_time() -> None:
    repository = InMemoryAgentTaskRepository()

    with pytest.raises(ValidationError):
        repository.enqueue(valid_submission(), now=datetime(2026, 8, 7, 9, 0))
