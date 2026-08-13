from __future__ import annotations

import copy
from datetime import timedelta

import pytest

from services.action_guard.policy import ActionGuard
from services.email_gateway.models import TenantScope
from services.email_gateway.outbound import (
    CommandIngestService,
    CommandPublication,
    InMemoryOutboundRepository,
)
from services.email_gateway.provider import (
    ProviderOutcome,
    ProviderSubmissionResult,
    ProviderSubmissionUncertain,
)
from services.email_gateway.worker import EmailSendWorker, WorkerAuthorityState
from tests.email_gateway.fakes.provider import (
    NOW,
    FakeEmailProvider,
    authority_for,
    closed_command,
)


def _publication(command: dict[str, object]) -> CommandPublication:
    return CommandPublication(
        publication_ref="PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        attempt=1,
        generation=1,
        fence_token="FNC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        payload_digest="sha256:" + str(command["payload_sha256"]),
    )


def _authority_state(*, stopped: bool = False) -> WorkerAuthorityState:
    return WorkerAuthorityState(
        emergency_stop_active=stopped,
        execution_enabled=True,
        command_unexpired=True,
        identity_active=True,
        mailbox_revision_current=True,
        route_authority_current=True,
    )


def test_closed_approved_command_fixture_reaches_fake_receipt_once_across_replay() -> None:
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = InMemoryOutboundRepository()
    ingest = CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=lambda _scope, _publication, _command: authority_for(command),
        clock=lambda: NOW,
    )
    first = ingest.accept(scope, publication=_publication(command), command=command)
    assert ingest.accept(scope, publication=_publication(command), command=command) == first
    provider = FakeEmailProvider(
        ProviderSubmissionResult(
            outcome=ProviderOutcome.DELIVERED,
            safe_code="fake_delivered",
            provider_receipt_ref="fake-receipt-1",
        )
    )
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _authority_state(),
        lease_duration=timedelta(seconds=30),
    )

    assert worker.run_once(scope).state == "delivered"
    assert worker.run_once(scope).state == "idle"
    assert len(provider.submissions) == 1
    assert repository.command_receipt_count(scope) == repository.outbox_count(scope) == 1


@pytest.mark.parametrize("case", ["stale_owner", "revoked_recipient"])
def test_live_authority_rejection_never_creates_outbox(case: str) -> None:
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = InMemoryOutboundRepository()
    live = authority_for(command)
    if case == "stale_owner":
        live = authority_for({**command, "owner_user_ref": "USR-01ARZ3NDEKTSV4RRFFQ69G5FAX"})
    else:
        changed = copy.deepcopy(command)
        changed["participants"][1]["identity_mapping_revision"] += 1
        live = authority_for(changed)
    ingest = CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=lambda _scope, _publication, _command: live,
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError):
        ingest.accept(scope, publication=_publication(command), command=command)
    assert repository.outbox_count(scope) == 0


def test_uncertainty_and_emergency_stop_each_preserve_one_effect_maximum() -> None:
    command = closed_command()
    scope = TenantScope(command["site_id"], command["processing_purpose"])
    repository = InMemoryOutboundRepository()
    receipt = CommandIngestService(
        repository=repository,
        action_guard=ActionGuard(),
        authority_resolver=lambda _scope, _publication, _command: authority_for(command),
        clock=lambda: NOW,
    ).accept(scope, publication=_publication(command), command=command)
    provider = FakeEmailProvider(ProviderSubmissionUncertain("lost response"))
    stopped = False
    worker = EmailSendWorker(
        repository=repository,
        provider=provider,
        worker_id="fake-send-worker-1",
        clock=lambda: NOW,
        authority_check=lambda _envelope: _authority_state(stopped=stopped),
        lease_duration=timedelta(seconds=30),
    )
    assert worker.run_once(scope).state == "reconciliation_required"
    stopped = True
    assert worker.run_once(scope).state == "idle"
    assert len(provider.submissions) == 1
    assert repository.get(scope, receipt.send_outbox_ref).state == "reconciliation_required"
