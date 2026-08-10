"""Fail-closed Observer, CAS, and tokenizer-vault retention enforcement."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .models import TenantScope

_MAX_BATCH_SIZE = 1_000
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")


class RetentionError(RuntimeError):
    """Content-free retention failure suitable for logs and process boundaries."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid retention error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"RetentionError(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    scanned_count: int
    eligible_count: int
    legal_hold_count: int
    historical_reference_count: int

    def __post_init__(self) -> None:
        values = (
            self.scanned_count,
            self.eligible_count,
            self.legal_hold_count,
            self.historical_reference_count,
        )
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("retention counts must be non-negative integers")
        if self.eligible_count + self.legal_hold_count + self.historical_reference_count > (
            self.scanned_count
        ):
            raise ValueError("retention preview counts are inconsistent")


@dataclass(frozen=True, slots=True, repr=False)
class RetentionFence:
    run_id: str
    worker_id: str
    generation: int

    def __post_init__(self) -> None:
        if (
            not self.run_id
            or not self.worker_id
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("invalid retention fence")

    def __repr__(self) -> str:
        return f"RetentionFence(generation={self.generation}, identifiers=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class CasDeletionLease:
    object_ref: str
    sha256: str
    lease_generation: int

    def __post_init__(self) -> None:
        if (
            not self.object_ref
            or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None
            or isinstance(self.lease_generation, bool)
            or self.lease_generation < 1
        ):
            raise ValueError("invalid CAS deletion lease")

    def __repr__(self) -> str:
        return (
            f"CasDeletionLease(lease_generation={self.lease_generation}, "
            "object_ref=<redacted>, sha256=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class RetentionResult:
    dry_run: bool
    preview: RetentionPreview
    metadata_deleted_count: int
    cas_deleted_count: int
    vault_deleted_count: int
    completed_at: datetime

    def as_receipt(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "dry_run": self.dry_run,
            "scanned_count": self.preview.scanned_count,
            "eligible_count": self.preview.eligible_count,
            "legal_hold_count": self.preview.legal_hold_count,
            "historical_reference_count": self.preview.historical_reference_count,
            "metadata_deleted_count": self.metadata_deleted_count,
            "cas_deleted_count": self.cas_deleted_count,
            "vault_deleted_count": self.vault_deleted_count,
            "completed_at": self.completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }


class RetentionStorage(Protocol):
    def preview_batch(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        batch_size: int,
    ) -> RetentionPreview: ...

    def claim_run(
        self,
        scope: TenantScope,
        *,
        run_id: str,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RetentionFence: ...

    def expire_metadata(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        batch_size: int,
    ) -> tuple[RetentionPreview, int]: ...

    def claim_cas_deletions(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[CasDeletionLease, ...]: ...

    def complete_cas_deletion(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        lease: CasDeletionLease,
        *,
        now: datetime,
    ) -> None: ...

    def complete_run(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        metadata_deleted_count: int,
        cas_deleted_count: int,
        vault_deleted_count: int,
    ) -> None: ...


class CasStore(Protocol):
    def delete(self, scope: TenantScope, object_ref: str) -> None: ...


class MappingVault(Protocol):
    def cleanup_expired(self, *, now: datetime | None = None) -> int: ...


class RetentionService:
    """Coordinate one bounded retention pass across durable and filesystem stores."""

    __slots__ = (
        "_storage",
        "_cas",
        "_vault",
        "_worker_id",
        "_clock",
        "_lease_duration",
        "_run_id_factory",
    )

    def __init__(
        self,
        *,
        storage: RetentionStorage,
        cas: CasStore,
        vault: MappingVault,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(minutes=5),
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 256:
            raise ValueError("invalid retention worker_id")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(hours=1):
            raise ValueError("invalid retention lease duration")
        self._storage = storage
        self._cas = cas
        self._vault = vault
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._run_id_factory = run_id_factory or (lambda: f"retention-{uuid.uuid4().hex}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(dependencies=<redacted>)"

    def run(
        self,
        scope: TenantScope,
        *,
        batch_size: int,
        dry_run: bool,
    ) -> RetentionResult:
        _validate_batch_size(batch_size)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be boolean")
        now = _aware_utc(self._clock(), "clock")
        if dry_run:
            preview = self._storage.preview_batch(scope, now=now, batch_size=batch_size)
            return RetentionResult(True, preview, 0, 0, 0, now)

        lease_until = now + self._lease_duration
        fence = self._storage.claim_run(
            scope,
            run_id=self._run_id_factory(),
            worker_id=self._worker_id,
            now=now,
            lease_until=lease_until,
        )
        preview, metadata_deleted_count = self._storage.expire_metadata(
            scope,
            fence,
            now=now,
            batch_size=batch_size,
        )
        leases = self._storage.claim_cas_deletions(
            scope,
            fence,
            now=now,
            lease_until=lease_until,
            batch_size=batch_size,
        )
        cas_deleted_count = 0
        for lease in leases:
            try:
                self._cas.delete(scope, lease.object_ref)
            except Exception as exc:
                raise RetentionError("retention.cas_delete_failed") from exc
            self._storage.complete_cas_deletion(scope, fence, lease, now=now)
            cas_deleted_count += 1
        try:
            vault_deleted_count = self._vault.cleanup_expired(now=now)
        except Exception as exc:
            raise RetentionError("retention.vault_cleanup_failed") from exc
        self._storage.complete_run(
            scope,
            fence,
            now=now,
            metadata_deleted_count=metadata_deleted_count,
            cas_deleted_count=cas_deleted_count,
            vault_deleted_count=vault_deleted_count,
        )
        return RetentionResult(
            False,
            preview,
            metadata_deleted_count,
            cas_deleted_count,
            vault_deleted_count,
            now,
        )


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresRetentionStorage:
    """PostgreSQL implementation backed by migration 011 retention functions."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return f"{type(self).__name__}(connection=<redacted>)"

    def preview_batch(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        batch_size: int,
    ) -> RetentionPreview:
        _validate_batch_size(batch_size)
        _aware_utc(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT * FROM observer.preview_retention_batch(%s, %s, %s)",
                (scope.site_id, now, batch_size),
            )
            return _preview_from_row(_required_row(cursor.fetchone()))

    def claim_run(
        self,
        scope: TenantScope,
        *,
        run_id: str,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RetentionFence:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT run_id, worker_id, lease_generation "
                "FROM observer.claim_retention_run(%s, %s, %s, %s, %s)",
                (scope.site_id, run_id, worker_id, now, lease_until),
            )
            row = cursor.fetchone()
        if row is None:
            raise RetentionError("retention.run_claim_conflict")
        return RetentionFence(str(row[0]), str(row[1]), int(row[2]))

    def expire_metadata(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        batch_size: int,
    ) -> tuple[RetentionPreview, int]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT * FROM observer.expire_retention_metadata(%s, %s, %s, %s, %s, %s)",
                (
                    scope.site_id,
                    fence.run_id,
                    fence.worker_id,
                    fence.generation,
                    now,
                    batch_size,
                ),
            )
            row = _required_row(cursor.fetchone())
        return _preview_from_row(row[:4]), int(row[4])

    def claim_cas_deletions(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[CasDeletionLease, ...]:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT object_ref, object_sha256, lease_generation "
                "FROM observer.claim_retention_cas_deletions(%s, %s, %s, %s, %s, %s, %s)",
                (
                    scope.site_id,
                    fence.run_id,
                    fence.worker_id,
                    fence.generation,
                    now,
                    lease_until,
                    batch_size,
                ),
            )
            rows = cursor.fetchall()
        return tuple(CasDeletionLease(str(row[0]), str(row[1]), int(row[2])) for row in rows)

    def complete_cas_deletion(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        lease: CasDeletionLease,
        *,
        now: datetime,
    ) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT observer.complete_retention_cas_deletion(%s, %s, %s, %s, %s, %s, %s)",
                (
                    scope.site_id,
                    fence.run_id,
                    fence.worker_id,
                    fence.generation,
                    lease.object_ref,
                    lease.lease_generation,
                    now,
                ),
            )
            row = cursor.fetchone()
        if row is None or row[0] is not True:
            raise RetentionError("retention.cas_fence_conflict")

    def complete_run(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        metadata_deleted_count: int,
        cas_deleted_count: int,
        vault_deleted_count: int,
    ) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            _set_scope(cursor, scope)
            cursor.execute(
                "SELECT observer.complete_retention_run(%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    scope.site_id,
                    fence.run_id,
                    fence.worker_id,
                    fence.generation,
                    now,
                    metadata_deleted_count,
                    cas_deleted_count,
                    vault_deleted_count,
                ),
            )
            row = cursor.fetchone()
        if row is None or row[0] is not True:
            raise RetentionError("retention.run_fence_conflict")


def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
    cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
    cursor.execute(
        "SELECT set_config('app.processing_purpose', %s, true)",
        (scope.processing_purpose,),
    )


def _preview_from_row(row: tuple[Any, ...]) -> RetentionPreview:
    return RetentionPreview(*(int(value) for value in row[:4]))


def _required_row(row: tuple[Any, ...] | None) -> tuple[Any, ...]:
    if row is None:
        raise RetentionError("retention.storage_contract_failed")
    return row


def _validate_batch_size(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_BATCH_SIZE:
        raise ValueError("batch_size must be between 1 and 1000")


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "CasDeletionLease",
    "PostgresRetentionStorage",
    "RetentionError",
    "RetentionFence",
    "RetentionPreview",
    "RetentionResult",
    "RetentionService",
]
