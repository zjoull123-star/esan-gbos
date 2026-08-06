from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    event_type: str
    request_id: str
    site_id: str
    account_set_fingerprint: str
    processing_purpose: str
    tool_name: str
    logical_object: str
    status: str
    reason_code: str | None
    returned_rows: int
    synthetic: bool
    network_calls: int
    writer_tools_discovered: int
    mutation_attempts: int

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    def record(self, event: AuditEvent) -> None:
        del event


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def account_set_fingerprint(account_set_ref: str) -> str:
    digest = hashlib.sha256(account_set_ref.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"
