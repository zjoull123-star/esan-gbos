from __future__ import annotations

import pytest
from esan_gbos.domain.idempotency import (
    IdempotencyConflict,
    MemoryIdempotencyRepository,
    execute_once,
)


def test_same_key_and_payload_replays_original_result() -> None:
    repository = MemoryIdempotencyRepository()
    executions = 0

    def command() -> dict[str, str]:
        nonlocal executions
        executions += 1
        return {"name": "SAM-01"}

    first = execute_once(repository, "sample.create_project", "key-1", {"title": "A"}, command)
    replay = execute_once(repository, "sample.create_project", "key-1", {"title": "A"}, command)

    assert first == replay == {"name": "SAM-01"}
    assert executions == 1


def test_same_key_with_different_payload_is_rejected() -> None:
    repository = MemoryIdempotencyRepository()
    execute_once(repository, "work.transition", "key-2", {"to_status": "Done"}, lambda: {"ok": 1})

    with pytest.raises(IdempotencyConflict, match="idempotency_conflict"):
        execute_once(
            repository,
            "work.transition",
            "key-2",
            {"to_status": "Cancelled"},
            lambda: {"ok": 2},
        )


def test_key_is_site_scoped_and_cannot_be_reused_by_another_command() -> None:
    repository = MemoryIdempotencyRepository()
    execute_once(
        repository,
        "work.transition",
        "site-wide-key",
        {"value": 1},
        lambda: {"ok": 1},
    )

    with pytest.raises(IdempotencyConflict, match="idempotency_conflict"):
        execute_once(
            repository,
            "sample.create_project",
            "site-wide-key",
            {"value": 1},
            lambda: {"ok": 2},
        )
