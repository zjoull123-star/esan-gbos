from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)
from .repositories import validate_governed_record


class ChecksumMismatch(RuntimeError):
    """An already-applied migration no longer matches its recorded checksum."""


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class MigrationRunner:
    """Apply immutable plain-SQL migrations using a checksum ledger."""

    def __init__(
        self,
        connection: Connection,
        migration_directories: list[Path] | tuple[Path, ...],
    ) -> None:
        self._connection = connection
        self._directories = tuple(migration_directories)

    def run(self) -> tuple[str, ...]:
        applied: list[str] = []
        self._ensure_ledger()
        for directory in self._directories:
            for path in sorted(directory.glob("*.sql")):
                name = f"{directory.parent.name}/{path.name}"
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                with self._connection.transaction(), self._connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT checksum
                        FROM observer.schema_migrations
                        WHERE migration_name = %s
                        """,
                        (name,),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        if row[0] != checksum:
                            raise ChecksumMismatch(f"applied migration checksum changed: {name}")
                        continue
                    cursor.execute(sql)
                    cursor.execute(
                        """
                        INSERT INTO observer.schema_migrations
                            (migration_name, checksum)
                        VALUES (%s, %s)
                        """,
                        (name, checksum),
                    )
                    applied.append(name)
        return tuple(applied)

    def _ensure_ledger(self) -> None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS observer")
            cursor.execute(
                """
                    CREATE TABLE IF NOT EXISTS observer.schema_migrations (
                        migration_name text PRIMARY KEY,
                        checksum char(64) NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
            )


def connect_postgres(dsn: str) -> Any:
    if not dsn:
        raise RuntimeError("a PostgreSQL DSN is required for Gate 3 integration")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for PostgreSQL integration; "
            "install psycopg before enabling GBOS_RUN_POSTGRES_INTEGRATION"
        ) from exc
    return psycopg.connect(dsn)


def connect_postgres_components(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> Any:
    if not host or not database or not user or not password:
        raise RuntimeError("complete PostgreSQL connection components are required")
    if not 1 <= port <= 65535:
        raise RuntimeError("PostgreSQL port is outside the valid range")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for the Gate 3 Context runtime") from exc
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
    )


TABLES: dict[RecordKind, tuple[str, str]] = {
    RecordKind.EVIDENCE: ("context.evidence_records", "evidence_record_id"),
    RecordKind.FACT_PROPOSAL: ("context.fact_proposals", "fact_proposal_record_id"),
    RecordKind.ENTITY_RESOLUTION_PROPOSAL: (
        "context.entity_resolution_proposals",
        "entity_resolution_proposal_id",
    ),
}


class PostgresContextRepository:
    """PostgreSQL repository with mandatory transaction-local tenant scope."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def save(
        self,
        scope: TenantScope,
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> RecordMetadata:
        record_id = validate_governed_record(scope, kind, envelope)
        table, id_column = TABLES[kind]
        child_insertions = self._child_insertions(
            scope,
            kind,
            record_id,
            envelope.payload,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            cursor.execute(
                f"""
                    SELECT '{kind.value}', {id_column}, processing_purpose,
                           idempotency_key, payload_digest, recorded_at
                    FROM {table}
                    WHERE site_id = %s AND idempotency_key = %s
                    """,
                (scope.site_id, envelope.idempotency_key),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if existing[4] != envelope.payload_digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used with a different payload"
                    )
                return self._metadata_from_row(scope.site_id, existing)
            columns, values = self._insert_fields(kind, envelope)
            placeholders = ", ".join(["%s"] * (5 + len(values)) + ["%s::jsonb"])
            cursor.execute(
                f"""
                    INSERT INTO {table}
                        (site_id, {id_column}, processing_purpose,
                         idempotency_key, payload_digest, {", ".join(columns)}, document)
                    VALUES ({placeholders})
                    RETURNING '{kind.value}', {id_column}, processing_purpose,
                              idempotency_key, payload_digest, recorded_at
                    """,
                (
                    scope.site_id,
                    record_id,
                    scope.processing_purpose,
                    envelope.idempotency_key,
                    envelope.payload_digest,
                    *values,
                    json.dumps(dict(envelope.payload), ensure_ascii=False),
                ),
            )
            created = cursor.fetchone()
            if created is None:
                raise RuntimeError("Context metadata insert returned no row")
            for sql, params in child_insertions:
                cursor.execute(sql, params)
            return self._metadata_from_row(scope.site_id, created)

    def get(
        self,
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
    ) -> RecordMetadata | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            table, id_column = TABLES[kind]
            cursor.execute(
                f"""
                    SELECT '{kind.value}', {id_column}, processing_purpose,
                           idempotency_key, payload_digest, recorded_at
                    FROM {table}
                    WHERE site_id = %s AND {id_column} = %s
                    """,
                (scope.site_id, record_id),
            )
            row = cursor.fetchone()
        return None if row is None else self._metadata_from_row(scope.site_id, row)

    @staticmethod
    def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (scope.site_id,),
        )

    @staticmethod
    def _insert_fields(
        kind: RecordKind,
        envelope: GovernedEnvelope,
    ) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        payload = envelope.payload
        if kind is RecordKind.EVIDENCE:
            evidence_ref = payload.get("evidence_ref")
            if not isinstance(evidence_ref, dict):
                raise ValidationError("evidence_ref is required")
            return (
                ("observer_evidence_id", "review_status", "data_classification"),
                (
                    evidence_ref.get("evidence_id"),
                    payload.get("review_status"),
                    payload.get("data_classification"),
                ),
            )
        if kind is RecordKind.FACT_PROPOSAL:
            fact = payload.get("fact")
            if not isinstance(fact, dict):
                raise ValidationError("fact is required")
            return (
                ("status", "subject_ref", "predicate", "confidence"),
                (
                    "proposed",
                    fact.get("subject_ref"),
                    fact.get("predicate"),
                    fact.get("confidence"),
                ),
            )
        return (
            ("status", "entity_type", "source_entity_ref"),
            (
                "proposed",
                payload.get("entity_type"),
                payload.get("source_entity_ref"),
            ),
        )

    @staticmethod
    def _child_insertions(
        scope: TenantScope,
        kind: RecordKind,
        record_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[tuple[str, tuple[Any, ...]], ...]:
        if kind is RecordKind.EVIDENCE:
            return ()
        if kind is RecordKind.FACT_PROPOSAL:
            fact = payload.get("fact")
            if not isinstance(fact, dict):
                raise ValidationError("fact is required")
            evidence_refs = fact.get("evidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs:
                raise ValidationError("fact evidence_refs must be a non-empty list")
            insertions: list[tuple[str, tuple[Any, ...]]] = []
            for evidence_record_id in evidence_refs:
                if not isinstance(evidence_record_id, str) or not evidence_record_id:
                    raise ValidationError("fact evidence_refs must contain strings")
                insertions.append(
                    (
                        """
                        INSERT INTO context.fact_evidence
                            (site_id, fact_proposal_record_id, evidence_record_id)
                        VALUES (%s, %s, %s)
                        """,
                        (scope.site_id, record_id, evidence_record_id),
                    )
                )
            return tuple(insertions)

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValidationError("entity candidates must be a non-empty list")
        candidate_insertions: list[tuple[str, tuple[Any, ...]]] = []
        for index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                raise ValidationError("entity candidates must contain objects")
            entity_ref = candidate.get("entity_ref")
            confidence = candidate.get("confidence")
            matching_attributes = candidate.get("matching_attributes")
            if not isinstance(entity_ref, str) or not entity_ref:
                raise ValidationError("candidate entity_ref must be a non-empty string")
            if not isinstance(confidence, int | float) or isinstance(confidence, bool):
                raise ValidationError("candidate confidence must be numeric")
            if (
                not isinstance(matching_attributes, list)
                or not matching_attributes
                or not all(isinstance(value, str) for value in matching_attributes)
            ):
                raise ValidationError(
                    "candidate matching_attributes must be a non-empty string list"
                )
            candidate_insertions.append(
                (
                    """
                    INSERT INTO context.candidates
                        (site_id, entity_resolution_proposal_id, candidate_id,
                         entity_ref, confidence, matching_attributes)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        scope.site_id,
                        record_id,
                        f"candidate-{index:04d}",
                        entity_ref,
                        confidence,
                        json.dumps(matching_attributes, ensure_ascii=False),
                    ),
                )
            )
        return tuple(candidate_insertions)

    @staticmethod
    def _metadata_from_row(site_id: str, row: tuple[Any, ...]) -> RecordMetadata:
        recorded_at = row[5]
        if not isinstance(recorded_at, datetime):
            raise ValidationError("database returned invalid recorded_at metadata")
        return RecordMetadata(
            kind=RecordKind(str(row[0])),
            record_id=str(row[1]),
            site_id=site_id,
            processing_purpose=str(row[2]),
            idempotency_key=str(row[3]),
            payload_digest=str(row[4]),
            recorded_at=recorded_at,
        )
