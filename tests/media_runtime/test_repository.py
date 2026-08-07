from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.media_runtime.repository import (
    InMemoryMediaJobRepository,
    LeaseConflict,
    MediaJobStatus,
    MediaJobSubmission,
)
from services.media_runtime.upload import SourceKind, UploadReceipt

NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


def _receipt(*, checksum: str = "c" * 64, sha256: str = "a" * 64) -> UploadReceipt:
    return UploadReceipt(
        receipt_id="receipt-01",
        site_id="site-a",
        purpose="meeting_capture",
        request_id="request-01",
        source_kind=SourceKind.MEETING,
        media_type="audio/wav",
        byte_size=7,
        sha256=sha256,
        object_ref="object://site-a/object-01",
        evidence_ref="evidence://site-a/evidence-01",
        received_at=NOW,
        immutable_checksum=checksum,
    )


def _submission(
    *,
    checksum: str = "c" * 64,
    sha256: str = "a" * 64,
    max_attempts: int = 2,
) -> MediaJobSubmission:
    return MediaJobSubmission(
        receipt=_receipt(checksum=checksum, sha256=sha256),
        duration_ms=1_000,
        channels=1,
        sample_rate=16_000,
        language_hint="en",
        max_attempts=max_attempts,
    )


def test_enqueue_is_idempotent_but_rejects_changed_receipt() -> None:
    repository = InMemoryMediaJobRepository()

    first = repository.enqueue(_submission(), now=NOW)
    replay = repository.enqueue(
        _submission(checksum="d" * 64),
        now=NOW + timedelta(seconds=1),
    )

    assert replay == first
    with pytest.raises(ValueError, match="idempotency_conflict"):
        repository.enqueue(_submission(sha256="b" * 64), now=NOW)


def test_expired_lease_is_reclaimed_with_new_fencing_token() -> None:
    repository = InMemoryMediaJobRepository()
    queued = repository.enqueue(_submission(), now=NOW)
    first = repository.claim(
        "site-a",
        worker_id="worker-old",
        now=NOW,
        lease_duration=timedelta(seconds=10),
    )
    assert first is not None

    reclaimed = repository.claim(
        "site-a",
        worker_id="worker-new",
        now=NOW + timedelta(seconds=11),
        lease_duration=timedelta(seconds=10),
    )

    assert reclaimed is not None
    assert reclaimed.job_id == queued.job_id
    assert reclaimed.attempt == 2
    assert reclaimed.fencing_token > first.fencing_token
    with pytest.raises(LeaseConflict):
        repository.complete(
            "site-a",
            queued.job_id,
            worker_id="worker-old",
            fencing_token=first.fencing_token,
            now=NOW + timedelta(seconds=12),
            status=MediaJobStatus.READY,
            reason_codes=(),
            transcript_ref=None,
            artifact_proof=None,
        )


def test_retry_exhaustion_moves_job_to_dead_letter() -> None:
    repository = InMemoryMediaJobRepository()
    queued = repository.enqueue(_submission(max_attempts=1), now=NOW)
    claim = repository.claim(
        "site-a",
        worker_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=10),
    )
    assert claim is not None

    terminal = repository.retry(
        "site-a",
        queued.job_id,
        worker_id="worker-a",
        fencing_token=claim.fencing_token,
        now=NOW + timedelta(seconds=1),
        retry_at=NOW + timedelta(minutes=1),
        reason_codes=("speech_provider_error",),
    )

    assert terminal.status is MediaJobStatus.DEAD_LETTER
    assert terminal.reason_codes == ("speech_provider_error",)
    assert terminal.lease_owner is None


def test_heartbeat_requires_current_live_fence() -> None:
    repository = InMemoryMediaJobRepository()
    queued = repository.enqueue(_submission(), now=NOW)
    claim = repository.claim(
        "site-a",
        worker_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=10),
    )
    assert claim is not None

    renewed = repository.heartbeat(
        "site-a",
        queued.job_id,
        worker_id="worker-a",
        fencing_token=claim.fencing_token,
        now=NOW + timedelta(seconds=5),
        lease_duration=timedelta(seconds=10),
    )
    assert renewed.lease_expires_at == NOW + timedelta(seconds=15)

    with pytest.raises(LeaseConflict):
        repository.heartbeat(
            "site-a",
            queued.job_id,
            worker_id="worker-a",
            fencing_token=claim.fencing_token - 1,
            now=NOW + timedelta(seconds=6),
            lease_duration=timedelta(seconds=10),
        )
