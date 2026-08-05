from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast


class IdempotencyConflict(ValueError):
    """Raised when a key is reused for a different command payload."""


@dataclass(frozen=True)
class IdempotencyRecord:
    payload_hash: str
    result: Any


class IdempotencyRepository(Protocol):
    def get(self, key: str) -> IdempotencyRecord | None: ...

    def put(
        self,
        key: str,
        record: IdempotencyRecord,
    ) -> None: ...


class MemoryIdempotencyRepository:
    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)

    def put(self, key: str, record: IdempotencyRecord) -> None:
        self._records[key] = record


def payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def command_payload_hash(
    command: str,
    actor: str,
    payload: dict[str, Any],
) -> str:
    return payload_hash(
        {
            "command": command,
            "actor": actor,
            "payload": payload,
        }
    )


def execute_once[Result](
    repository: IdempotencyRepository,
    command: str,
    key: str,
    payload: dict[str, Any],
    execute: Callable[[], Result],
    *,
    actor: str = "",
) -> Result:
    digest = command_payload_hash(command, actor, payload)
    existing = repository.get(key)
    if existing is not None:
        if existing.payload_hash != digest:
            raise IdempotencyConflict("idempotency_conflict")
        return cast("Result", existing.result)

    result = execute()
    repository.put(key, IdempotencyRecord(payload_hash=digest, result=result))
    return result
