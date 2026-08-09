from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest

from services.media_runtime.repository import (
    Connection,
    LeaseConflict,
    MediaJobStatus,
    MediaJobSubmission,
    PostgresMediaJobRepository,
)
from services.media_runtime.upload import SourceKind, UploadReceipt

pytestmark = pytest.mark.postgres_integration

MIGRATION = (
    Path(__file__).parents[2]
    / "services"
    / "media_runtime"
    / "migrations"
    / "001_local_media_jobs.sql"
)


def _dsn() -> str:
    if os.getenv("GBOS_RUN_LOCAL_MEDIA_POSTGRES_INTEGRATION") != "1":
        pytest.skip(
            "set GBOS_RUN_LOCAL_MEDIA_POSTGRES_INTEGRATION=1 and "
            "GBOS_LOCAL_MEDIA_POSTGRES_DSN to run"
        )
    value = os.getenv("GBOS_LOCAL_MEDIA_POSTGRES_DSN")
    if not value:
        pytest.fail("GBOS_LOCAL_MEDIA_POSTGRES_DSN is required")
    return value


def _receipt(site_id: str, suffix: str) -> UploadReceipt:
    return UploadReceipt(
        receipt_id=f"receipt-{suffix}",
        site_id=site_id,
        purpose="meeting_capture",
        request_id=f"request-{suffix}",
        source_kind=SourceKind.MEETING,
        media_type="audio/wav",
        byte_size=7,
        sha256="a" * 64,
        object_ref=f"object://{site_id}/object-{suffix}",
        evidence_ref=f"evidence://{site_id}/evidence-{suffix}",
        received_at=datetime.now(UTC),
        immutable_checksum="c" * 64,
    )


def _apply_migration(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(MIGRATION.read_text(encoding="utf-8"))


def test_local_media_migration_is_repeatable_and_forces_rls() -> None:
    dsn = _dsn()
    _apply_migration(dsn)
    _apply_migration(dsn)

    with psycopg.connect(dsn) as connection:
        row = connection.execute(
            """
            SELECT relation.relrowsecurity, relation.relforcerowsecurity,
                   role.rolsuper, role.rolbypassrls
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            JOIN pg_roles AS role ON role.rolname = 'gbos_media_runtime_app'
            WHERE namespace.nspname = 'media_runtime'
              AND relation.relname = 'local_media_jobs'
            """
        ).fetchone()
    assert row == (True, True, False, False)


def test_postgres_media_job_replay_lease_fencing_and_dead_letter() -> None:
    dsn = _dsn()
    _apply_migration(dsn)
    suffix = uuid.uuid4().hex[:12]
    site_id = f"media-{suffix}"
    now = datetime.now(UTC)

    with psycopg.connect(dsn) as connection:
        repository = PostgresMediaJobRepository(cast(Connection, connection))
        submission = MediaJobSubmission(
            receipt=_receipt(site_id, suffix),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint="en",
            max_attempts=2,
        )
        queued = repository.enqueue(submission, now=now)
        assert repository.enqueue(submission, now=now) == queued

        first = repository.claim(
            site_id,
            worker_id="worker-old",
            now=now,
            lease_duration=timedelta(seconds=10),
        )
        assert first is not None
        renewed = repository.heartbeat(
            site_id,
            queued.job_id,
            worker_id="worker-old",
            fencing_token=first.fencing_token,
            now=now + timedelta(seconds=5),
            lease_duration=timedelta(seconds=10),
        )
        assert renewed.lease_expires_at == now + timedelta(seconds=15)

        second = repository.claim(
            site_id,
            worker_id="worker-new",
            now=now + timedelta(seconds=16),
            lease_duration=timedelta(seconds=10),
        )
        assert second is not None
        assert second.fencing_token > first.fencing_token
        with pytest.raises(LeaseConflict):
            repository.complete(
                site_id,
                queued.job_id,
                worker_id="worker-old",
                fencing_token=first.fencing_token,
                now=now + timedelta(seconds=17),
                status=MediaJobStatus.READY,
                reason_codes=(),
                transcript_ref=None,
                artifact_proof=None,
            )

        terminal = repository.retry(
            site_id,
            queued.job_id,
            worker_id="worker-new",
            fencing_token=second.fencing_token,
            now=now + timedelta(seconds=17),
            retry_at=now + timedelta(minutes=2),
            reason_codes=("transient_failure",),
        )
        assert terminal.status is MediaJobStatus.DEAD_LETTER
        assert terminal.reason_codes == ("transient_failure",)
