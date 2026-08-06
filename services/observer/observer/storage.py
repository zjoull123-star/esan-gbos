from __future__ import annotations

import hashlib
import json
import re
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from .models import (
    CanonicalObservation,
    EntityResolutionProposal,
    EvidenceRecord,
    FactProposal,
    ImportResult,
    TenantScope,
    stable_ulid,
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class IdempotencyConflict(ValueError):
    """The same site-local idempotency key was reused for another payload."""


class CheckpointDisposition(StrEnum):
    ADVANCE = "advance"
    LATE_WITHIN_WINDOW = "late_within_window"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    site_id: str
    processing_purpose: str
    job_id: str
    status: str
    event_id: str | None
    connector: str
    provider_event_id: str | None
    raw_sha256: str
    occurred_at: datetime
    ingested_at: datetime
    evidence_ids: tuple[str, ...]
    checkpoint_id: str
    checkpoint_disposition: str
    dead_letter_reason: str | None = None


class ObserverPersistence(Protocol):
    def persist(
        self,
        scope: TenantScope,
        *,
        idempotency_key: str,
        payload_digest: str,
        result: ImportResult,
        provider_event_id: str | None,
        checkpoint_id: str,
        replay_window_seconds: int,
    ) -> ObservationMetadata: ...


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


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
        raise RuntimeError("psycopg is required for the Gate 3 Observer runtime") from exc
    return psycopg.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
    )


def utc_minute(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deduplication time must be timezone-aware")
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def fallback_dedup_key(
    scope: TenantScope,
    connector: str,
    raw_sha256: str,
    occurred_at: datetime,
) -> str:
    if not connector:
        raise ValueError("connector is required")
    if not _SHA256.fullmatch(raw_sha256):
        raise ValueError("raw_sha256 must be lowercase hexadecimal")
    material = "\x1f".join(
        (
            scope.site_id,
            connector,
            raw_sha256,
            utc_minute(occurred_at).isoformat(),
        )
    ).encode()
    return hashlib.sha256(material).hexdigest()


def classify_checkpoint(
    cursor_occurred_at: datetime | None,
    event_occurred_at: datetime,
    replay_window_seconds: int,
) -> CheckpointDisposition:
    if replay_window_seconds < 0:
        raise ValueError("replay_window_seconds must be non-negative")
    if event_occurred_at.tzinfo is None or event_occurred_at.utcoffset() is None:
        raise ValueError("event_occurred_at must be timezone-aware")
    if cursor_occurred_at is not None and (
        cursor_occurred_at.tzinfo is None or cursor_occurred_at.utcoffset() is None
    ):
        raise ValueError("cursor_occurred_at must be timezone-aware")
    utc_event = event_occurred_at.astimezone(UTC)
    if cursor_occurred_at is None:
        return CheckpointDisposition.ADVANCE
    utc_cursor = cursor_occurred_at.astimezone(UTC)
    if utc_event >= utc_cursor:
        return CheckpointDisposition.ADVANCE
    if utc_event >= utc_cursor - timedelta(seconds=replay_window_seconds):
        return CheckpointDisposition.LATE_WITHIN_WINDOW
    return CheckpointDisposition.DEAD_LETTER


class PostgresObserverRepository:
    """Persist accepted Observer output behind mandatory PostgreSQL RLS scope."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def persist(
        self,
        scope: TenantScope,
        *,
        idempotency_key: str,
        payload_digest: str,
        result: ImportResult,
        provider_event_id: str | None,
        checkpoint_id: str,
        replay_window_seconds: int,
    ) -> ObservationMetadata:
        self._validate_write(
            scope=scope,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            result=result,
            provider_event_id=provider_event_id,
            checkpoint_id=checkpoint_id,
            replay_window_seconds=replay_window_seconds,
        )
        observation = result.observation
        job_id = stable_ulid("manual-import-job", scope.site_id, idempotency_key)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            self._lock(cursor, "idempotency", scope.site_id, idempotency_key)
            existing_job = self._find_idempotency(cursor, scope, idempotency_key)
            if existing_job is not None:
                if (
                    str(existing_job[0]) != payload_digest
                    or str(existing_job[5]) != scope.processing_purpose
                ):
                    raise IdempotencyConflict("idempotency_conflict")
                return self._metadata_for_existing_job(
                    cursor=cursor,
                    scope=scope,
                    job_id=job_id,
                    checkpoint_id=checkpoint_id,
                    existing=existing_job,
                    fallback_observation=observation,
                    provider_event_id=provider_event_id,
                )

            dedup_key = (
                f"provider:{provider_event_id}"
                if provider_event_id is not None
                else "fallback:"
                + fallback_dedup_key(
                    scope,
                    observation.connector,
                    observation.raw_sha256,
                    observation.occurred_at,
                )
            )
            self._lock(cursor, "dedup", scope.site_id, observation.connector, dedup_key)
            duplicate_event_id = self._find_duplicate(
                cursor,
                scope=scope,
                observation=observation,
                provider_event_id=provider_event_id,
            )
            if duplicate_event_id is not None:
                self._insert_job(
                    cursor,
                    scope=scope,
                    job_id=job_id,
                    idempotency_key=idempotency_key,
                    payload_digest=payload_digest,
                    status="duplicate",
                    event_id=duplicate_event_id,
                    checkpoint_disposition="duplicate",
                )
                duplicate_metadata = self._load_event_metadata(
                    cursor=cursor,
                    scope=scope,
                    event_id=duplicate_event_id,
                    job_id=job_id,
                    status="duplicate",
                    checkpoint_id=checkpoint_id,
                    checkpoint_disposition="duplicate",
                )
                if duplicate_metadata is None:
                    raise RuntimeError("deduplicated Observer event metadata is missing")
                return duplicate_metadata

            self._lock(
                cursor,
                "checkpoint",
                scope.site_id,
                observation.connector,
            )
            checkpoint = self._load_checkpoint(
                cursor,
                scope=scope,
                connector=observation.connector,
            )
            cursor_time = None if checkpoint is None else checkpoint[0]
            window = replay_window_seconds if checkpoint is None else int(checkpoint[1])
            disposition = classify_checkpoint(
                cursor_time,
                observation.occurred_at,
                window,
            )
            if disposition is CheckpointDisposition.DEAD_LETTER:
                self._insert_job(
                    cursor,
                    scope=scope,
                    job_id=job_id,
                    idempotency_key=idempotency_key,
                    payload_digest=payload_digest,
                    status="dead_letter",
                    event_id=None,
                    checkpoint_disposition=disposition.value,
                )
                dead_letter_id = stable_ulid(
                    "observer-dead-letter",
                    scope.site_id,
                    observation.connector,
                    idempotency_key,
                )
                cursor.execute(
                    """
                    INSERT INTO observer.dead_letter (
                        site_id, dead_letter_id, event_id, job_id,
                        reason_code, attempts, last_error_at
                    ) VALUES (%s, %s, NULL, %s, %s, 0, %s)
                    """,
                    (
                        scope.site_id,
                        dead_letter_id,
                        job_id,
                        "outside_replay_window",
                        observation.ingested_at,
                    ),
                )
                return ObservationMetadata(
                    site_id=scope.site_id,
                    processing_purpose=scope.processing_purpose,
                    job_id=job_id,
                    status="dead_letter",
                    event_id=None,
                    connector=observation.connector,
                    provider_event_id=provider_event_id,
                    raw_sha256=observation.raw_sha256,
                    occurred_at=observation.occurred_at,
                    ingested_at=observation.ingested_at,
                    evidence_ids=(),
                    checkpoint_id=checkpoint_id,
                    checkpoint_disposition=disposition.value,
                    dead_letter_reason="outside_replay_window",
                )

            self._insert_job(
                cursor,
                scope=scope,
                job_id=job_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
                status="processing",
                event_id=None,
                checkpoint_disposition=disposition.value,
            )
            object_ids = self._persist_raw_objects(cursor, scope, result.evidence)
            self._persist_observation(
                cursor,
                scope=scope,
                job_id=job_id,
                observation=observation,
                provider_event_id=provider_event_id,
                raw_object_id=object_ids[0],
            )
            self._persist_participants(cursor, scope, observation)
            self._persist_evidence(cursor, scope, result.evidence, object_ids)
            self._persist_processor_lineage(cursor, scope, result)
            self._advance_checkpoint(
                cursor,
                scope=scope,
                checkpoint_id=checkpoint_id,
                observation=observation,
                replay_window_seconds=window,
            )
            cursor.execute(
                """
                UPDATE observer.manual_import_jobs
                SET status = 'stored', result_event_id = %s
                WHERE site_id = %s AND job_id = %s
                """,
                (observation.event_id, scope.site_id, job_id),
            )
            return ObservationMetadata(
                site_id=scope.site_id,
                processing_purpose=scope.processing_purpose,
                job_id=job_id,
                status="stored",
                event_id=observation.event_id,
                connector=observation.connector,
                provider_event_id=provider_event_id,
                raw_sha256=observation.raw_sha256,
                occurred_at=observation.occurred_at,
                ingested_at=observation.ingested_at,
                evidence_ids=observation.evidence_refs,
                checkpoint_id=checkpoint_id,
                checkpoint_disposition=disposition.value,
            )

    def get(
        self,
        scope: TenantScope,
        event_id: str,
    ) -> ObservationMetadata | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_scope(cursor, scope)
            return self._load_event_metadata(
                cursor=cursor,
                scope=scope,
                event_id=event_id,
                job_id="",
                status="stored",
                checkpoint_id="",
                checkpoint_disposition="",
            )

    @staticmethod
    def _set_scope(cursor: Cursor, scope: TenantScope) -> None:
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (scope.site_id,),
        )

    @staticmethod
    def _lock(cursor: Cursor, *parts: str) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("\x1f".join(parts),),
        )

    @staticmethod
    def _find_idempotency(
        cursor: Cursor,
        scope: TenantScope,
        idempotency_key: str,
    ) -> tuple[Any, ...] | None:
        cursor.execute(
            """
            SELECT
                job.payload_sha256,
                job.status,
                job.result_event_id,
                dead.reason_code,
                job.checkpoint_disposition,
                job.processing_purpose
            FROM observer.manual_import_jobs AS job
            LEFT JOIN observer.dead_letter AS dead
              ON dead.site_id = job.site_id AND dead.job_id = job.job_id
            WHERE job.site_id = %s AND job.idempotency_key = %s
            """,
            (scope.site_id, idempotency_key),
        )
        return cursor.fetchone()

    @staticmethod
    def _find_duplicate(
        cursor: Cursor,
        *,
        scope: TenantScope,
        observation: CanonicalObservation,
        provider_event_id: str | None,
    ) -> str | None:
        if provider_event_id is not None:
            cursor.execute(
                """
                SELECT event_id
                FROM observer.observation_events
                WHERE site_id = %s
                  AND connector = %s
                  AND provider_event_id = %s
                """,
                (scope.site_id, observation.connector, provider_event_id),
            )
        else:
            cursor.execute(
                """
                SELECT event_id
                FROM observer.observation_events
                WHERE site_id = %s
                  AND connector = %s
                  AND provider_event_id IS NULL
                  AND raw_sha256 = %s
                  AND occurred_minute = %s
                """,
                (
                    scope.site_id,
                    observation.connector,
                    observation.raw_sha256,
                    utc_minute(observation.occurred_at),
                ),
            )
        row = cursor.fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _load_checkpoint(
        cursor: Cursor,
        *,
        scope: TenantScope,
        connector: str,
    ) -> tuple[Any, ...] | None:
        cursor.execute(
            """
            SELECT cursor_occurred_at, replay_window_seconds
            FROM observer.checkpoints
            WHERE site_id = %s AND connector = %s
            FOR UPDATE
            """,
            (scope.site_id, connector),
        )
        return cursor.fetchone()

    @staticmethod
    def _insert_job(
        cursor: Cursor,
        *,
        scope: TenantScope,
        job_id: str,
        idempotency_key: str,
        payload_digest: str,
        status: str,
        event_id: str | None,
        checkpoint_disposition: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO observer.manual_import_jobs (
                site_id, job_id, processing_purpose, idempotency_key,
                payload_sha256, status, result_event_id, checkpoint_disposition
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scope.site_id,
                job_id,
                scope.processing_purpose,
                idempotency_key,
                payload_digest,
                status,
                event_id,
                checkpoint_disposition,
            ),
        )

    @staticmethod
    def _persist_raw_objects(
        cursor: Cursor,
        scope: TenantScope,
        evidence: tuple[EvidenceRecord, ...],
    ) -> tuple[str, ...]:
        object_ids: list[str] = []
        for record in evidence:
            cursor.execute(
                """
                SELECT object_id
                FROM observer.raw_objects
                WHERE site_id = %s AND sha256 = %s
                """,
                (scope.site_id, record.raw_sha256),
            )
            row = cursor.fetchone()
            if row is None:
                object_id = stable_ulid(
                    "observer-raw-object",
                    scope.site_id,
                    record.raw_sha256,
                )
                cursor.execute(
                    """
                    INSERT INTO observer.raw_objects (
                        site_id, object_id, object_ref, sha256, media_type,
                        byte_size, retention_class, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scope.site_id,
                        object_id,
                        record.object_ref,
                        record.raw_sha256,
                        record.media_type,
                        record.locator.end,
                        record.retention_class,
                        record.created_at,
                    ),
                )
            else:
                object_id = str(row[0])
            object_ids.append(object_id)
        return tuple(object_ids)

    @staticmethod
    def _persist_observation(
        cursor: Cursor,
        *,
        scope: TenantScope,
        job_id: str,
        observation: CanonicalObservation,
        provider_event_id: str | None,
        raw_object_id: str,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO observer.observation_events (
                site_id, event_id, job_id, raw_object_id, provider_event_id,
                connector, channel, processing_purpose, consent_basis,
                data_classification, retention_class, correlation_id,
                occurred_at, ingested_at, document, raw_sha256, occurred_minute
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s::jsonb, %s, %s
            )
            """,
            (
                scope.site_id,
                observation.event_id,
                job_id,
                raw_object_id,
                provider_event_id,
                observation.connector,
                observation.channel,
                scope.processing_purpose,
                observation.consent_basis,
                observation.data_classification,
                observation.retention_class,
                observation.correlation_id,
                observation.occurred_at,
                observation.ingested_at,
                _json_document(observation),
                observation.raw_sha256,
                utc_minute(observation.occurred_at),
            ),
        )

    @staticmethod
    def _persist_participants(
        cursor: Cursor,
        scope: TenantScope,
        observation: CanonicalObservation,
    ) -> None:
        for index, participant in enumerate(observation.participants):
            participant_id = stable_ulid(
                "observer-participant",
                scope.site_id,
                observation.event_id,
                str(index),
                participant.identity_ref,
            )
            cursor.execute(
                """
                INSERT INTO observer.participants (
                    site_id, event_id, participant_id, role,
                    identity_ref, display_name
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    scope.site_id,
                    observation.event_id,
                    participant_id,
                    participant.role,
                    participant.identity_ref,
                    participant.display_name,
                ),
            )

    @staticmethod
    def _persist_evidence(
        cursor: Cursor,
        scope: TenantScope,
        evidence: tuple[EvidenceRecord, ...],
        object_ids: tuple[str, ...],
    ) -> None:
        for ordinal, (record, object_id) in enumerate(zip(evidence, object_ids, strict=True)):
            cursor.execute(
                """
                INSERT INTO observer.evidence_refs (
                    site_id, evidence_id, event_id, raw_object_id,
                    raw_sha256, media_type, locator, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    scope.site_id,
                    record.evidence_id,
                    record.observation_event_id,
                    object_id,
                    record.raw_sha256,
                    record.media_type,
                    json.dumps(
                        {"message_start": record.locator.start, "message_end": record.locator.end}
                    ),
                    record.created_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.event_evidence (
                    site_id, event_id, evidence_id, evidence_ordinal
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    scope.site_id,
                    record.observation_event_id,
                    record.evidence_id,
                    ordinal,
                ),
            )

    def _persist_processor_lineage(
        self,
        cursor: Cursor,
        scope: TenantScope,
        result: ImportResult,
    ) -> None:
        for fact_proposal in result.fact_proposals:
            self._persist_proposal_lineage(
                cursor,
                scope=scope,
                event_id=result.observation.event_id,
                proposal=fact_proposal,
                derived_type="fact_proposal",
                derived_id=fact_proposal.fact_id,
            )
        for entity_proposal in result.entity_resolution_proposals:
            self._persist_proposal_lineage(
                cursor,
                scope=scope,
                event_id=result.observation.event_id,
                proposal=entity_proposal,
                derived_type="entity_resolution_proposal",
                derived_id=entity_proposal.proposal_id,
            )

    @staticmethod
    def _persist_proposal_lineage(
        cursor: Cursor,
        *,
        scope: TenantScope,
        event_id: str,
        proposal: FactProposal | EntityResolutionProposal,
        derived_type: str,
        derived_id: str,
    ) -> None:
        run_id = stable_ulid(
            "observer-processor-run",
            scope.site_id,
            event_id,
            derived_type,
            derived_id,
        )
        cursor.execute(
            """
            INSERT INTO observer.processor_runs (
                site_id, processor_run_id, event_id, processor_id,
                processor_version, rule_version, output_version,
                network_calls, tool_calls, started_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0, %s, %s)
            """,
            (
                scope.site_id,
                run_id,
                event_id,
                "deterministic_observer",
                proposal.processor_version,
                proposal.rule_version,
                proposal.output_version,
                proposal.recorded_at,
                proposal.recorded_at,
            ),
        )
        for evidence_id in proposal.evidence_refs:
            edge_id = stable_ulid(
                "observer-derivation-edge",
                scope.site_id,
                evidence_id,
                derived_type,
                derived_id,
            )
            cursor.execute(
                """
                INSERT INTO observer.derivation_edges (
                    site_id, derivation_edge_id, source_type, source_id,
                    derived_type, derived_id, processor_run_id, created_at
                ) VALUES (%s, %s, 'evidence', %s, %s, %s, %s, %s)
                """,
                (
                    scope.site_id,
                    edge_id,
                    evidence_id,
                    derived_type,
                    derived_id,
                    run_id,
                    proposal.recorded_at,
                ),
            )

    @staticmethod
    def _advance_checkpoint(
        cursor: Cursor,
        *,
        scope: TenantScope,
        checkpoint_id: str,
        observation: CanonicalObservation,
        replay_window_seconds: int,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO observer.checkpoints (
                site_id, checkpoint_id, connector, cursor_value,
                cursor_occurred_at, last_event_id, replay_window_seconds,
                status, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'healthy', %s)
            ON CONFLICT (site_id, connector) DO UPDATE SET
                cursor_value = CASE
                    WHEN observer.checkpoints.cursor_occurred_at IS NULL
                      OR EXCLUDED.cursor_occurred_at >= observer.checkpoints.cursor_occurred_at
                    THEN EXCLUDED.cursor_value
                    ELSE observer.checkpoints.cursor_value
                END,
                last_event_id = CASE
                    WHEN observer.checkpoints.cursor_occurred_at IS NULL
                      OR EXCLUDED.cursor_occurred_at >= observer.checkpoints.cursor_occurred_at
                    THEN EXCLUDED.last_event_id
                    ELSE observer.checkpoints.last_event_id
                END,
                cursor_occurred_at = CASE
                    WHEN observer.checkpoints.cursor_occurred_at IS NULL
                      OR EXCLUDED.cursor_occurred_at >= observer.checkpoints.cursor_occurred_at
                    THEN EXCLUDED.cursor_occurred_at
                    ELSE observer.checkpoints.cursor_occurred_at
                END,
                status = 'healthy',
                updated_at = GREATEST(
                    observer.checkpoints.updated_at,
                    EXCLUDED.updated_at
                )
            """,
            (
                scope.site_id,
                checkpoint_id,
                observation.connector,
                observation.event_id,
                observation.occurred_at,
                observation.event_id,
                replay_window_seconds,
                observation.ingested_at,
            ),
        )

    def _metadata_for_existing_job(
        self,
        *,
        cursor: Cursor,
        scope: TenantScope,
        job_id: str,
        checkpoint_id: str,
        existing: tuple[Any, ...],
        fallback_observation: CanonicalObservation,
        provider_event_id: str | None,
    ) -> ObservationMetadata:
        status = str(existing[1])
        event_id = None if existing[2] is None else str(existing[2])
        dead_letter_reason = None if existing[3] is None else str(existing[3])
        checkpoint_disposition = str(existing[4])
        if event_id is not None:
            metadata = self._load_event_metadata(
                cursor=cursor,
                scope=scope,
                event_id=event_id,
                job_id=job_id,
                status=status,
                checkpoint_id=checkpoint_id,
                checkpoint_disposition=checkpoint_disposition,
            )
            if metadata is None:
                raise RuntimeError("idempotent Observer event metadata is missing")
            return metadata
        return ObservationMetadata(
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            job_id=job_id,
            status=status,
            event_id=None,
            connector=fallback_observation.connector,
            provider_event_id=provider_event_id,
            raw_sha256=fallback_observation.raw_sha256,
            occurred_at=fallback_observation.occurred_at,
            ingested_at=fallback_observation.ingested_at,
            evidence_ids=(),
            checkpoint_id=checkpoint_id,
            checkpoint_disposition=checkpoint_disposition,
            dead_letter_reason=dead_letter_reason,
        )

    @staticmethod
    def _load_event_metadata(
        *,
        cursor: Cursor,
        scope: TenantScope,
        event_id: str,
        job_id: str,
        status: str,
        checkpoint_id: str,
        checkpoint_disposition: str,
    ) -> ObservationMetadata | None:
        cursor.execute(
            """
            SELECT
                event_id, connector, provider_event_id, raw_sha256,
                occurred_at, ingested_at
            FROM observer.observation_events
            WHERE site_id = %s
              AND processing_purpose = %s
              AND event_id = %s
            """,
            (scope.site_id, scope.processing_purpose, event_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            SELECT evidence_id
            FROM observer.event_evidence
            WHERE site_id = %s AND event_id = %s
            ORDER BY evidence_ordinal, evidence_id
            """,
            (scope.site_id, event_id),
        )
        evidence_ids = tuple(str(item[0]) for item in cursor.fetchall())
        return ObservationMetadata(
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            job_id=job_id,
            status=status,
            event_id=str(row[0]),
            connector=str(row[1]),
            provider_event_id=None if row[2] is None else str(row[2]),
            raw_sha256=str(row[3]),
            occurred_at=row[4],
            ingested_at=row[5],
            evidence_ids=evidence_ids,
            checkpoint_id=checkpoint_id,
            checkpoint_disposition=checkpoint_disposition,
        )

    @staticmethod
    def _validate_write(
        *,
        scope: TenantScope,
        idempotency_key: str,
        payload_digest: str,
        result: ImportResult,
        provider_event_id: str | None,
        checkpoint_id: str,
        replay_window_seconds: int,
    ) -> None:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("invalid idempotency_key")
        if not _SHA256.fullmatch(payload_digest):
            raise ValueError("invalid payload_digest")
        if not checkpoint_id:
            raise ValueError("checkpoint_id is required")
        if replay_window_seconds < 0:
            raise ValueError("replay_window_seconds must be non-negative")
        if provider_event_id is not None and not provider_event_id:
            raise ValueError("provider_event_id cannot be empty")
        observation = result.observation
        if observation.site_id != scope.site_id:
            raise ValueError("observation site does not match TenantScope")
        if observation.processing_purpose != scope.processing_purpose:
            raise ValueError("observation purpose does not match TenantScope")
        if observation.raw_sha256 != payload_digest:
            raise ValueError("observation raw_sha256 does not match payload_digest")
        if not result.evidence:
            raise ValueError("at least one evidence record is required")
        if observation.evidence_refs != tuple(record.evidence_id for record in result.evidence):
            raise ValueError("observation evidence_refs do not match evidence records")
        for record in result.evidence:
            if (
                record.site_id != scope.site_id
                or record.processing_purpose != scope.processing_purpose
                or record.observation_event_id != observation.event_id
            ):
                raise ValueError("evidence lineage is outside TenantScope")
        if any(proposal.status != "proposed" for proposal in result.fact_proposals):
            raise ValueError("Observer persistence accepts proposed output only")
        if any(
            proposal.site_id != scope.site_id
            or proposal.processing_purpose != scope.processing_purpose
            for proposal in result.fact_proposals
        ):
            raise ValueError("proposal lineage is outside TenantScope")
        if any(proposal.status != "proposed" for proposal in result.entity_resolution_proposals):
            raise ValueError("Observer persistence accepts proposed output only")
        if any(
            proposal.site_id != scope.site_id
            or proposal.processing_purpose != scope.processing_purpose
            for proposal in result.entity_resolution_proposals
        ):
            raise ValueError("proposal lineage is outside TenantScope")


def _json_document(value: CanonicalObservation) -> str:
    return json.dumps(
        asdict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    )
