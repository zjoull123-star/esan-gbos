from __future__ import annotations

from datetime import timedelta

from services.email_gateway.provider import (
    ProviderOutcome,
    ProviderSubmissionResult,
    ProviderSubmissionUncertain,
)
from services.email_gateway.worker import EmailSendWorker
from tests.email_gateway.fakes.provider import NOW, FakeEmailProvider
from tests.email_gateway.test_worker import _allowed, _queued


def test_uncertain_submission_is_terminal_for_normal_worker_and_never_blind_retried() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(
        ProviderSubmissionUncertain("response lost"),
        ProviderSubmissionResult(
            outcome=ProviderOutcome.ACCEPTED,
            safe_code="must_not_be_used",
            provider_receipt_ref="duplicate",
        ),
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
    )

    assert worker.run_once(scope).state == "reconciliation_required"
    assert worker.run_once(scope).state == "idle"
    assert len(provider.submissions) == 1
    assert repository.get(scope, receipt.send_outbox_ref).state == "reconciliation_required"


def test_reconciliation_requeues_only_when_stable_lookup_proves_not_submitted() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(
        ProviderSubmissionUncertain("response lost"),
        ProviderSubmissionResult(
            outcome=ProviderOutcome.NOT_SUBMITTED,
            safe_code="stable_id_not_submitted",
            provider_receipt_ref=None,
        ),
        ProviderSubmissionResult(
            outcome=ProviderOutcome.DELIVERED,
            safe_code="provider_delivered",
            provider_receipt_ref="fake-delivery-1",
        ),
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
    )
    assert worker.run_once(scope).state == "reconciliation_required"

    reconciled = worker.reconcile(scope, receipt.send_outbox_ref)
    sent = worker.run_once(scope)

    assert reconciled.state == "queued"
    assert sent.state == "delivered"
    assert len(provider.submissions) == 2


def test_authoritative_lookup_records_distinct_terminal_outcome_and_receipt() -> None:
    states = {
        ProviderOutcome.ACCEPTED: "provider_accepted",
        ProviderOutcome.DELIVERED: "delivered",
        ProviderOutcome.BOUNCED: "bounced",
        ProviderOutcome.PERMANENTLY_REJECTED: "provider_rejected",
    }
    for outcome, expected_state in states.items():
        _command, scope, repository, receipt = _queued()
        provider = FakeEmailProvider(
            ProviderSubmissionUncertain("response lost"),
            ProviderSubmissionResult(
                outcome=outcome,
                safe_code=f"lookup_{outcome.value}",
                provider_receipt_ref=f"provider-{outcome.value}-receipt",
            ),
        )
        worker = EmailSendWorker(
            repository=repository,
            provider=provider,
            worker_id="fake-send-worker-1",
            clock=lambda: NOW,
            authority_check=lambda _envelope: _allowed(),
            lease_duration=timedelta(seconds=30),
        )
        worker.run_once(scope)
        result = worker.reconcile(scope, receipt.send_outbox_ref)
        assert result.state == expected_state
        assert worker.run_once(scope).state == "idle"
        assert len(provider.submissions) == 1
        assert repository.receipt_count(scope, receipt.send_outbox_ref) == 1


def test_unknown_lookup_remains_manual_and_never_requeues_same_approval() -> None:
    _command, scope, repository, receipt = _queued()
    provider = FakeEmailProvider(
        ProviderSubmissionUncertain("response lost"),
        ProviderSubmissionResult(
            outcome=ProviderOutcome.UNKNOWN,
            safe_code="lookup_unknown",
            provider_receipt_ref=None,
        ),
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _allowed(),
        lease_duration=timedelta(seconds=30),
    )

    worker.run_once(scope)
    result = worker.reconcile(scope, receipt.send_outbox_ref)

    assert result.state == "reconciliation_required"
    assert worker.run_once(scope).state == "idle"
    assert len(provider.submissions) == 1
    assert repository.receipt_count(scope, receipt.send_outbox_ref) == 0
