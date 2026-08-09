from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol

from .upload import SourceKind, UploadReceipt

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")
_JOB_COLUMNS = """
    job_id, site_id, request_id, receipt, work_spec, submission_digest,
    status, due_at, attempt, max_attempts, fencing_token, lease_owner,
    lease_expires_at, reason_codes, transcript_ref, artifact_proof,
    created_at, updated_at
"""


class MediaJobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRY = "retry"
    READY = "ready"
    QUARANTINED = "quarantined"
    DEAD_LETTER = "dead_letter"


class LeaseConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaArtifactProof:
    ffmpeg_output_sha256: str
    ffmpeg_executable_sha256: str
    whisper_model_sha256: str

    def __post_init__(self) -> None:
        if not all(
            _valid_sha256(value)
            for value in (
                self.ffmpeg_output_sha256,
                self.ffmpeg_executable_sha256,
                self.whisper_model_sha256,
            )
        ):
            raise ValueError("artifact_proof_invalid")


@dataclass(frozen=True, slots=True)
class MediaJobSubmission:
    receipt: UploadReceipt
    duration_ms: int
    channels: int
    sample_rate: int
    language_hint: str | None
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.duration_ms <= 7_200_000:
            raise ValueError("duration_out_of_bounds")
        if not 1 <= self.channels <= 8:
            raise ValueError("channels_out_of_bounds")
        if not 8_000 <= self.sample_rate <= 192_000:
            raise ValueError("sample_rate_out_of_bounds")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts_out_of_bounds")
        if self.language_hint is not None and not 2 <= len(self.language_hint) <= 16:
            raise ValueError("language_hint_invalid")

    @property
    def digest(self) -> str:
        document = {
            "receipt": _receipt_identity(self.receipt),
            "duration_ms": self.duration_ms,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "language_hint": self.language_hint,
            "max_attempts": self.max_attempts,
        }
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaJob:
    job_id: str
    site_id: str
    request_id: str
    receipt: UploadReceipt
    duration_ms: int
    channels: int
    sample_rate: int
    language_hint: str | None
    submission_digest: str
    status: MediaJobStatus
    due_at: datetime
    attempt: int
    max_attempts: int
    fencing_token: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    reason_codes: tuple[str, ...]
    transcript_ref: str | None
    artifact_proof: MediaArtifactProof | None
    created_at: datetime
    updated_at: datetime

    def to_summary(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
        }


class MediaJobRepository(Protocol):
    def enqueue(self, submission: MediaJobSubmission, *, now: datetime) -> MediaJob: ...

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob | None: ...

    def heartbeat(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob: ...

    def complete(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        status: MediaJobStatus,
        reason_codes: tuple[str, ...],
        transcript_ref: str | None,
        artifact_proof: MediaArtifactProof | None,
    ) -> MediaJob: ...

    def retry(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        retry_at: datetime,
        reason_codes: tuple[str, ...],
    ) -> MediaJob: ...


class InMemoryMediaJobRepository:
    """Deterministic test repository matching the PostgreSQL fencing contract."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], MediaJob] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def enqueue(self, submission: MediaJobSubmission, *, now: datetime) -> MediaJob:
        _require_aware(now)
        request_key = (submission.receipt.site_id, submission.receipt.request_id)
        with self._lock:
            existing_id = self._requests.get(request_key)
            if existing_id is not None:
                existing = self._jobs[(submission.receipt.site_id, existing_id)]
                if existing.submission_digest != submission.digest:
                    raise ValueError("idempotency_conflict")
                return replace(existing)
            job_id = _job_id(submission)
            job = MediaJob(
                job_id=job_id,
                site_id=submission.receipt.site_id,
                request_id=submission.receipt.request_id,
                receipt=submission.receipt,
                duration_ms=submission.duration_ms,
                channels=submission.channels,
                sample_rate=submission.sample_rate,
                language_hint=submission.language_hint,
                submission_digest=submission.digest,
                status=MediaJobStatus.QUEUED,
                due_at=now,
                attempt=0,
                max_attempts=submission.max_attempts,
                fencing_token=0,
                lease_owner=None,
                lease_expires_at=None,
                reason_codes=(),
                transcript_ref=None,
                artifact_proof=None,
                created_at=now,
                updated_at=now,
            )
            self._jobs[(job.site_id, job.job_id)] = job
            self._requests[request_key] = job.job_id
            return replace(job)

    def get(self, site_id: str, job_id: str) -> MediaJob | None:
        with self._lock:
            job = self._jobs.get((site_id, job_id))
            return None if job is None else replace(job)

    def get_by_request(self, site_id: str, request_id: str) -> MediaJob | None:
        with self._lock:
            job_id = self._requests.get((site_id, request_id))
            if job_id is None:
                return None
            return replace(self._jobs[(site_id, job_id)])

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob | None:
        _validate_lease_input(site_id, worker_id, now, lease_duration)
        with self._lock:
            self._reap_exhausted(site_id, now)
            candidates = [
                job
                for (candidate_site, _), job in self._jobs.items()
                if candidate_site == site_id
                and job.attempt < job.max_attempts
                and (
                    (
                        job.status in {MediaJobStatus.QUEUED, MediaJobStatus.RETRY}
                        and job.due_at <= now
                    )
                    or (
                        job.status is MediaJobStatus.LEASED
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= now
                    )
                )
            ]
            if not candidates:
                return None
            current = min(candidates, key=lambda job: (job.due_at, job.created_at, job.job_id))
            claimed = replace(
                current,
                status=MediaJobStatus.LEASED,
                attempt=current.attempt + 1,
                fencing_token=current.fencing_token + 1,
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
                updated_at=now,
            )
            self._jobs[(site_id, claimed.job_id)] = claimed
            return replace(claimed)

    def heartbeat(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob:
        _validate_lease_input(site_id, worker_id, now, lease_duration)
        with self._lock:
            current = self._live_lease(
                site_id,
                job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                now=now,
            )
            renewed = replace(
                current,
                lease_expires_at=now + lease_duration,
                updated_at=now,
            )
            self._jobs[(site_id, job_id)] = renewed
            return replace(renewed)

    def complete(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        status: MediaJobStatus,
        reason_codes: tuple[str, ...],
        transcript_ref: str | None,
        artifact_proof: MediaArtifactProof | None,
    ) -> MediaJob:
        _require_aware(now)
        if status not in {
            MediaJobStatus.READY,
            MediaJobStatus.QUARANTINED,
            MediaJobStatus.DEAD_LETTER,
        }:
            raise ValueError("terminal_status_invalid")
        _validate_reason_codes(reason_codes)
        with self._lock:
            current = self._live_lease(
                site_id,
                job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                now=now,
            )
            completed = replace(
                current,
                status=status,
                lease_owner=None,
                lease_expires_at=None,
                reason_codes=reason_codes,
                transcript_ref=transcript_ref,
                artifact_proof=artifact_proof,
                updated_at=now,
            )
            self._jobs[(site_id, job_id)] = completed
            return replace(completed)

    def retry(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        retry_at: datetime,
        reason_codes: tuple[str, ...],
    ) -> MediaJob:
        _require_aware(now)
        _require_aware(retry_at)
        if retry_at <= now:
            raise ValueError("retry_at_invalid")
        _validate_reason_codes(reason_codes)
        with self._lock:
            current = self._live_lease(
                site_id,
                job_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                now=now,
            )
            status = (
                MediaJobStatus.RETRY
                if current.attempt < current.max_attempts
                else MediaJobStatus.DEAD_LETTER
            )
            updated = replace(
                current,
                status=status,
                due_at=retry_at if status is MediaJobStatus.RETRY else current.due_at,
                lease_owner=None,
                lease_expires_at=None,
                reason_codes=reason_codes,
                updated_at=now,
            )
            self._jobs[(site_id, job_id)] = updated
            return replace(updated)

    def _live_lease(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
    ) -> MediaJob:
        current = self._jobs.get((site_id, job_id))
        if (
            current is None
            or current.status is not MediaJobStatus.LEASED
            or current.lease_owner != worker_id
            or current.fencing_token != fencing_token
            or current.lease_expires_at is None
            or current.lease_expires_at <= now
        ):
            raise LeaseConflict("lease_lost")
        return current

    def _reap_exhausted(self, site_id: str, now: datetime) -> None:
        for key, job in tuple(self._jobs.items()):
            if (
                key[0] == site_id
                and job.status is MediaJobStatus.LEASED
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
                and job.attempt >= job.max_attempts
            ):
                self._jobs[key] = replace(
                    job,
                    status=MediaJobStatus.DEAD_LETTER,
                    lease_owner=None,
                    lease_expires_at=None,
                    reason_codes=("lease_expired_max_attempts",),
                    updated_at=now,
                )


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


class Connection(Protocol):
    def transaction(self) -> AbstractContextManager[Any]: ...

    def cursor(self) -> Cursor: ...


class PostgresMediaJobRepository:
    """PostgreSQL repository with site scoping, lease fencing and replay safety."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def enqueue(self, submission: MediaJobSubmission, *, now: datetime) -> MediaJob:
        _require_aware(now)
        receipt_json = json.dumps(submission.receipt.to_contract(), sort_keys=True)
        work_spec_json = json.dumps(
            {
                "duration_ms": submission.duration_ms,
                "channels": submission.channels,
                "sample_rate": submission.sample_rate,
                "language_hint": submission.language_hint,
            },
            sort_keys=True,
        )
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, submission.receipt.site_id)
            cursor.execute(
                f"""
                INSERT INTO media_runtime.local_media_jobs (
                    job_id, site_id, request_id, receipt, work_spec,
                    submission_digest, status, due_at, attempt, max_attempts,
                    fencing_token, reason_codes, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s::jsonb, %s::jsonb, %s, 'queued',
                    %s, 0, %s, 0, '[]'::jsonb, %s, %s
                )
                ON CONFLICT (site_id, request_id) DO NOTHING
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    _job_id(submission),
                    submission.receipt.site_id,
                    submission.receipt.request_id,
                    receipt_json,
                    work_spec_json,
                    submission.digest,
                    now,
                    submission.max_attempts,
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    f"""
                    SELECT {_JOB_COLUMNS}
                    FROM media_runtime.local_media_jobs
                    WHERE site_id = %s AND request_id = %s
                    """,
                    (submission.receipt.site_id, submission.receipt.request_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("idempotency_conflict")
                existing = _job_from_row(row)
                if existing.submission_digest != submission.digest:
                    raise ValueError("idempotency_conflict")
                return existing
            return _job_from_row(row)

    def get(self, site_id: str, job_id: str) -> MediaJob | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM media_runtime.local_media_jobs
                WHERE site_id = %s AND job_id = %s
                """,
                (site_id, job_id),
            )
            row = cursor.fetchone()
            return None if row is None else _job_from_row(row)

    def get_by_request(self, site_id: str, request_id: str) -> MediaJob | None:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM media_runtime.local_media_jobs
                WHERE site_id = %s AND request_id = %s
                """,
                (site_id, request_id),
            )
            row = cursor.fetchone()
            return None if row is None else _job_from_row(row)

    def claim(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob | None:
        _validate_lease_input(site_id, worker_id, now, lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                """
                UPDATE media_runtime.local_media_jobs
                SET status = 'dead_letter',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    reason_codes = '["lease_expired_max_attempts"]'::jsonb,
                    updated_at = %s
                WHERE site_id = %s
                  AND status = 'leased'
                  AND lease_expires_at <= %s
                  AND attempt >= max_attempts
                """,
                (now, site_id, now),
            )
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT site_id, job_id
                    FROM media_runtime.local_media_jobs
                    WHERE site_id = %s
                      AND attempt < max_attempts
                      AND (
                        (status IN ('queued', 'retry') AND due_at <= %s)
                        OR (status = 'leased' AND lease_expires_at <= %s)
                      )
                    ORDER BY due_at, created_at, job_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE media_runtime.local_media_jobs AS job
                SET status = 'leased',
                    attempt = job.attempt + 1,
                    fencing_token = job.fencing_token + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    updated_at = %s
                FROM candidate
                WHERE job.site_id = candidate.site_id
                  AND job.job_id = candidate.job_id
                RETURNING {_qualified_columns("job")}
                """,
                (site_id, now, now, worker_id, now + lease_duration, now),
            )
            row = cursor.fetchone()
            return None if row is None else _job_from_row(row)

    def heartbeat(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> MediaJob:
        _validate_lease_input(site_id, worker_id, now, lease_duration)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE media_runtime.local_media_jobs
                SET lease_expires_at = %s, updated_at = %s
                WHERE site_id = %s AND job_id = %s
                  AND status = 'leased'
                  AND lease_owner = %s
                  AND fencing_token = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    now + lease_duration,
                    now,
                    site_id,
                    job_id,
                    worker_id,
                    fencing_token,
                    now,
                ),
            )
            return self._required_lease_row(cursor)

    def complete(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        status: MediaJobStatus,
        reason_codes: tuple[str, ...],
        transcript_ref: str | None,
        artifact_proof: MediaArtifactProof | None,
    ) -> MediaJob:
        _require_aware(now)
        if status not in {
            MediaJobStatus.READY,
            MediaJobStatus.QUARANTINED,
            MediaJobStatus.DEAD_LETTER,
        }:
            raise ValueError("terminal_status_invalid")
        _validate_reason_codes(reason_codes)
        proof_json = None if artifact_proof is None else json.dumps(asdict(artifact_proof))
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE media_runtime.local_media_jobs
                SET status = %s,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    reason_codes = %s::jsonb,
                    transcript_ref = %s,
                    artifact_proof = %s::jsonb,
                    updated_at = %s
                WHERE site_id = %s AND job_id = %s
                  AND status = 'leased'
                  AND lease_owner = %s
                  AND fencing_token = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    status.value,
                    json.dumps(reason_codes),
                    transcript_ref,
                    proof_json,
                    now,
                    site_id,
                    job_id,
                    worker_id,
                    fencing_token,
                    now,
                ),
            )
            return self._required_lease_row(cursor)

    def retry(
        self,
        site_id: str,
        job_id: str,
        *,
        worker_id: str,
        fencing_token: int,
        now: datetime,
        retry_at: datetime,
        reason_codes: tuple[str, ...],
    ) -> MediaJob:
        _require_aware(now)
        _require_aware(retry_at)
        if retry_at <= now:
            raise ValueError("retry_at_invalid")
        _validate_reason_codes(reason_codes)
        with self._connection.transaction(), self._connection.cursor() as cursor:
            self._set_site(cursor, site_id)
            cursor.execute(
                f"""
                UPDATE media_runtime.local_media_jobs
                SET status = CASE
                      WHEN attempt < max_attempts THEN 'retry'
                      ELSE 'dead_letter'
                    END,
                    due_at = CASE
                      WHEN attempt < max_attempts THEN %s
                      ELSE due_at
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    reason_codes = %s::jsonb,
                    updated_at = %s
                WHERE site_id = %s AND job_id = %s
                  AND status = 'leased'
                  AND lease_owner = %s
                  AND fencing_token = %s
                  AND lease_expires_at > %s
                RETURNING {_JOB_COLUMNS}
                """,
                (
                    retry_at,
                    json.dumps(reason_codes),
                    now,
                    site_id,
                    job_id,
                    worker_id,
                    fencing_token,
                    now,
                ),
            )
            return self._required_lease_row(cursor)

    @staticmethod
    def _set_site(cursor: Cursor, site_id: str) -> None:
        if not site_id or len(site_id) > 140:
            raise ValueError("site_id_invalid")
        cursor.execute("SELECT set_config('app.site_id', %s, true)", (site_id,))

    @staticmethod
    def _required_lease_row(cursor: Cursor) -> MediaJob:
        row = cursor.fetchone()
        if row is None:
            raise LeaseConflict("lease_lost")
        return _job_from_row(row)


def _job_id(submission: MediaJobSubmission) -> str:
    document = "\x1f".join(
        (
            "local-media-job-v1",
            submission.receipt.site_id,
            submission.receipt.request_id,
        )
    )
    return f"media-{hashlib.sha256(document.encode()).hexdigest()[:32]}"


def _receipt_identity(receipt: UploadReceipt) -> dict[str, object]:
    """Stable exact-content identity; receipt timestamps/IDs may change on HTTP replay."""
    return {
        "site_id": receipt.site_id,
        "purpose": receipt.purpose,
        "request_id": receipt.request_id,
        "source_kind": receipt.source_kind.value,
        "media_type": receipt.media_type,
        "byte_size": receipt.byte_size,
        "sha256": receipt.sha256,
        "object_ref": receipt.object_ref,
        "evidence_ref": receipt.evidence_ref,
        "retention_days": receipt.retention_days,
        "consent_basis": receipt.consent_basis,
    }


def _qualified_columns(alias: str) -> str:
    return ", ".join(
        f"{alias}.{column.strip()}" for column in _JOB_COLUMNS.split(",") if column.strip()
    )


def _job_from_row(row: tuple[Any, ...]) -> MediaJob:
    receipt_value = _mapping(row[3], "receipt")
    work_spec = _mapping(row[4], "work_spec")
    reason_codes_value = _json_value(row[13])
    if not isinstance(reason_codes_value, list) or not all(
        isinstance(item, str) for item in reason_codes_value
    ):
        raise ValueError("reason_codes_invalid")
    proof_value = None if row[15] is None else _mapping(row[15], "artifact_proof")
    proof = None if proof_value is None else MediaArtifactProof(**proof_value)
    return MediaJob(
        job_id=str(row[0]),
        site_id=str(row[1]),
        request_id=str(row[2]),
        receipt=_receipt_from_mapping(receipt_value),
        duration_ms=int(work_spec["duration_ms"]),
        channels=int(work_spec["channels"]),
        sample_rate=int(work_spec["sample_rate"]),
        language_hint=(
            None if work_spec.get("language_hint") is None else str(work_spec["language_hint"])
        ),
        submission_digest=str(row[5]),
        status=MediaJobStatus(str(row[6])),
        due_at=row[7],
        attempt=int(row[8]),
        max_attempts=int(row[9]),
        fencing_token=int(row[10]),
        lease_owner=None if row[11] is None else str(row[11]),
        lease_expires_at=row[12],
        reason_codes=tuple(reason_codes_value),
        transcript_ref=None if row[14] is None else str(row[14]),
        artifact_proof=proof,
        created_at=row[16],
        updated_at=row[17],
    )


def _receipt_from_mapping(value: Mapping[str, Any]) -> UploadReceipt:
    received_at = str(value["received_at"]).replace("Z", "+00:00")
    return UploadReceipt(
        receipt_id=str(value["receipt_id"]),
        site_id=str(value["site_id"]),
        purpose=str(value["purpose"]),
        request_id=str(value["request_id"]),
        source_kind=SourceKind(str(value["source_kind"])),
        media_type=str(value["media_type"]),
        byte_size=int(value["byte_size"]),
        sha256=str(value["sha256"]),
        object_ref=str(value["object_ref"]),
        evidence_ref=str(value["evidence_ref"]),
        received_at=datetime.fromisoformat(received_at),
        immutable_checksum=str(value["immutable_checksum"]),
        retention_days=int(value.get("retention_days", 30)),
        consent_basis=str(value.get("consent_basis", "pilot_deferred_review")),
    )


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    decoded = _json_value(value)
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ValueError(f"{name}_invalid")
    return decoded


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _validate_lease_input(
    site_id: str,
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
) -> None:
    _require_aware(now)
    if not site_id or not worker_id:
        raise ValueError("lease_identity_required")
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration_invalid")


def _validate_reason_codes(reason_codes: tuple[str, ...]) -> None:
    if len(reason_codes) > 32 or any(_REASON_CODE.fullmatch(code) is None for code in reason_codes):
        raise ValueError("reason_codes_invalid")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")


def _valid_sha256(value: str) -> bool:
    return _SHA256.fullmatch(value) is not None and value != hashlib.sha256(b"").hexdigest()


__all__ = [
    "InMemoryMediaJobRepository",
    "LeaseConflict",
    "MediaArtifactProof",
    "MediaJob",
    "MediaJobRepository",
    "MediaJobStatus",
    "MediaJobSubmission",
    "PostgresMediaJobRepository",
]
