from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event

import pytest

from services.action_guard.policy import ActionGuard
from services.agent_runtime import (
    AgentOrchestrator,
    AgentTaskSubmission,
    AgentWorker,
    ContextResolutionRequest,
    CostMetadata,
    DeterministicLocalProvider,
    FailureClassification,
    InMemoryAgentTaskRepository,
    InvocationReferences,
    LocalPilotTaskPayload,
    ModelInvocationRecord,
    ResolvedAgentContext,
    TaskStatus,
    ThreadedHeartbeatRunner,
    TokenUsageMetadata,
    WorkerRunStatus,
)
from services.agent_runtime.models import canonical_payload_digest

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def payload() -> LocalPilotTaskPayload:
    return LocalPilotTaskPayload.from_mapping(
        {
            "schema_version": "local-pilot-agent-task-v1",
            "evidence_refs": ["evidence-1"],
            "fact_version_refs": [{"fact_id": "fact-1", "fact_version": 1}],
            "subject": {"revision": 1},
            "request": {
                "requested_by": "sales-agent",
                "decision_ref": "decision-1",
                "expected_action_type": "internal.work_item.propose",
                "candidate_refs": [],
            },
        }
    )


def enqueue(repository: InMemoryAgentTaskRepository) -> None:
    repository.enqueue(
        AgentTaskSubmission(
            task_id="task-1",
            site_id="site-a",
            processing_purpose="sales_follow_up",
            idempotency_key="idem-1",
            agent_type="sales",
            subject_type="CRM Deal",
            subject_ref="deal-1",
            due_at=NOW,
            priority=50,
            max_attempts=3,
            causation_id="cause-1",
            correlation_id="correlation-1",
            payload=payload().to_mapping(),
        ),
        now=NOW,
    )


class Resolver:
    def __init__(self, *, subject_ref: str = "deal-1") -> None:
        self.subject_ref = subject_ref
        self.requests: list[ContextResolutionRequest] = []

    def resolve(self, request: ContextResolutionRequest) -> ResolvedAgentContext:
        self.requests.append(request)
        return ResolvedAgentContext(
            site_id=request.site_id,
            subject_type=request.subject_type,
            subject_ref=self.subject_ref,
            evidence_refs=request.evidence_refs,
            fact_version_refs=request.fact_version_refs,
            raw_context="Alice at alice@example.com from Acme Trading.",
        )


def orchestrator() -> AgentOrchestrator:
    return AgentOrchestrator(
        provider=DeterministicLocalProvider(),
        guard=ActionGuard(),
        known_evidence_refs={"evidence-1"},
        known_fact_refs={("fact-1", 1)},
        known_subject_refs={("CRM Deal", "deal-1")},
    )


class HeartbeatRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, execute, heartbeat):
        self.calls += 1
        heartbeat()
        return execute()


def worker(
    repository: InMemoryAgentTaskRepository,
    *,
    now: datetime = NOW,
    resolver: Resolver | None = None,
    executor=None,
    heartbeat_runner: HeartbeatRunner | None = None,
) -> AgentWorker:
    return AgentWorker(
        repository=repository,
        site_id="site-a",
        worker_id="worker-1",
        resolver=resolver or Resolver(),
        executor=executor or orchestrator(),
        clock=lambda: now,
        lease_duration=timedelta(seconds=30),
        heartbeat_runner=heartbeat_runner,
        retry_delay=timedelta(minutes=5),
    )


def test_worker_run_once_resolves_context_just_in_time_heartbeats_and_commits() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue(repository)
    resolver = Resolver()
    heartbeat_runner = HeartbeatRunner()

    outcome = worker(
        repository,
        resolver=resolver,
        heartbeat_runner=heartbeat_runner,
    ).run_once()

    assert outcome.status is WorkerRunStatus.SUCCEEDED
    assert heartbeat_runner.calls == 1
    assert len(resolver.requests) == 1
    assert resolver.requests[0].site_id == "site-a"
    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.status is TaskStatus.SUCCEEDED
    assert repository.get_proposal("site-a", "task-1", attempt=1) is not None


def test_worker_resolver_mismatch_fails_closed_without_proposal() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue(repository)

    outcome = worker(repository, resolver=Resolver(subject_ref="other-deal")).run_once()

    assert outcome.status is WorkerRunStatus.FAILED
    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.status is TaskStatus.RECHECK
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None


class IdentityLeakingExecutor:
    def __init__(self) -> None:
        self.runtime = orchestrator()

    def execute(self, request, *, now):
        result = self.runtime.execute(request, now=now)
        leaked_payload = {"summary": "Contact Alice at alice@example.com"}
        return replace(
            result,
            action_proposal={
                **result.action_proposal,
                "payload": leaked_payload,
                "payload_digest": canonical_payload_digest(leaked_payload),
            },
        )


def test_worker_classifies_identity_leaking_proposal_as_invalid_output() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue(repository)

    outcome = worker(repository, executor=IdentityLeakingExecutor()).run_once()

    assert outcome.status is WorkerRunStatus.FAILED
    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.failure_classification is FailureClassification.INVALID_OUTPUT
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None


class SimulatedCrash(BaseException):
    pass


class CrashingExecutor:
    def execute(self, request, *, now):
        raise SimulatedCrash


def test_worker_crash_leaves_no_bundle_and_expired_lease_can_recover_once() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue(repository)

    with pytest.raises(SimulatedCrash):
        worker(repository, executor=CrashingExecutor()).run_once()

    running = repository.get("site-a", "task-1")
    assert running is not None
    assert running.status is TaskStatus.RUNNING
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None
    assert repository.invocations("site-a") == ()

    recovered = AgentWorker(
        repository=repository,
        site_id="site-a",
        worker_id="worker-1",
        resolver=Resolver(),
        executor=orchestrator(),
        clock=lambda: NOW + timedelta(seconds=31),
        lease_duration=timedelta(seconds=30),
        heartbeat_runner=HeartbeatRunner(),
        retry_delay=timedelta(minutes=5),
    ).run_once()

    assert recovered.status is WorkerRunStatus.SUCCEEDED
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None
    assert repository.get_proposal("site-a", "task-1", attempt=2) is not None


class CrashBeforeFirstCommitRepository(InMemoryAgentTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.crash_before_commit = True

    def complete_with_proposal(self, *args, **kwargs):
        if self.crash_before_commit:
            self.crash_before_commit = False
            raise SimulatedCrash
        return super().complete_with_proposal(*args, **kwargs)


class AuditedExecutor:
    def __init__(self) -> None:
        self.runtime = orchestrator()

    def execute(self, request, *, now):
        result = self.runtime.execute(request, now=now)
        record = ModelInvocationRecord(
            invocation_id="invocation-worker-recovery",
            site_id=request.site_id,
            provider="deepseek",
            requested_model="deepseek-v4-flash",
            observed_model="deepseek-v4-flash",
            prompt_version="sales-local-pilot-v1",
            output_schema_version="sales-proposal-v1.0",
            policy_version="model-gateway-policy-v1",
            tokenizer_version="stable-hmac-tokenizer-v1",
            request_id=request.task_id,
            response_id="response-worker-recovery",
            started_at=now,
            completed_at=now,
            latency_ms=1,
            status="succeeded",
            token_usage=TokenUsageMetadata.known(10, 5, 15),
            cost=CostMetadata.known(Decimal("0.001"), "USD"),
            network_call_count=1,
            tool_call_count=0,
            external_send_count=0,
            references=InvocationReferences(
                evidence_refs=request.evidence_refs,
                tokenization_receipt_refs=("receipt-worker-recovery",),
            ),
            idempotency_key="model-call-worker-recovery",
            attempt=1,
            retry_count=0,
            finish_code="stop",
            error_code=None,
            budget_status="normal",
            price_catalog_version="catalog-v1",
            output_digest="a" * 64,
        )
        return replace(result, invocations=(record,))


def test_worker_crash_after_model_before_commit_recovers_without_duplicate_ledger_cost() -> None:
    repository = CrashBeforeFirstCommitRepository()
    enqueue(repository)
    executor = AuditedExecutor()

    with pytest.raises(SimulatedCrash):
        worker(repository, executor=executor).run_once()

    assert repository.invocations("site-a") == ()
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None

    outcome = AgentWorker(
        repository=repository,
        site_id="site-a",
        worker_id="worker-1",
        resolver=Resolver(),
        executor=executor,
        clock=lambda: NOW + timedelta(seconds=31),
        lease_duration=timedelta(seconds=30),
        heartbeat_runner=HeartbeatRunner(),
        retry_delay=timedelta(minutes=5),
    ).run_once()

    assert outcome.status is WorkerRunStatus.SUCCEEDED
    records = repository.invocations("site-a")
    assert len(records) == 1
    assert records[0].cost.amount == Decimal("0.001")
    assert repository.get_proposal("site-a", "task-1", attempt=1) is None
    assert repository.get_proposal("site-a", "task-1", attempt=2) is not None


def test_worker_loop_honors_pre_set_stop_without_claiming() -> None:
    repository = InMemoryAgentTaskRepository()
    enqueue(repository)
    stop = Event()
    stop.set()

    worker(repository).run(stop_event=stop, idle_delay=0.01)

    task = repository.get("site-a", "task-1")
    assert task is not None
    assert task.status is TaskStatus.QUEUED


def test_threaded_heartbeat_runner_renews_during_blocking_execution() -> None:
    heartbeat_seen = Event()

    def execute():
        assert heartbeat_seen.wait(1)
        return object()

    result = ThreadedHeartbeatRunner(interval_seconds=0.01).run(
        execute,  # type: ignore[arg-type]
        lambda: heartbeat_seen.set(),
    )

    assert result is not None


def test_resolved_context_repr_redacts_tokenization_input() -> None:
    context = Resolver().resolve(
        ContextResolutionRequest(
            site_id="site-a",
            task_id="task-1",
            subject_type="CRM Deal",
            subject_ref="deal-1",
            evidence_refs=("evidence-1",),
            fact_version_refs=(
                LocalPilotTaskPayload.from_mapping(payload().to_mapping()).fact_version_refs[0],
            ),
        )
    )

    assert "alice@example.com" not in repr(context).casefold()
    assert "raw_context=<redacted>" in repr(context)
