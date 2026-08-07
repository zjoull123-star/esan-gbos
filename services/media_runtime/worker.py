from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event, Thread
from typing import Protocol, TypeVar

from .common import PipelineStatus
from .observer_bridge import ObserverBridge
from .pipeline import MediaPipelineOutcome, MediaPipelineRequest
from .repository import (
    LeaseConflict,
    MediaArtifactProof,
    MediaJob,
    MediaJobRepository,
    MediaJobStatus,
)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ResolvedMediaPaths:
    source_path: str = field(repr=False)
    output_path: str = field(repr=False)


class MediaPathResolver(Protocol):
    def resolve(self, job: MediaJob) -> ResolvedMediaPaths: ...


class MediaPipelineRunner(Protocol):
    def run(self, request: MediaPipelineRequest) -> MediaPipelineOutcome: ...


class MediaProofProvider(Protocol):
    def proof_for(
        self,
        job: MediaJob,
        outcome: MediaPipelineOutcome,
    ) -> MediaArtifactProof | None: ...


class HeartbeatRunner(Protocol):
    def run(self, execute: Callable[[], T], heartbeat: Callable[[], object]) -> T: ...


class ThreadedHeartbeatRunner:
    def __init__(self, *, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat_interval_invalid")
        self._interval_seconds = interval_seconds

    def run(self, execute: Callable[[], T], heartbeat: Callable[[], object]) -> T:
        stop = Event()
        failures: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self._interval_seconds):
                try:
                    heartbeat()
                except BaseException as exc:
                    failures.append(exc)
                    stop.set()
                    return

        thread = Thread(target=renew, name="media-lease-heartbeat", daemon=True)
        thread.start()
        try:
            result = execute()
        finally:
            stop.set()
            thread.join()
        if failures:
            raise failures[0]
        return result


class WorkerRunStatus(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    RETRY = "retry"
    QUARANTINED = "quarantined"
    DEAD_LETTER = "dead_letter"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    status: WorkerRunStatus
    job_id: str | None = None
    attempt: int | None = None


class MediaWorker:
    def __init__(
        self,
        *,
        repository: MediaJobRepository,
        pipeline: MediaPipelineRunner,
        observer_bridge: ObserverBridge,
        path_resolver: MediaPathResolver,
        proof_provider: MediaProofProvider,
        site_id: str,
        worker_id: str,
        enabled: Callable[[], bool],
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        retry_delay: timedelta = timedelta(minutes=1),
        heartbeat_runner: HeartbeatRunner | None = None,
    ) -> None:
        if not site_id or not worker_id:
            raise ValueError("worker_identity_required")
        if lease_duration <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("worker_duration_invalid")
        self._repository = repository
        self._pipeline = pipeline
        self._observer_bridge = observer_bridge
        self._path_resolver = path_resolver
        self._proof_provider = proof_provider
        self._site_id = site_id
        self._worker_id = worker_id
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._heartbeat_runner = heartbeat_runner or ThreadedHeartbeatRunner(
            interval_seconds=max(0.1, lease_duration.total_seconds() / 3)
        )

    def run_once(self) -> WorkerRunResult:
        if not self._enabled():
            return WorkerRunResult(status=WorkerRunStatus.DISABLED)
        claim = self._repository.claim(
            self._site_id,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return WorkerRunResult(status=WorkerRunStatus.IDLE)
        try:
            paths = self._path_resolver.resolve(claim)
            request = MediaPipelineRequest(
                receipt=claim.receipt,
                source_path=paths.source_path,
                output_path=paths.output_path,
                duration_ms=claim.duration_ms,
                channels=claim.channels,
                sample_rate=claim.sample_rate,
                attempt=claim.attempt,
                max_attempts=claim.max_attempts,
                language_hint=claim.language_hint,
            )
            outcome = self._heartbeat_runner.run(
                lambda: self._pipeline.run(request),
                lambda: self._repository.heartbeat(
                    claim.site_id,
                    claim.job_id,
                    worker_id=self._worker_id,
                    fencing_token=claim.fencing_token,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                ),
            )
            return self._record_outcome(claim, outcome)
        except LeaseConflict:
            return self._result(WorkerRunStatus.LEASE_LOST, claim)
        except Exception:
            return self._retry(claim, ("media_runtime_error",))

    def _record_outcome(
        self,
        claim: MediaJob,
        outcome: MediaPipelineOutcome,
    ) -> WorkerRunResult:
        if outcome.status is PipelineStatus.RETRY:
            return self._retry(claim, outcome.reason_codes or ("pipeline_retry",))
        if outcome.status is PipelineStatus.QUARANTINED:
            return self._complete(
                claim,
                status=MediaJobStatus.QUARANTINED,
                worker_status=WorkerRunStatus.QUARANTINED,
                reason_codes=outcome.reason_codes,
            )
        if outcome.status is PipelineStatus.DEAD_LETTER:
            return self._complete(
                claim,
                status=MediaJobStatus.DEAD_LETTER,
                worker_status=WorkerRunStatus.DEAD_LETTER,
                reason_codes=outcome.reason_codes,
            )
        if outcome.status is not PipelineStatus.READY:
            return self._complete(
                claim,
                status=MediaJobStatus.QUARANTINED,
                worker_status=WorkerRunStatus.QUARANTINED,
                reason_codes=("pipeline_status_invalid",),
            )
        if not claim.receipt.media_type.startswith("audio/"):
            try:
                manifest_ref = self._observer_bridge.publish_media_evidence(
                    claim.receipt,
                    idempotency_key=f"media-evidence:{claim.job_id}",
                )
            except Exception:
                return self._retry(claim, ("observer_bridge_unavailable",))
            return self._complete(
                claim,
                status=MediaJobStatus.READY,
                worker_status=WorkerRunStatus.SUCCEEDED,
                reason_codes=(),
                transcript_ref=manifest_ref,
            )
        transcript = outcome.transcript
        if (
            transcript is None
            or transcript.site_id != claim.site_id
            or transcript.source_evidence_ref != claim.receipt.evidence_ref
        ):
            return self._complete(
                claim,
                status=MediaJobStatus.QUARANTINED,
                worker_status=WorkerRunStatus.QUARANTINED,
                reason_codes=("transcript_proof_invalid",),
            )
        proof = self._proof_provider.proof_for(claim, outcome)
        if proof is None or proof.whisper_model_sha256 != transcript.model_sha256:
            return self._complete(
                claim,
                status=MediaJobStatus.QUARANTINED,
                worker_status=WorkerRunStatus.QUARANTINED,
                reason_codes=("runtime_artifact_proof_missing",),
            )
        try:
            transcript_ref = self._observer_bridge.publish(
                transcript,
                idempotency_key=f"media-transcript:{claim.job_id}",
            )
        except Exception:
            return self._retry(claim, ("observer_bridge_unavailable",))
        return self._complete(
            claim,
            status=MediaJobStatus.READY,
            worker_status=WorkerRunStatus.SUCCEEDED,
            reason_codes=(),
            transcript_ref=transcript_ref,
            artifact_proof=proof,
        )

    def _complete(
        self,
        claim: MediaJob,
        *,
        status: MediaJobStatus,
        worker_status: WorkerRunStatus,
        reason_codes: tuple[str, ...],
        transcript_ref: str | None = None,
        artifact_proof: MediaArtifactProof | None = None,
    ) -> WorkerRunResult:
        try:
            self._repository.complete(
                claim.site_id,
                claim.job_id,
                worker_id=self._worker_id,
                fencing_token=claim.fencing_token,
                now=self._clock(),
                status=status,
                reason_codes=reason_codes,
                transcript_ref=transcript_ref,
                artifact_proof=artifact_proof,
            )
        except LeaseConflict:
            return self._result(WorkerRunStatus.LEASE_LOST, claim)
        return self._result(worker_status, claim)

    def _retry(self, claim: MediaJob, reason_codes: tuple[str, ...]) -> WorkerRunResult:
        now = self._clock()
        try:
            updated = self._repository.retry(
                claim.site_id,
                claim.job_id,
                worker_id=self._worker_id,
                fencing_token=claim.fencing_token,
                now=now,
                retry_at=now + self._retry_delay,
                reason_codes=reason_codes,
            )
        except LeaseConflict:
            return self._result(WorkerRunStatus.LEASE_LOST, claim)
        status = (
            WorkerRunStatus.DEAD_LETTER
            if updated.status is MediaJobStatus.DEAD_LETTER
            else WorkerRunStatus.RETRY
        )
        return self._result(status, claim)

    @staticmethod
    def _result(status: WorkerRunStatus, job: MediaJob) -> WorkerRunResult:
        return WorkerRunResult(status=status, job_id=job.job_id, attempt=job.attempt)


__all__ = [
    "MediaPathResolver",
    "MediaProofProvider",
    "MediaWorker",
    "ResolvedMediaPaths",
    "ThreadedHeartbeatRunner",
    "WorkerRunResult",
    "WorkerRunStatus",
]
