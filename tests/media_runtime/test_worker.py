from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from services.media_runtime.common import PipelineStatus
from services.media_runtime.observer_bridge import (
    MediaEvidenceManifest,
    ObserverBridge,
    TranscriptReferenceSubmission,
)
from services.media_runtime.pipeline import MediaPipelineOutcome
from services.media_runtime.repository import (
    InMemoryMediaJobRepository,
    MediaArtifactProof,
    MediaJobStatus,
    MediaJobSubmission,
)
from services.media_runtime.speech import TranscriptSegment, TranscriptSegments
from services.media_runtime.upload import SourceKind, UploadReceipt
from services.media_runtime.worker import (
    MediaWorker,
    ResolvedMediaPaths,
    WorkerRunStatus,
)

NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


class RecordingPipeline:
    def __init__(self, outcome: MediaPipelineOutcome) -> None:
        self.outcome = outcome
        self.requests: list[object] = []

    def run(self, request: object) -> MediaPipelineOutcome:
        self.requests.append(request)
        return self.outcome


class FixedResolver:
    def resolve(self, _job: object) -> ResolvedMediaPaths:
        return ResolvedMediaPaths(
            source_path="/media/input/request-01.wav",
            output_path="/media/output/request-01.wav",
        )


class RecordingObserverClient:
    def __init__(self, *, fail_after_first_commit: bool = False) -> None:
        self.keys: list[str] = []
        self.transcript_submissions: list[TranscriptReferenceSubmission] = []
        self.manifest_submissions: list[MediaEvidenceManifest] = []
        self.fail_after_first_commit = fail_after_first_commit

    def submit_transcript_refs(
        self,
        submission: TranscriptReferenceSubmission,
        *,
        idempotency_key: str,
    ) -> None:
        self.transcript_submissions.append(submission)
        self._record(idempotency_key)

    def submit_evidence_manifest(
        self,
        manifest: MediaEvidenceManifest,
        *,
        idempotency_key: str,
    ) -> None:
        self.manifest_submissions.append(manifest)
        self._record(idempotency_key)

    def _record(self, idempotency_key: str) -> None:
        self.keys.append(idempotency_key)
        if self.fail_after_first_commit and len(self.keys) == 1:
            raise RuntimeError("response lost after observer commit")


class FixedProofProvider:
    def __init__(self, proof: MediaArtifactProof | None) -> None:
        self.proof = proof

    def proof_for(self, _job: object, _outcome: object) -> MediaArtifactProof | None:
        return self.proof


def _receipt(*, media_type: str = "audio/wav") -> UploadReceipt:
    return UploadReceipt(
        receipt_id="receipt-01",
        site_id="site-a",
        purpose="meeting_capture",
        request_id="request-01",
        source_kind=SourceKind.MEETING,
        media_type=media_type,
        byte_size=7,
        sha256="a" * 64,
        object_ref="object://site-a/object-01",
        evidence_ref="evidence://site-a/evidence-01",
        received_at=NOW,
        immutable_checksum="c" * 64,
    )


def _transcript() -> TranscriptSegments:
    return TranscriptSegments(
        transcript_id="transcript_01",
        site_id="site-a",
        source_evidence_ref="evidence://site-a/evidence-01",
        model_provider="local_faster_whisper",
        model_name="large-v3-turbo",
        model_version="large-v3-turbo-ct2-local-v1",
        model_sha256=hashlib.sha256(b"model").hexdigest(),
        language="en",
        segments=(
            TranscriptSegment(
                segment_id="segment_000001",
                start_ms=0,
                end_ms=900,
                speaker="Unknown",
                confidence=0.9,
                text_ref="localtext://site-a/text-01",
                evidence_ref="evidence://site-a/evidence-01/range-0-900",
            ),
        ),
        generated_at=NOW,
    )


def _proof() -> MediaArtifactProof:
    return MediaArtifactProof(
        ffmpeg_output_sha256=hashlib.sha256(b"output").hexdigest(),
        ffmpeg_executable_sha256=hashlib.sha256(b"ffmpeg").hexdigest(),
        whisper_model_sha256=_transcript().model_sha256,
    )


def _worker(
    repository: InMemoryMediaJobRepository,
    pipeline: RecordingPipeline,
    observer: RecordingObserverClient,
    *,
    proof: MediaArtifactProof | None,
    enabled: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> MediaWorker:
    return MediaWorker(
        repository=repository,
        pipeline=pipeline,
        observer_bridge=ObserverBridge(
            client=observer,
            site_id="site-a",
            team_ref="team-sales",
        ),
        path_resolver=FixedResolver(),
        proof_provider=FixedProofProvider(proof),
        site_id="site-a",
        worker_id="worker-a",
        enabled=lambda: enabled,
        clock=clock or (lambda: NOW),
        lease_duration=timedelta(minutes=1),
        retry_delay=timedelta(minutes=1),
    )


def test_worker_calls_pipeline_and_publishes_refs_before_ready() -> None:
    repository = InMemoryMediaJobRepository()
    job = repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint="en",
            max_attempts=3,
        ),
        now=NOW,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=(),
            transcript=_transcript(),
        )
    )
    observer = RecordingObserverClient()

    result = _worker(repository, pipeline, observer, proof=_proof()).run_once()

    assert result.status is WorkerRunStatus.SUCCEEDED
    assert len(pipeline.requests) == 1
    assert observer.keys == [f"media-transcript:{job.job_id}"]
    stored = repository.get("site-a", job.job_id)
    assert stored is not None
    assert stored.status is MediaJobStatus.READY
    assert stored.transcript_ref is not None
    assert stored.artifact_proof == _proof()


def test_ready_non_audio_publishes_refs_before_ready() -> None:
    repository = InMemoryMediaJobRepository()
    job = repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(media_type="application/pdf"),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint=None,
            max_attempts=3,
        ),
        now=NOW,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=(),
        )
    )
    observer = RecordingObserverClient()

    result = _worker(repository, pipeline, observer, proof=None).run_once()

    assert result.status is WorkerRunStatus.SUCCEEDED
    assert observer.keys == [f"media-evidence:{job.job_id}"]
    assert len(observer.manifest_submissions) == 1
    manifest = observer.manifest_submissions[0]
    assert (
        manifest.site_id,
        manifest.team_ref,
        manifest.source_evidence_ref,
    ) == ("site-a", "team-sales", job.receipt.evidence_ref)
    stored = repository.get("site-a", job.job_id)
    assert stored is not None
    assert stored.transcript_ref == manifest.manifest_ref


def test_missing_runtime_artifact_proof_fails_closed_to_quarantine() -> None:
    repository = InMemoryMediaJobRepository()
    job = repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint=None,
            max_attempts=3,
        ),
        now=NOW,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=(),
            transcript=_transcript(),
        )
    )
    observer = RecordingObserverClient()

    result = _worker(repository, pipeline, observer, proof=None).run_once()

    assert result.status is WorkerRunStatus.QUARANTINED
    stored = repository.get("site-a", job.job_id)
    assert stored is not None
    assert stored.status is MediaJobStatus.QUARANTINED
    assert stored.reason_codes == ("runtime_artifact_proof_missing",)
    assert observer.keys == []


def test_kill_switch_prevents_claim_and_pipeline_execution() -> None:
    repository = InMemoryMediaJobRepository()
    repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint=None,
        ),
        now=NOW,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.RETRY,
            reason_codes=("unused",),
            stage_idempotency_keys=(),
        )
    )
    observer = RecordingObserverClient()

    result = _worker(repository, pipeline, observer, proof=None, enabled=False).run_once()

    assert result.status is WorkerRunStatus.DISABLED
    assert pipeline.requests == []
    assert (
        repository.claim(
            "site-a",
            worker_id="probe",
            now=NOW,
            lease_duration=timedelta(seconds=1),
        )
        is not None
    )


def test_observer_response_loss_replays_with_same_idempotency_key() -> None:
    class MutableClock:
        now = NOW

        def __call__(self) -> datetime:
            return self.now

    clock = MutableClock()
    repository = InMemoryMediaJobRepository()
    job = repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint="en",
            max_attempts=3,
        ),
        now=clock.now,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=(),
            transcript=_transcript(),
        )
    )
    observer = RecordingObserverClient(fail_after_first_commit=True)
    worker = _worker(
        repository,
        pipeline,
        observer,
        proof=_proof(),
        clock=clock,
    )

    first = worker.run_once()
    clock.now += timedelta(minutes=2)
    replay = worker.run_once()

    assert first.status is WorkerRunStatus.RETRY
    assert replay.status is WorkerRunStatus.SUCCEEDED
    assert observer.keys == [f"media-transcript:{job.job_id}"] * 2
    assert len(pipeline.requests) == 2
    stored = repository.get("site-a", job.job_id)
    assert stored is not None
    assert stored.status is MediaJobStatus.READY


def test_non_audio_observer_response_loss_replays_without_scope_drift() -> None:
    class MutableClock:
        now = NOW

        def __call__(self) -> datetime:
            return self.now

    clock = MutableClock()
    repository = InMemoryMediaJobRepository()
    job = repository.enqueue(
        MediaJobSubmission(
            receipt=_receipt(media_type="application/pdf"),
            duration_ms=1_000,
            channels=1,
            sample_rate=16_000,
            language_hint=None,
            max_attempts=3,
        ),
        now=clock.now,
    )
    pipeline = RecordingPipeline(
        MediaPipelineOutcome(
            status=PipelineStatus.READY,
            reason_codes=(),
            stage_idempotency_keys=(),
        )
    )
    observer = RecordingObserverClient(fail_after_first_commit=True)
    worker = _worker(
        repository,
        pipeline,
        observer,
        proof=None,
        clock=clock,
    )

    first = worker.run_once()
    clock.now += timedelta(minutes=2)
    replay = worker.run_once()

    assert first.status is WorkerRunStatus.RETRY
    assert replay.status is WorkerRunStatus.SUCCEEDED
    assert observer.keys == [f"media-evidence:{job.job_id}"] * 2
    assert observer.manifest_submissions[0] == observer.manifest_submissions[1]
    manifest = observer.manifest_submissions[0]
    assert (
        manifest.site_id,
        manifest.team_ref,
        manifest.source_evidence_ref,
    ) == ("site-a", "team-sales", job.receipt.evidence_ref)
    assert "application/pdf" not in repr(manifest)
    stored = repository.get("site-a", job.job_id)
    assert stored is not None
    assert stored.status is MediaJobStatus.READY
    assert stored.transcript_ref == manifest.manifest_ref
