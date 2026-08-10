"""Durable fail-closed latch for site-scoped model egress."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from .models import TenantScope, _require_aware
from .storage import Connection, Cursor

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")

FATAL_MODEL_ERROR_CODES = frozenset(
    {
        "budget_hard_stop",
        "input_token_limit",
        "internal_error",
        "invalid_model_output",
        "model_binding_mismatch",
        "model_mismatch",
        "model_provider_failed",
        "output_invalid_json",
        "output_schema_invalid",
        "pricing_error",
        "provider_http_error",
        "request_binding_failed",
        "response_invalid_json",
        "response_protocol_error",
        "unsafe_output",
    }
)


class ModelFatalLatchError(RuntimeError):
    """Safe latch rejection without tenant, identity, or provider content."""

    __slots__ = ("code",)

    def __init__(self, code: str = "model_fatal_latched") -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid model latch error code")
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ModelFatalLatchError(code={self.code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ModelFatalLatchStatus:
    """Low-cardinality read seam for status and evidence reporting."""

    tripped: bool
    error_code: str | None
    latched_at: datetime | None

    def __post_init__(self) -> None:
        if self.tripped:
            if self.error_code not in FATAL_MODEL_ERROR_CODES or self.latched_at is None:
                raise ValueError("invalid tripped model latch status")
            _require_aware(self.latched_at, "latched_at")
        elif self.error_code is not None or self.latched_at is not None:
            raise ValueError("open model latch cannot contain failure metadata")

    def __repr__(self) -> str:
        return (
            "ModelFatalLatchStatus("
            f"tripped={self.tripped!r}, error_code={self.error_code!r}, "
            f"latched_at={self.latched_at!r})"
        )


class ModelFatalLatch(Protocol):
    def status(self, scope: TenantScope) -> ModelFatalLatchStatus: ...

    def assert_open(self, scope: TenantScope) -> None: ...

    def egress_guard(self, scope: TenantScope) -> AbstractContextManager[None]: ...

    def trip(
        self,
        scope: TenantScope,
        *,
        error_code: str,
        now: datetime,
    ) -> ModelFatalLatchStatus: ...


class InMemoryModelFatalLatch:
    """Process-local implementation for unit tests and non-production composition."""

    __slots__ = ("_lock", "_statuses")

    def __init__(self) -> None:
        self._statuses: dict[tuple[str, str], ModelFatalLatchStatus] = {}
        self._lock = RLock()

    def status(self, scope: TenantScope) -> ModelFatalLatchStatus:
        with self._lock:
            return self._statuses.get(
                (scope.site_id, scope.processing_purpose),
                ModelFatalLatchStatus(tripped=False, error_code=None, latched_at=None),
            )

    def assert_open(self, scope: TenantScope) -> None:
        if self.status(scope).tripped:
            raise ModelFatalLatchError()

    @contextmanager
    def egress_guard(self, scope: TenantScope) -> Iterator[None]:
        with self._lock:
            self.assert_open(scope)
            yield

    def trip(
        self,
        scope: TenantScope,
        *,
        error_code: str,
        now: datetime,
    ) -> ModelFatalLatchStatus:
        _fatal_code(error_code)
        _require_aware(now, "now")
        with self._lock:
            key = (scope.site_id, scope.processing_purpose)
            status = self._statuses.get(key)
            if status is None:
                status = ModelFatalLatchStatus(
                    tripped=True,
                    error_code=error_code,
                    latched_at=now,
                )
                self._statuses[key] = status
            return status

    def __repr__(self) -> str:
        return "InMemoryModelFatalLatch(statuses=<redacted>)"


class PostgresModelFatalLatchRepository:
    """RLS-scoped immutable latch persisted across worker restarts."""

    __slots__ = ("_connection",)

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def __repr__(self) -> str:
        return "PostgresModelFatalLatchRepository(connection=<redacted>)"

    def status(self, scope: TenantScope) -> ModelFatalLatchStatus:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                """
                SELECT error_code, latched_at
                FROM observer.model_fatal_latches
                WHERE site_id = %s AND processing_purpose = %s
                """,
                (scope.site_id, scope.processing_purpose),
            )
            row = cursor.fetchone()
        if row is None:
            return ModelFatalLatchStatus(
                tripped=False,
                error_code=None,
                latched_at=None,
            )
        return ModelFatalLatchStatus(
            tripped=True,
            error_code=str(row[0]),
            latched_at=row[1],
        )

    def assert_open(self, scope: TenantScope) -> None:
        if self.status(scope).tripped:
            raise ModelFatalLatchError()

    @contextmanager
    def egress_guard(self, scope: TenantScope) -> Iterator[None]:
        acquired = False
        try:
            with self._connection.transaction(), self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s || chr(31) || %s, 0))",
                    (scope.site_id, scope.processing_purpose),
                )
                acquired = True
            self.assert_open(scope)
            yield
        finally:
            if acquired:
                with self._connection.transaction(), self._connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s || chr(31) || %s, 0))",
                        (scope.site_id, scope.processing_purpose),
                    )

    def trip(
        self,
        scope: TenantScope,
        *,
        error_code: str,
        now: datetime,
    ) -> ModelFatalLatchStatus:
        _fatal_code(error_code)
        _require_aware(now, "now")
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s || chr(31) || %s, 0))",
                (scope.site_id, scope.processing_purpose),
            )
            cursor.execute(
                """
                INSERT INTO observer.model_fatal_latches (
                    site_id, processing_purpose, error_code, latched_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (site_id, processing_purpose) DO NOTHING
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    error_code,
                    now,
                ),
            )
            cursor.execute(
                """
                SELECT error_code, latched_at
                FROM observer.model_fatal_latches
                WHERE site_id = %s AND processing_purpose = %s
                """,
                (scope.site_id, scope.processing_purpose),
            )
            row = cursor.fetchone()
        if row is None:
            raise ModelFatalLatchError("model_fatal_latch_unavailable")
        return ModelFatalLatchStatus(
            tripped=True,
            error_code=str(row[0]),
            latched_at=row[1],
        )

    @staticmethod
    def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (scope.site_id,))
        cursor.execute(
            "SELECT set_config('app.processing_purpose', %s, true)",
            (scope.processing_purpose,),
        )


def is_fatal_model_error_code(error_code: str) -> bool:
    return error_code in FATAL_MODEL_ERROR_CODES


def sanitized_provider_error_code(error: BaseException) -> str:
    candidate = getattr(error, "error_code", None)
    if isinstance(candidate, str) and _SAFE_CODE.fullmatch(candidate) is not None:
        return candidate
    return "model_provider_failed"


def _fatal_code(error_code: str) -> None:
    if error_code not in FATAL_MODEL_ERROR_CODES:
        raise ValueError("error code is not a frozen model failure")


__all__ = [
    "FATAL_MODEL_ERROR_CODES",
    "InMemoryModelFatalLatch",
    "ModelFatalLatch",
    "ModelFatalLatchError",
    "ModelFatalLatchStatus",
    "PostgresModelFatalLatchRepository",
    "is_fatal_model_error_code",
    "sanitized_provider_error_code",
]
