"""Crash-safe email poll-batch lifecycle used to fence checkpoint advancement."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime

from .models import ConnectorKey, TenantScope, _require_aware, stable_ulid


class EmailCheckpointFenceConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class EmailPollBatchMember:
    delivery_id: str
    terminal_kind: str | None = None
    terminal_ref: str | None = None
    terminal_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EmailPollBatchFence:
    batch_id: str
    site_id: str
    connector_instance_id: str
    expected_cursor: str | None
    candidate_cursor: str | None
    expected_version: int
    lease_generation: int
    members: tuple[EmailPollBatchMember, ...]
    created_at: datetime
    finalized_at: datetime | None = None


class InMemoryEmailCheckpointFence:
    """Reference state machine matching the PostgreSQL batch-fence invariants."""

    def __init__(self) -> None:
        self._batches: dict[tuple[str, str, str], EmailPollBatchFence] = {}

    @property
    def batches(self) -> tuple[EmailPollBatchFence, ...]:
        return tuple(self._batches.values())

    def register(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_cursor: str | None,
        candidate_cursor: str | None,
        expected_version: int,
        lease_generation: int,
        delivery_ids: tuple[str, ...],
        now: datetime,
    ) -> EmailPollBatchFence:
        if key.connector != "email":
            raise ValueError("email poll fence requires email connector")
        _require_aware(now, "now")
        if isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("invalid expected checkpoint version")
        if isinstance(lease_generation, bool) or lease_generation < 1:
            raise ValueError("invalid connector lease generation")
        if not delivery_ids or len(delivery_ids) > 1_000:
            raise ValueError("email poll batch requires bounded deliveries")
        if len(delivery_ids) != len(set(delivery_ids)):
            raise ValueError("email poll batch delivery ids must be unique")
        material = "\x1f".join(
            (
                scope.site_id,
                key.instance_id,
                expected_cursor or "",
                candidate_cursor or "",
                str(expected_version),
                *delivery_ids,
            )
        )
        batch_id = "EPB-" + stable_ulid("email-poll-batch", material)
        storage_key = (scope.site_id, key.instance_id, batch_id)
        candidate = EmailPollBatchFence(
            batch_id=batch_id,
            site_id=scope.site_id,
            connector_instance_id=key.instance_id,
            expected_cursor=expected_cursor,
            candidate_cursor=candidate_cursor,
            expected_version=expected_version,
            lease_generation=lease_generation,
            members=tuple(EmailPollBatchMember(value) for value in delivery_ids),
            created_at=now,
        )
        existing = self._batches.get(storage_key)
        if existing is None:
            self._batches[storage_key] = candidate
            return candidate
        if (
            existing.expected_cursor != expected_cursor
            or existing.candidate_cursor != candidate_cursor
            or existing.expected_version != expected_version
            or tuple(value.delivery_id for value in existing.members) != delivery_ids
        ):
            raise EmailCheckpointFenceConflict("poll_batch_replay_drift")
        if lease_generation < existing.lease_generation:
            raise EmailCheckpointFenceConflict("poll_batch_lease_generation_stale")
        if lease_generation > existing.lease_generation:
            existing = replace(existing, lease_generation=lease_generation)
            self._batches[storage_key] = existing
        return existing

    def mark_publication_terminal(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        delivery_id: str,
        terminal_ref: str,
        lease_generation: int,
        now: datetime,
    ) -> EmailPollBatchFence:
        return self._mark_terminal(
            scope,
            key,
            batch_id=batch_id,
            delivery_id=delivery_id,
            terminal_ref=terminal_ref,
            lease_generation=lease_generation,
            now=now,
            terminal_kind="published",
        )

    def mark_quarantine_terminal(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        delivery_id: str,
        terminal_ref: str,
        lease_generation: int,
        now: datetime,
    ) -> EmailPollBatchFence:
        return self._mark_terminal(
            scope,
            key,
            batch_id=batch_id,
            delivery_id=delivery_id,
            terminal_ref=terminal_ref,
            lease_generation=lease_generation,
            now=now,
            terminal_kind="quarantined",
        )

    def _mark_terminal(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        delivery_id: str,
        terminal_ref: str,
        lease_generation: int,
        now: datetime,
        terminal_kind: str,
    ) -> EmailPollBatchFence:
        _require_aware(now, "now")
        storage_key = (scope.site_id, key.instance_id, batch_id)
        batch = self._batches.get(storage_key)
        if batch is None:
            raise EmailCheckpointFenceConflict("poll_batch_missing")
        if batch.lease_generation != lease_generation:
            raise EmailCheckpointFenceConflict("poll_batch_lease_generation_stale")
        members = list(batch.members)
        for index, member in enumerate(members):
            if member.delivery_id != delivery_id:
                continue
            if member.terminal_kind is not None:
                if member.terminal_kind != terminal_kind or member.terminal_ref != terminal_ref:
                    raise EmailCheckpointFenceConflict("poll_batch_terminal_drift")
                return batch
            members[index] = replace(
                member,
                terminal_kind=terminal_kind,
                terminal_ref="sha256:" + hashlib.sha256(terminal_ref.encode()).hexdigest(),
                terminal_at=now,
            )
            updated = replace(batch, members=tuple(members))
            self._batches[storage_key] = updated
            return updated
        raise EmailCheckpointFenceConflict("poll_batch_delivery_missing")

    def finalize(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        batch_id: str,
        expected_version: int,
        lease_generation: int,
        now: datetime,
    ) -> bool:
        _require_aware(now, "now")
        storage_key = (scope.site_id, key.instance_id, batch_id)
        batch = self._batches.get(storage_key)
        if batch is None or batch.finalized_at is not None:
            return batch is not None and batch.finalized_at is not None
        if batch.lease_generation != lease_generation:
            return False
        if batch.expected_version != expected_version:
            return False
        if any(member.terminal_kind is None for member in batch.members):
            return False
        self._batches[storage_key] = replace(batch, finalized_at=now)
        return True

    def __repr__(self) -> str:
        return f"{type(self).__name__}(batch_count={len(self._batches)})"


__all__ = [
    "EmailCheckpointFenceConflict",
    "EmailPollBatchFence",
    "EmailPollBatchMember",
    "InMemoryEmailCheckpointFence",
]
