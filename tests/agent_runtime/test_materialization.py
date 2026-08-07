from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

import services.agent_runtime as agent_runtime
from services.agent_runtime import (
    MaterializationContext,
    MaterializationEnvelope,
    TrustedMaterializer,
)
from services.agent_runtime.materialization import (
    FrappeDraftReceipt,
    MaterializationClaim,
    MaterializationContextRequest,
    MaterializationWorker,
)
from services.agent_runtime.models import IdempotencyConflict, LeaseConflict
from services.agent_runtime.postgres import PostgresAgentTaskRepository

ROOT = Path(__file__).parents[2]


class _CrashAfterFrappeCommit(BaseException):
    pass


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class _Repository:
    def __init__(self, claim: MaterializationClaim) -> None:
        self.claim = claim
        self.status = "pending"
        self.receipt: FrappeDraftReceipt | None = None
        self.failures: list[str] = []
        self.heartbeats: list[tuple[str, int]] = []
        self.lose_heartbeat_at: int | None = None

    def claim_materialization(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> MaterializationClaim | None:
        if site_id != self.claim.site_id:
            return None
        if self.status == "succeeded":
            return None
        if self.status == "running" and self.claim.lease_expires_at > now:
            return None
        self.status = "running"
        self.claim = MaterializationClaim(
            materialization_id=self.claim.materialization_id,
            proposal_id=self.claim.proposal_id,
            site_id=self.claim.site_id,
            attempt=self.claim.attempt + 1,
            max_attempts=self.claim.max_attempts,
            lease_owner=worker_id,
            lease_expires_at=now + lease_duration,
            envelope=self.claim.envelope,
        )
        return self.claim

    def heartbeat_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> None:
        self.heartbeats.append((materialization_id, expected_attempt))
        if self.lose_heartbeat_at == len(self.heartbeats):
            raise LeaseConflict("materialization lease lost")
        if (
            site_id != self.claim.site_id
            or materialization_id != self.claim.materialization_id
            or worker_id != self.claim.lease_owner
            or expected_attempt != self.claim.attempt
            or self.claim.lease_expires_at <= now
        ):
            raise LeaseConflict("materialization lease lost")
        proposed_expiry = now + lease_duration
        if proposed_expiry > self.claim.lease_expires_at:
            self.claim = replace(self.claim, lease_expires_at=proposed_expiry)

    def acknowledge_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        receipt: FrappeDraftReceipt,
    ) -> FrappeDraftReceipt:
        if self.status == "succeeded":
            if receipt != self.receipt:
                raise IdempotencyConflict("receipt body conflict")
            return receipt
        if (
            site_id != self.claim.site_id
            or materialization_id != self.claim.materialization_id
            or worker_id != self.claim.lease_owner
            or expected_attempt != self.claim.attempt
            or self.claim.lease_expires_at <= now
        ):
            raise LeaseConflict("materialization lease lost")
        self.status = "succeeded"
        self.receipt = receipt
        return receipt

    def fail_materialization(
        self,
        site_id: str,
        materialization_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> Literal["retry", "dead_letter"] | None:
        self.status = "retry"
        self.failures.append(error_code)
        return None


class _Frappe:
    def __init__(self, *, crash_once: bool = False) -> None:
        self.crash_once = crash_once
        self.calls = 0
        self.created: dict[str, tuple[str, FrappeDraftReceipt]] = {}
        self.intents: list[Any] = []

    def apply(
        self,
        intent: Any,
        *,
        request_id: str,
        request_digest: str,
    ) -> FrappeDraftReceipt:
        self.calls += 1
        self.intents.append(intent)
        existing = self.created.get(request_id)
        if existing is not None:
            if existing[0] != request_digest:
                raise IdempotencyConflict("request body conflict")
            return existing[1]
        receipt = FrappeDraftReceipt(
            doctype=intent.doctype,
            name="DRAFT-0001",
            revision=0,
            request_id=request_id,
            request_digest=request_digest,
        )
        self.created[request_id] = (request_digest, receipt)
        if self.crash_once:
            self.crash_once = False
            raise _CrashAfterFrappeCommit
        return receipt


def _claim(now: datetime) -> MaterializationClaim:
    return MaterializationClaim(
        materialization_id="materialization-1",
        proposal_id="proposal-1",
        site_id="site-a",
        attempt=0,
        max_attempts=3,
        lease_owner="",
        lease_expires_at=now,
        envelope=MaterializationEnvelope(
            proposal_id="proposal-1",
            task_id="task-1",
            processing_purpose="metric_reporting",
            origin="AI",
            review_status="AI Draft",
            action_type="internal.ai_draft.propose",
            subject_type="GBOS Synthetic Executive Snapshot",
            subject_ref="snapshot-1",
            subject_revision=1,
            evidence_refs=("evidence-1",),
            model_name="deepseek-v4-flash",
            model_version="deepseek-v4-flash",
            policy_version="action-guard-v1",
            proposal={
                "payload": {
                    "title": "Communication-based observation",
                    "summary": "Leadership communication pattern",
                    "is_official_metric": False,
                }
            },
        ),
    )


class _ContextResolver:
    def __init__(self, context: MaterializationContext | None) -> None:
        self.context = context
        self.requests: list[MaterializationContextRequest] = []

    def resolve(
        self,
        request: MaterializationContextRequest,
    ) -> MaterializationContext | None:
        self.requests.append(request)
        return self.context


def test_ceo_proposal_materializes_as_non_formal_informal_observation() -> None:
    intent = TrustedMaterializer().materialize(
        MaterializationEnvelope(
            proposal_id="proposal-1",
            task_id="task-1",
            processing_purpose="metric_reporting",
            origin="AI",
            review_status="AI Draft",
            action_type="internal.ai_draft.propose",
            subject_type="GBOS Synthetic Executive Snapshot",
            subject_ref="snapshot-1",
            subject_revision=1,
            evidence_refs=("evidence-1",),
            model_name="deepseek-v4-flash",
            model_version="deepseek-v4-flash",
            policy_version="action-guard-v1",
            proposal={
                "payload": {
                    "title": "Communication-based observation",
                    "summary": "Leadership communication pattern",
                    "is_official_metric": False,
                }
            },
        ),
        context=MaterializationContext(team="team-a"),
    )

    assert intent.operation == "create"
    assert intent.doctype == "GBOS Informal Observation"
    assert intent.values["is_official_metric"] is False


def test_worker_recovers_after_crash_without_creating_a_second_frappe_draft() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    clock = _Clock(now)
    repository = _Repository(_claim(now))
    client = _Frappe(crash_once=True)
    context_resolver = _ContextResolver(MaterializationContext(team="team-a"))
    worker = MaterializationWorker(
        repository=repository,
        client=client,
        materializer=TrustedMaterializer(),
        context_resolver=context_resolver,
        worker_id="worker-a",
        clock=clock,
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )

    with pytest.raises(_CrashAfterFrappeCommit):
        worker.run_once("site-a")

    clock.now += timedelta(seconds=11)
    result = worker.run_once("site-a")

    assert result.status == "succeeded"
    assert client.calls == 2
    assert len(client.created) == 1
    assert context_resolver.requests[0] == MaterializationContextRequest(
        site_id="site-a",
        task_id="task-1",
        processing_purpose="metric_reporting",
        proposal_id="proposal-1",
        subject_type="GBOS Synthetic Executive Snapshot",
        subject_ref="snapshot-1",
        subject_revision=1,
    )
    assert client.intents[0].values["team"] == "team-a"
    assert repository.receipt is not None
    assert set(repository.receipt.__dataclass_fields__) == {
        "doctype",
        "name",
        "revision",
        "request_id",
        "request_digest",
    }
    assert "summary" not in repr(repository.receipt)


def test_worker_heartbeats_before_context_resolution_and_frappe_apply() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    events: list[str] = []

    class TracingRepository(_Repository):
        def heartbeat_materialization(self, *args: Any, **kwargs: Any) -> None:
            events.append("heartbeat")
            super().heartbeat_materialization(*args, **kwargs)

    class TracingContextResolver(_ContextResolver):
        def resolve(
            self,
            request: MaterializationContextRequest,
        ) -> MaterializationContext | None:
            events.append("context")
            return super().resolve(request)

    class TracingFrappe(_Frappe):
        def apply(self, *args: Any, **kwargs: Any) -> FrappeDraftReceipt:
            events.append("frappe")
            return super().apply(*args, **kwargs)

    repository = TracingRepository(_claim(now))
    worker = MaterializationWorker(
        repository=repository,
        client=TracingFrappe(),
        materializer=TrustedMaterializer(),
        context_resolver=TracingContextResolver(MaterializationContext(team="team-a")),
        worker_id="worker-a",
        clock=_Clock(now),
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )

    result = worker.run_once("site-a")

    assert result.status == "succeeded"
    assert events == ["heartbeat", "context", "heartbeat", "frappe"]
    assert repository.heartbeats == [
        ("materialization-1", 1),
        ("materialization-1", 1),
    ]


@pytest.mark.parametrize(
    ("lost_heartbeat", "context_calls", "frappe_calls"),
    [
        (1, 0, 0),
        (2, 1, 0),
    ],
)
def test_worker_stops_side_effects_when_heartbeat_loses_lease(
    lost_heartbeat: int,
    context_calls: int,
    frappe_calls: int,
) -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    repository = _Repository(_claim(now))
    repository.lose_heartbeat_at = lost_heartbeat
    context_resolver = _ContextResolver(MaterializationContext(team="team-a"))
    client = _Frappe()
    worker = MaterializationWorker(
        repository=repository,
        client=client,
        materializer=TrustedMaterializer(),
        context_resolver=context_resolver,
        worker_id="worker-a",
        clock=_Clock(now),
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )

    result = worker.run_once("site-a")

    assert result.status == "lease_lost"
    assert len(context_resolver.requests) == context_calls
    assert client.calls == frappe_calls
    assert repository.failures == []


def test_worker_reports_lease_loss_without_retrying_a_stale_ack() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    clock = _Clock(now)
    repository = _Repository(_claim(now))
    client = _Frappe()
    worker = MaterializationWorker(
        repository=repository,
        client=client,
        materializer=TrustedMaterializer(),
        context_resolver=_ContextResolver(MaterializationContext(team="team-a")),
        worker_id="worker-a",
        clock=clock,
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )
    original_ack = repository.acknowledge_materialization

    def lose_lease(*args: Any, **kwargs: Any) -> FrappeDraftReceipt:
        raise LeaseConflict("materialization lease lost")

    repository.acknowledge_materialization = lose_lease  # type: ignore[method-assign]
    result = worker.run_once("site-a")
    repository.acknowledge_materialization = original_ack  # type: ignore[method-assign]

    assert result.status == "lease_lost"
    assert repository.failures == []


def test_worker_reports_dead_letter_when_final_attempt_fails() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    clock = _Clock(now)
    repository = _Repository(_claim(now))

    class FinalAttemptRepository(_Repository):
        def fail_materialization(
            self,
            site_id: str,
            materialization_id: str,
            *,
            worker_id: str,
            expected_attempt: int,
            now: datetime,
            retry_at: datetime,
            error_code: str,
        ) -> Literal["retry", "dead_letter"] | None:
            self.status = "dead_letter"
            return "dead_letter"

    class FailingFrappe(_Frappe):
        def apply(self, *args: Any, **kwargs: Any) -> FrappeDraftReceipt:
            raise RuntimeError("sensitive Frappe response body")

    final_repository = FinalAttemptRepository(repository.claim)
    worker = MaterializationWorker(
        repository=final_repository,
        client=FailingFrappe(),
        materializer=TrustedMaterializer(),
        context_resolver=_ContextResolver(MaterializationContext(team="team-a")),
        worker_id="worker-a",
        clock=clock,
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )

    result = worker.run_once("site-a")

    assert result.status == "dead_letter"
    assert "sensitive" not in repr(result)


def test_worker_fails_closed_when_controlled_team_context_is_missing() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    repository = _Repository(_claim(now))
    client = _Frappe()
    worker = MaterializationWorker(
        repository=repository,
        client=client,
        materializer=TrustedMaterializer(),
        context_resolver=_ContextResolver(None),
        worker_id="worker-a",
        clock=_Clock(now),
        lease_duration=timedelta(seconds=10),
        retry_delay=timedelta(seconds=1),
    )

    result = worker.run_once("site-a")

    assert result.status == "retry"
    assert result.error_code == "materialization_failed"
    assert client.calls == 0


def test_ack_replay_is_idempotent_and_changed_receipt_is_a_body_conflict() -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    repository = _Repository(_claim(now))
    claim = repository.claim_materialization(
        "site-a",
        worker_id="worker-a",
        now=now,
        lease_duration=timedelta(seconds=10),
    )
    assert claim is not None
    receipt = FrappeDraftReceipt(
        doctype="GBOS Informal Observation",
        name="DRAFT-0001",
        revision=0,
        request_id="materialization-1",
        request_digest="a" * 64,
    )

    first = repository.acknowledge_materialization(
        "site-a",
        "materialization-1",
        worker_id="worker-a",
        expected_attempt=1,
        now=now + timedelta(seconds=1),
        receipt=receipt,
    )
    replay = repository.acknowledge_materialization(
        "site-a",
        "materialization-1",
        worker_id="worker-a",
        expected_attempt=1,
        now=now + timedelta(seconds=2),
        receipt=receipt,
    )

    assert first == replay
    with pytest.raises(IdempotencyConflict, match="conflict"):
        repository.acknowledge_materialization(
            "site-a",
            "materialization-1",
            worker_id="worker-a",
            expected_attempt=1,
            now=now + timedelta(seconds=2),
            receipt=FrappeDraftReceipt(
                doctype=receipt.doctype,
                name="DRAFT-CHANGED",
                revision=receipt.revision,
                request_id=receipt.request_id,
                request_digest=receipt.request_digest,
            ),
        )


def test_materialization_migration_opens_only_the_outbox_state_machine() -> None:
    migration = (
        (ROOT / "services" / "agent_runtime" / "migrations" / "004_local_pilot_materialization.sql")
        .read_text(encoding="utf-8")
        .casefold()
    )

    for status in ("pending", "running", "succeeded", "retry", "dead_letter"):
        assert f"'{status}'" in migration
    for column in (
        "attempt",
        "max_attempts",
        "lease_owner",
        "lease_expires_at",
        "receipt_doctype",
        "receipt_name",
        "receipt_revision",
        "receipt_request_id",
        "receipt_digest",
    ):
        assert column in migration
    assert "drop trigger if exists proposal_materialization_outbox_immutable" in migration
    assert "before delete on agent_runtime.proposal_materialization_outbox" in migration
    assert "action_proposals_immutable" in migration
    assert "enforce_materialization_state_transition" in migration
    assert "invalid materialization state transition" in migration
    assert "enable row level security" in migration
    assert "force row level security" in migration
    assert "grant select, insert, update" in migration
    assert "grant delete" not in migration


def test_materialization_heartbeat_migration_allows_only_fenced_lease_extension() -> None:
    migration = (
        (
            ROOT
            / "services"
            / "agent_runtime"
            / "migrations"
            / "006_local_pilot_materialization_heartbeat.sql"
        )
        .read_text(encoding="utf-8")
        .casefold()
    )

    assert "old.status = 'running'" in migration
    assert "new.status = 'running'" in migration
    assert "new.attempt = old.attempt" in migration
    assert "new.lease_owner is not distinct from old.lease_owner" in migration
    assert "new.lease_expires_at > old.lease_expires_at" in migration
    for protected in (
        "next_attempt_at",
        "last_error_code",
        "receipt_doctype",
        "receipt_name",
        "receipt_revision",
        "receipt_request_id",
        "receipt_digest",
    ):
        assert f"new.{protected} is not distinct from old.{protected}" in migration


def test_postgres_repository_exposes_fenced_materialization_transitions() -> None:
    for method in (
        "claim_materialization",
        "heartbeat_materialization",
        "acknowledge_materialization",
        "fail_materialization",
        "materialization_health",
    ):
        assert callable(getattr(PostgresAgentTaskRepository, method, None))


def test_materialization_public_api_exposes_only_safe_boundary_types() -> None:
    for name in (
        "FrappeDraftClient",
        "FrappeDraftReceipt",
        "MaterializationClaim",
        "MaterializationContext",
        "MaterializationContextRequest",
        "MaterializationContextResolver",
        "MaterializationHealth",
        "MaterializationWorker",
        "PostgresAgentReadService",
    ):
        assert getattr(agent_runtime, name, None) is not None
