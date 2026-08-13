from __future__ import annotations

import json
from datetime import timedelta

from services.action_guard.policy import ActionGuard
from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import (
    CommandIngestService,
    CommandPublication,
    InMemoryOutboundRepository,
)
from services.email_gateway.provider import ProviderOutcome, ProviderSubmissionResult
from services.email_gateway.send_outbox import PostgresEmailSendRepository
from services.email_gateway.worker import EmailSendWorker, WorkerAuthorityState
from tests.email_gateway.fakes.provider import (
    NOW,
    FakeEmailProvider,
    authority_for,
    closed_command,
)


def _queued():
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = InMemoryOutboundRepository()
    receipt = CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=lambda _scope, _publication, _command: authority_for(command),
        clock=lambda: NOW,
    ).accept(
        scope,
        publication=CommandPublication(
            publication_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            attempt=1,
            generation=1,
            fence_token="FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            payload_digest="sha256:" + command["payload_sha256"],
        ),
        command=command,
    )
    return command, scope, repository, receipt


def _allowed() -> WorkerAuthorityState:
    return WorkerAuthorityState(
        emergency_stop_active=False,
        execution_enabled=True,
        command_unexpired=True,
        identity_active=True,
        mailbox_revision_current=True,
        route_authority_current=True,
    )


def test_one_command_has_one_fenced_attempt_and_one_provider_submission() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(
        ProviderSubmissionResult(
            outcome=ProviderOutcome.ACCEPTED,
            safe_code="provider_accepted",
            provider_receipt_ref="fake-receipt-1",
        )
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
    )

    result = worker.run_once(scope)
    replay = worker.run_once(scope)

    assert result.state == "provider_accepted"
    assert replay.state == "idle"
    assert len(provider.submissions) == 1
    assert repository.attempt_count(scope, receipt.send_outbox_ref) == 1
    assert repository.receipt_count(scope, receipt.send_outbox_ref) == 1


def test_fatal_live_authority_drift_stops_before_provider_call() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(AssertionError("provider must not run"))
    changed = WorkerAuthorityState(
        emergency_stop_active=False,
        execution_enabled=True,
        command_unexpired=True,
        identity_active=False,
        mailbox_revision_current=True,
        route_authority_current=True,
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: changed,
        lease_duration=timedelta(seconds=30),
    )

    assert worker.run_once(scope).state == "authority_review_required"
    assert provider.submissions == []
    assert repository.get(scope, receipt.send_outbox_ref).state == "authority_review_required"


def test_live_authority_is_checked_exactly_once_immediately_before_provider_call() -> None:
    _command, scope, repository, _receipt = _queued()
    provider = FakeEmailProvider(
        ProviderSubmissionResult(
            outcome=ProviderOutcome.ACCEPTED,
            safe_code="provider_accepted",
            provider_receipt_ref="fake-receipt-1",
        )
    )
    checks: list[str] = []

    def authority(envelope):
        checks.append(envelope.command_ref)
        return _allowed()

    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=authority,
        lease_duration=timedelta(seconds=30),
    )

    assert worker.run_once(scope).state == "provider_accepted"
    assert len(checks) == 1
    assert len(provider.submissions) == 1


def test_dynamic_emergency_stop_after_claim_prevents_provider_effect() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(AssertionError("provider must not run"))
    stops = iter((None, "emergency_stop_active"))
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
        runtime_stop_reader=lambda: next(stops),
    )

    result = worker.run_once(scope)

    assert result.state == "authority_review_required"
    assert provider.submissions == []
    assert repository.get(scope, receipt.send_outbox_ref).state == "authority_review_required"


def test_dynamic_external_send_stop_before_claim_has_no_outbox_attempt_or_effect() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(AssertionError("provider must not run"))
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
        runtime_stop_reader=lambda: "external_send_disabled",
    )

    assert worker.run_once(scope).state == "idle"
    assert repository.attempt_count(scope, receipt.send_outbox_ref) == 0
    assert provider.submissions == []


def test_postgres_worker_claims_state_with_skip_locked_and_appends_fenced_receipt() -> None:
    command = closed_command()

    class Cursor:
        def __init__(self) -> None:
            self.queries: list[str] = []
            self.row = None

        def execute(self, query: str, _params: tuple[object, ...] = ()) -> None:
            self.queries.append(query)
            if "state.state = 'queued'" in query:
                self.row = (
                    "SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "ECR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "sha256:" + command["payload_sha256"],
                    json.dumps(command),
                    "queued",
                    NOW,
                    0,
                    0,
                )
            elif "RETURNING send_outbox_ref" in query:
                self.row = ("SOB-01ARZ3NDEKTSV4RRFFQ69G5FAV",)
            else:
                self.row = None

        def fetchone(self):
            row, self.row = self.row, None
            return row

        def close(self) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.db = Cursor()
            self.commits = 0

        def cursor(self) -> Cursor:
            return self.db

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            raise AssertionError("worker transaction must not roll back")

    connection = Connection()
    repository = PostgresEmailSendRepository(
        connection,  # type: ignore[arg-type]
        actual_database_role="gbos_email_send_worker",
    )
    scope = TenantScope(command["site_id"], command["processing_purpose"])

    claim = repository.claim(
        scope,
        worker_id="fake-send-worker-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    result = repository.finish(
        scope,
        claim,
        outcome="accepted",
        safe_code="provider_accepted",
        provider_receipt_ref="fake-receipt-1",
        now=NOW,
    )

    sql = "\n".join(connection.db.queries)
    assert result.state == "provider_accepted"
    assert "FOR UPDATE OF state SKIP LOCKED" in sql
    assert "UPDATE email_gateway.send_outbox_state" in sql
    assert "UPDATE email_gateway.send_outbox\n" not in sql
    assert sql.count("INSERT INTO email_gateway.send_attempts") == 2
    assert "INSERT INTO email_gateway.provider_receipts" in sql
    assert connection.commits == 2
