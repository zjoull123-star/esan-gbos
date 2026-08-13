from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from .conftest import NOW


def test_mailbox_sla_policy_contract_is_closed_bounded_and_has_no_pause() -> None:
    from jsonschema import Draft202012Validator

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/email_gateway/mailbox-sla-policy-v1.0.schema.json"
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "policy_ref",
        "revision",
        "first_response_duration_seconds",
        "effective_at",
    }
    duration = schema["properties"]["first_response_duration_seconds"]
    assert (duration["minimum"], duration["maximum"]) == (60, 604800)
    assert "pause" not in json.dumps(schema).lower()


def _policy(*, revision: int = 1, duration: int = 3600, effective_delta: int = -60):
    from services.email_gateway.sla import MailboxSlaPolicy

    return MailboxSlaPolicy(
        policy_ref="SLA-01",
        revision=revision,
        first_response_duration_seconds=duration,
        effective_at=NOW + timedelta(seconds=effective_delta),
    )


@pytest.mark.parametrize("duration", [59, 604801, True])
def test_sla_policy_rejects_out_of_bounds_duration(duration) -> None:
    from services.email_gateway.models import ValidationError

    with pytest.raises(ValidationError):
        _policy(duration=duration)


def test_sla_starts_at_observer_received_at_and_has_no_pause(scope) -> None:
    from services.email_gateway.sla import SlaClock

    clock = SlaClock.start(
        inbox_item_ref="INB-01",
        received_at=NOW,
        policy=_policy(),
        quarantined=False,
    )
    assert clock.started_at == NOW
    assert clock.due_at == NOW + timedelta(hours=1)
    assert clock.status == "running"
    assert not hasattr(clock, "paused_at")
    assert clock.preserve_for_revision(2, now=NOW + timedelta(minutes=3)) == clock


def test_quarantine_is_not_applicable(scope) -> None:
    from services.email_gateway.sla import SlaClock

    clock = SlaClock.start(
        inbox_item_ref="INB-01",
        received_at=NOW,
        policy=_policy(),
        quarantined=True,
    )
    assert (clock.status, clock.started_at, clock.due_at) == ("not_applicable", None, None)


def test_only_first_provider_accepted_receipt_completes_sla(scope) -> None:
    from services.email_gateway.models import AuthorizationError, ValidationError
    from services.email_gateway.sla import SlaClock

    clock = SlaClock.start(
        inbox_item_ref="INB-01",
        received_at=NOW,
        policy=_policy(),
        quarantined=False,
    )
    with pytest.raises(AuthorizationError):
        clock.complete(
            accepted_at=NOW + timedelta(minutes=4),
            provider_accepted=False,
            receipt_ref="RCP-01",
            policy_revision=1,
        )
    complete = clock.complete(
        accepted_at=NOW + timedelta(minutes=5),
        provider_accepted=True,
        receipt_ref="RCP-02",
        policy_revision=1,
    )
    assert complete.status == "met"
    assert complete.completed_at == NOW + timedelta(minutes=5)
    assert (
        complete.complete(
            accepted_at=NOW + timedelta(minutes=5),
            provider_accepted=True,
            receipt_ref="RCP-02",
            policy_revision=1,
        )
        == complete
    )
    assert (
        complete.complete(
            accepted_at=NOW + timedelta(minutes=6),
            provider_accepted=True,
            receipt_ref="RCP-03",
            policy_revision=1,
        )
        == complete
    )
    with pytest.raises(ValidationError, match="regression"):
        complete.complete(
            accepted_at=NOW + timedelta(minutes=4),
            provider_accepted=True,
            receipt_ref="RCP-04",
            policy_revision=1,
        )


def test_close_snapshots_outcome_and_reopen_preserves_original_clock(scope) -> None:
    from services.email_gateway.sla import SlaClock

    clock = SlaClock.start(
        inbox_item_ref="INB-01",
        received_at=NOW,
        policy=_policy(),
        quarantined=False,
    )
    closed = clock.close(NOW + timedelta(hours=2), policy_revision=1)
    assert (closed.status, closed.closed_outcome) == ("closed_overdue", "overdue")
    reopened = closed.reopen(NOW + timedelta(hours=3), policy_revision=1)
    assert reopened.started_at == clock.started_at
    assert reopened.due_at == clock.due_at
    assert reopened.status == "overdue"
    assert reopened.audit_revision == closed.audit_revision + 1


def test_sla_rejects_clock_regression_policy_drift_and_future_policy(scope) -> None:
    from services.email_gateway.models import RevisionConflict, ValidationError
    from services.email_gateway.sla import SlaClock

    with pytest.raises(ValidationError, match="effective"):
        SlaClock.start(
            inbox_item_ref="INB-01",
            received_at=NOW,
            policy=_policy(effective_delta=1),
            quarantined=False,
        )
    clock = SlaClock.start(
        inbox_item_ref="INB-01",
        received_at=NOW,
        policy=_policy(),
        quarantined=False,
    )
    with pytest.raises(ValidationError, match="regression"):
        clock.close(NOW - timedelta(seconds=1), policy_revision=1)
    with pytest.raises(RevisionConflict, match="policy"):
        clock.close(NOW + timedelta(seconds=1), policy_revision=2)

    completed = clock.complete(
        accepted_at=NOW + timedelta(minutes=5),
        provider_accepted=True,
        receipt_ref="RCP-01",
        policy_revision=1,
    )
    closed = completed.close(NOW + timedelta(minutes=10), policy_revision=1)
    with pytest.raises(ValidationError, match="regression"):
        closed.reopen(NOW + timedelta(minutes=7), policy_revision=1)


def test_transactional_sla_repository_rolls_back_inbox_sla_audit_and_receipt(scope) -> None:
    from dataclasses import replace

    from services.email_gateway.models import AuditEvent, InboxItem, canonical_digest
    from services.email_gateway.repositories.sla import InMemorySlaRepository
    from services.email_gateway.sla import SlaClock

    inbox = InboxItem.new(
        site_id=scope.site_id,
        mailbox_ref="MBX-01",
        message_ref="MSG-01",
        team_ref="TEM-01",
        received_at=NOW,
        state="unassigned",
    )
    clock = SlaClock.start(
        inbox_item_ref=inbox.inbox_item_ref,
        received_at=inbox.received_at,
        policy=_policy(),
        quarantined=False,
    )

    def fail(phase: str) -> None:
        if phase == "after_sla_write":
            raise RuntimeError("injected")

    repository = InMemorySlaRepository(transaction_failure_injector=fail)
    repository.save_inbox_with_sla(scope, inbox, clock)
    digest = canonical_digest({"operation": "close", "revision": 1})
    revised = replace(inbox, state="closed", revision=2, updated_at=NOW + timedelta(minutes=2))
    closed = clock.close(NOW + timedelta(minutes=2), policy_revision=1)
    audit = AuditEvent(
        audit_ref="AUD-01",
        site_id=scope.site_id,
        actor_ref="sales-01",
        event_type="inbox_transitioned",
        subject_ref=inbox.inbox_item_ref,
        request_id="REQ-01",
        idempotency_key="audit:close-01",
        payload_digest=digest,
        occurred_at=NOW + timedelta(minutes=2),
    )
    with pytest.raises(RuntimeError, match="injected"):
        repository.apply_inbox_sla_operation(
            scope,
            before=inbox,
            revised=revised,
            sla_before=clock,
            sla_revised=closed,
            audit_event=audit,
            idempotency_key="close-01",
            payload_digest=digest,
        )
    assert repository.get_inbox(scope, inbox.inbox_item_ref) == inbox
    assert repository.get_sla(scope, inbox.inbox_item_ref) == clock
    assert repository.audit_count(scope) == 0
    assert repository.replay(scope, "close-01", digest) is None
