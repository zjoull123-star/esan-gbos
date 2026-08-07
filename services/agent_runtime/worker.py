from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event, Thread
from types import MappingProxyType
from typing import Protocol, TypeVar

from .agents import (
    AgentExecutionError,
    AgentExecutionResult,
    AgentInput,
    AgentKind,
    BudgetExceeded,
    FactVersionRef,
)
from .models import (
    FailureClassification,
    LeaseConflict,
    LocalPilotFactVersionRef,
    ValidationError,
)
from .repository import AgentTaskRepository

T = TypeVar("T")

_AGENT_KIND_BY_TASK_TYPE: Mapping[str, AgentKind] = MappingProxyType(
    {
        "sales": AgentKind.SALES,
        "purchase": AgentKind.PURCHASE,
        "product_sample": AgentKind.PRODUCT,
        "ceo": AgentKind.CEO,
    }
)


def _agent_kind_from_task_type(task_agent_type: str) -> AgentKind:
    try:
        return _AGENT_KIND_BY_TASK_TYPE[task_agent_type]
    except KeyError as exc:
        raise AgentExecutionError("unsupported agent task type") from exc


@dataclass(frozen=True, slots=True)
class ContextResolutionRequest:
    site_id: str
    task_id: str
    subject_type: str
    subject_ref: str
    evidence_refs: tuple[str, ...]
    fact_version_refs: tuple[LocalPilotFactVersionRef, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedAgentContext:
    site_id: str
    subject_type: str
    subject_ref: str
    evidence_refs: tuple[str, ...]
    fact_version_refs: tuple[LocalPilotFactVersionRef, ...]
    raw_context: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"ResolvedAgentContext(site_id={self.site_id!r}, "
            f"subject_type={self.subject_type!r}, subject_ref={self.subject_ref!r}, "
            "raw_context=<redacted>)"
        )


class ContextResolver(Protocol):
    def resolve(self, request: ContextResolutionRequest) -> ResolvedAgentContext: ...


class AgentExecutor(Protocol):
    def execute(
        self,
        request: AgentInput,
        *,
        now: datetime,
    ) -> AgentExecutionResult: ...


class HeartbeatRunner(Protocol):
    def run(
        self,
        execute: Callable[[], AgentExecutionResult],
        heartbeat: Callable[[], object],
    ) -> AgentExecutionResult: ...


class ThreadedHeartbeatRunner:
    """Renews a lease during a blocking model call and propagates lease loss."""

    def __init__(self, *, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._interval_seconds = interval_seconds

    def run(
        self,
        execute: Callable[[], AgentExecutionResult],
        heartbeat: Callable[[], object],
    ) -> AgentExecutionResult:
        stop = Event()
        failure: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self._interval_seconds):
                try:
                    heartbeat()
                except BaseException as exc:
                    failure.append(exc)
                    stop.set()
                    return

        thread = Thread(target=renew, name="agent-lease-heartbeat", daemon=True)
        thread.start()
        try:
            result = execute()
        finally:
            stop.set()
            thread.join()
        if failure:
            raise failure[0]
        return result


class WorkerRunStatus(StrEnum):
    IDLE = "idle"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    status: WorkerRunStatus
    task_id: str | None = None
    attempt: int | None = None


class ContextResolutionMismatch(AgentExecutionError):
    """A resolver returned context outside the claimed refs-only envelope."""


class AgentWorker:
    def __init__(
        self,
        *,
        repository: AgentTaskRepository,
        site_id: str,
        worker_id: str,
        resolver: ContextResolver,
        executor: AgentExecutor,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = timedelta(minutes=2),
        heartbeat_runner: HeartbeatRunner | None = None,
        retry_delay: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_duration <= timedelta(0) or retry_delay <= timedelta(0):
            raise ValueError("worker lease and retry durations must be positive")
        self._repository = repository
        self._site_id = site_id
        self._worker_id = worker_id
        self._resolver = resolver
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_duration = lease_duration
        self._heartbeat_runner = heartbeat_runner or ThreadedHeartbeatRunner(
            interval_seconds=max(0.1, lease_duration.total_seconds() / 3)
        )
        self._retry_delay = retry_delay

    def run_once(self) -> WorkerRunResult:
        claim = self._repository.claim_for_execution(
            self._site_id,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claim is None:
            return WorkerRunResult(status=WorkerRunStatus.IDLE)
        task = claim.metadata
        try:
            resolution_request = ContextResolutionRequest(
                site_id=task.site_id,
                task_id=task.task_id,
                subject_type=task.subject_type,
                subject_ref=task.subject_ref,
                evidence_refs=claim.payload.evidence_refs,
                fact_version_refs=claim.payload.fact_version_refs,
            )
            context = self._resolver.resolve(resolution_request)
            self._validate_context(resolution_request, context)
            request = AgentInput(
                task_id=task.task_id,
                site_id=task.site_id,
                processing_purpose=task.processing_purpose,
                agent_kind=_agent_kind_from_task_type(task.agent_type),
                requested_by=claim.payload.requested_by,
                subject_type=task.subject_type,
                subject_ref=task.subject_ref,
                subject_revision=claim.payload.subject_revision,
                evidence_refs=claim.payload.evidence_refs,
                fact_version_refs=tuple(
                    FactVersionRef(item.fact_id, item.fact_version)
                    for item in claim.payload.fact_version_refs
                ),
                decision_ref=claim.payload.decision_ref,
                correlation_id=task.correlation_id,
                raw_context=context.raw_context,
                expected_action_type=claim.payload.expected_action_type,
                candidate_refs=claim.payload.candidate_refs,
            )
            result = self._heartbeat_runner.run(
                lambda: self._executor.execute(request, now=self._clock()),
                lambda: self._repository.heartbeat(
                    task.site_id,
                    task.task_id,
                    worker_id=self._worker_id,
                    expected_attempt=task.attempt,
                    now=self._clock(),
                    lease_duration=self._lease_duration,
                ),
            )
            self._repository.complete_with_proposal(
                task.site_id,
                task.task_id,
                worker_id=self._worker_id,
                expected_attempt=task.attempt,
                now=self._clock(),
                result=result,
            )
            return WorkerRunResult(
                status=WorkerRunStatus.SUCCEEDED,
                task_id=task.task_id,
                attempt=task.attempt,
            )
        except LeaseConflict:
            return WorkerRunResult(
                status=WorkerRunStatus.LEASE_LOST,
                task_id=task.task_id,
                attempt=task.attempt,
            )
        except (AgentExecutionError, ValueError) as exc:
            classification = _classify_failure(exc)
            try:
                self._repository.fail(
                    task.site_id,
                    task.task_id,
                    worker_id=self._worker_id,
                    expected_attempt=task.attempt,
                    now=self._clock(),
                    retry_at=self._clock() + self._retry_delay,
                    classification=classification,
                )
            except LeaseConflict:
                return WorkerRunResult(
                    status=WorkerRunStatus.LEASE_LOST,
                    task_id=task.task_id,
                    attempt=task.attempt,
                )
            return WorkerRunResult(
                status=WorkerRunStatus.FAILED,
                task_id=task.task_id,
                attempt=task.attempt,
            )

    def run(self, *, stop_event: Event, idle_delay: float = 1.0) -> None:
        if idle_delay <= 0:
            raise ValueError("idle_delay must be positive")
        while not stop_event.is_set():
            outcome = self.run_once()
            if outcome.status is WorkerRunStatus.IDLE:
                stop_event.wait(idle_delay)

    @staticmethod
    def _validate_context(
        request: ContextResolutionRequest,
        context: ResolvedAgentContext,
    ) -> None:
        if (
            context.site_id != request.site_id
            or context.subject_type != request.subject_type
            or context.subject_ref != request.subject_ref
            or context.evidence_refs != request.evidence_refs
            or context.fact_version_refs != request.fact_version_refs
        ):
            raise ContextResolutionMismatch("resolver returned mismatched task context")


def _classify_failure(exc: BaseException) -> FailureClassification:
    if isinstance(exc, BudgetExceeded):
        return FailureClassification.BUDGET_EXHAUSTED
    if isinstance(exc, ContextResolutionMismatch):
        return FailureClassification.INVALID_OUTPUT
    if isinstance(exc, ValidationError):
        return FailureClassification.INVALID_OUTPUT
    if "policy" in str(exc).casefold():
        return FailureClassification.POLICY_DENIED
    if isinstance(exc, AgentExecutionError):
        return FailureClassification.INVALID_OUTPUT
    return FailureClassification.INTERNAL
