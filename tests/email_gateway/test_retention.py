from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .conftest import DIGEST_A, NOW, OPAQUE_FROM, SITE

ROOT = Path(__file__).resolve().parents[2]


class _VerifiedObserverReceipt:
    def verify_tombstone(self, _scope, _projection, *, now):
        return now.tzinfo is not None


def test_retention_scheduler_requires_an_explicit_observer_verifier() -> None:
    import inspect

    from services.email_gateway.retention import RetentionScheduler

    parameter = inspect.signature(RetentionScheduler).parameters["observer_tombstone_verifier"]
    assert parameter.default is inspect.Parameter.empty


def test_retention_only_expires_unconfirmed_projection_with_observer_receipt(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import RetentionPlanner

    expired = ContentProjection(
        projection_ref="PRJ-01",
        site_id=SITE,
        kind="unconfirmed_display",
        identity_ref=OPAQUE_FROM,
        evidence_ref="EVD-01",
        expires_at=NOW - timedelta(seconds=1),
        observer_expiration_receipt_ref="EXP-01",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    blocked = ContentProjection(
        projection_ref="PRJ-02",
        site_id=SITE,
        kind="unconfirmed_subject",
        identity_ref=None,
        evidence_ref="EVD-02",
        expires_at=NOW - timedelta(seconds=1),
        observer_expiration_receipt_ref=None,
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    active = ContentProjection(
        projection_ref="PRJ-03",
        site_id=SITE,
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-03",
        expires_at=NOW - timedelta(seconds=1),
        observer_expiration_receipt_ref="EXP-03",
        payload_digest=DIGEST_A,
        active_draft_ref="DRF-01",
        confirmed=False,
    )
    assert RetentionPlanner().plan(scope, (expired, blocked, active), now=NOW) == ("PRJ-01",)


def test_retention_never_expires_confirmed_crm_or_audit(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import RetentionPlanner

    confirmed = ContentProjection(
        projection_ref="PRJ-01",
        site_id=SITE,
        kind="confirmed_crm_metadata",
        identity_ref=OPAQUE_FROM,
        evidence_ref="EVD-01",
        expires_at=NOW,
        observer_expiration_receipt_ref="EXP-01",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=True,
    )
    assert RetentionPlanner().plan(scope, (confirmed,), now=NOW) == ()


def test_terminal_draft_reference_expires_exactly_thirty_days_after_terminal_time(
    scope,
) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import (
        RetentionPlanner,
        terminal_draft_expires_at,
    )

    terminal_at = NOW - timedelta(days=30)
    expires_at = terminal_draft_expires_at(terminal_at)
    terminal = ContentProjection(
        projection_ref="PRJ-TERMINAL",
        site_id=SITE,
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-TERMINAL",
        expires_at=expires_at,
        observer_expiration_receipt_ref="TMB-TERMINAL",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    active = ContentProjection(
        projection_ref="PRJ-ACTIVE",
        site_id=SITE,
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-ACTIVE",
        expires_at=expires_at,
        observer_expiration_receipt_ref="TMB-ACTIVE",
        payload_digest=DIGEST_A,
        active_draft_ref="DRF-ACTIVE",
        confirmed=False,
    )

    assert expires_at == terminal_at + timedelta(days=30)
    assert RetentionPlanner().plan(scope, (terminal, active), now=NOW) == ("PRJ-TERMINAL",)


def test_legal_hold_or_missing_observer_tombstone_blocks_content_expiration(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import RetentionPlanner

    held = ContentProjection(
        projection_ref="PRJ-HELD",
        site_id=SITE,
        kind="unconfirmed_subject",
        identity_ref=None,
        evidence_ref="EVD-HELD",
        expires_at=NOW,
        observer_expiration_receipt_ref="TMB-HELD",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    missing_tombstone = ContentProjection(
        projection_ref="PRJ-MISSING",
        site_id=SITE,
        kind="unconfirmed_display",
        identity_ref=None,
        evidence_ref="EVD-MISSING",
        expires_at=NOW,
        observer_expiration_receipt_ref=None,
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )

    assert (
        RetentionPlanner().plan(
            scope,
            (held, missing_tombstone),
            now=NOW,
            legal_hold_evidence_refs=frozenset({"EVD-HELD"}),
        )
        == ()
    )


def test_retention_run_is_idempotent_fenced_bounded_serial_and_dry_run_is_read_only(
    scope,
) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import (
        InMemoryRetentionRunRepository,
        RetentionScheduler,
    )

    projections = tuple(
        ContentProjection(
            projection_ref=f"PRJ-{index:02d}",
            site_id=SITE,
            kind="unconfirmed_subject",
            identity_ref=None,
            evidence_ref=f"EVD-{index:02d}",
            expires_at=NOW,
            observer_expiration_receipt_ref=f"TMB-{index:02d}",
            payload_digest=DIGEST_A,
            active_draft_ref=None,
            confirmed=False,
        )
        for index in range(3)
    )
    repository = InMemoryRetentionRunRepository()
    scheduler = RetentionScheduler(
        repository,
        emergency_stop=lambda: False,
        observer_tombstone_verifier=_VerifiedObserverReceipt(),
    )

    dry_run = scheduler.schedule(
        scope,
        run_ref="RTR-DRY",
        idempotency_key="retention-dry",
        projections=projections,
        dry_run=True,
        now=NOW,
    )
    assert (
        scheduler.schedule(
            scope,
            run_ref="RTR-DRY-REPLAY",
            idempotency_key="retention-dry",
            projections=projections,
            dry_run=True,
            now=NOW,
        )
        == dry_run
    )
    completed_dry_run = scheduler.run_once(
        scope,
        worker_id="retention-worker",
        now=NOW,
        limit=2,
    )
    assert completed_dry_run is not None
    assert completed_dry_run.status == "completed"
    assert completed_dry_run.planned_count == 2
    assert completed_dry_run.expired_count == 0
    assert repository.expiration_receipts(scope) == ()

    scheduler.schedule(
        scope,
        run_ref="RTR-EXECUTE",
        idempotency_key="retention-execute",
        projections=projections,
        dry_run=False,
        now=NOW + timedelta(seconds=1),
    )
    completed = scheduler.run_once(
        scope,
        worker_id="retention-worker",
        now=NOW + timedelta(seconds=1),
        limit=2,
    )
    assert completed is not None
    assert completed.status == "completed"
    assert completed.expired_count == 2
    receipts = repository.expiration_receipts(scope)
    assert tuple(item.projection_ref for item in receipts) == ("PRJ-00", "PRJ-01")
    assert len({item.expiration_receipt_ref for item in receipts}) == 2


def test_retention_emergency_stop_and_safe_failure_leave_work_retryable(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import (
        InMemoryRetentionRunRepository,
        RetentionScheduler,
    )

    projection = ContentProjection(
        projection_ref="PRJ-FAIL",
        site_id=SITE,
        kind="unconfirmed_subject",
        identity_ref=None,
        evidence_ref="EVD-FAIL",
        expires_at=NOW,
        observer_expiration_receipt_ref="TMB-FAIL",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    stopped = RetentionScheduler(
        InMemoryRetentionRunRepository(),
        emergency_stop=lambda: True,
        observer_tombstone_verifier=_VerifiedObserverReceipt(),
    )
    stopped.schedule(
        scope,
        run_ref="RTR-STOP",
        idempotency_key="retention-stop",
        projections=(projection,),
        dry_run=False,
        now=NOW,
    )
    assert stopped.run_once(scope, worker_id="worker", now=NOW, limit=1) is None

    repository = InMemoryRetentionRunRepository(fail_projection_ref="PRJ-FAIL")
    scheduler = RetentionScheduler(
        repository,
        emergency_stop=lambda: False,
        observer_tombstone_verifier=_VerifiedObserverReceipt(),
    )
    scheduler.schedule(
        scope,
        run_ref="RTR-FAIL",
        idempotency_key="retention-fail",
        projections=(projection,),
        dry_run=False,
        now=NOW,
    )
    failed = scheduler.run_once(scope, worker_id="worker", now=NOW, limit=1)
    assert failed is not None
    assert failed.status == "retry"
    assert failed.safe_error_code == "retention_apply_failed"
    assert repository.expiration_receipts(scope) == ()


def test_human_retention_migration_freezes_draft_window_fencing_and_no_cas_delete() -> None:
    migration = ROOT / "services/email_gateway/migrations/006_email_gateway_human_retention.sql"
    sql = " ".join(migration.read_text(encoding="utf-8").lower().split())

    assert "alter table email_gateway.reply_drafts" in sql
    assert "terminal_at" in sql
    assert "content_expires_at" in sql
    assert "interval '30 days'" in sql
    assert "observer_tombstone_receipt_ref" in sql
    assert "legal_hold_ref" in sql
    assert "lease_generation" in sql
    assert "idempotency_key" in sql
    assert "next_attempt_at" in sql
    assert "for update skip locked" in sql
    assert "grant delete" not in sql
    assert "delete from observer." not in sql
    assert "update observer." not in sql


def test_execute_requires_positive_observer_tombstone_verification(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import (
        InMemoryRetentionRunRepository,
        RetentionScheduler,
    )

    projection = ContentProjection(
        projection_ref="PRJ-VERIFY",
        site_id=SITE,
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-VERIFY",
        expires_at=NOW,
        observer_expiration_receipt_ref="TMB-VERIFY",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )
    verified: list[tuple[str, str]] = []

    class Verifier:
        def verify_tombstone(self, checked_scope, checked_projection, *, now):
            verified.append((checked_scope.site_id, checked_projection.projection_ref))
            return False

    repository = InMemoryRetentionRunRepository()
    scheduler = RetentionScheduler(
        repository,
        emergency_stop=lambda: False,
        observer_tombstone_verifier=Verifier(),
    )
    scheduler.schedule(
        scope,
        run_ref="RTR-VERIFY",
        idempotency_key="retention-verify",
        projections=(projection,),
        dry_run=False,
        now=NOW,
    )

    failed = scheduler.run_once(scope, worker_id="worker", now=NOW, limit=1)

    assert failed is not None and failed.status == "retry"
    assert verified == [(SITE, "PRJ-VERIFY")]
    assert repository.expiration_receipts(scope) == ()


def test_dry_run_never_calls_observer_tombstone_verifier(scope) -> None:
    from services.email_gateway.models import ContentProjection
    from services.email_gateway.retention import (
        InMemoryRetentionRunRepository,
        RetentionScheduler,
    )

    projection = ContentProjection(
        projection_ref="PRJ-DRY-VERIFY",
        site_id=SITE,
        kind="draft_projection",
        identity_ref=None,
        evidence_ref="EVD-DRY-VERIFY",
        expires_at=NOW,
        observer_expiration_receipt_ref="TMB-DRY-VERIFY",
        payload_digest=DIGEST_A,
        active_draft_ref=None,
        confirmed=False,
    )

    class Verifier:
        def verify_tombstone(self, *_args, **_kwargs):
            raise AssertionError("dry-run must not contact Observer")

    repository = InMemoryRetentionRunRepository()
    scheduler = RetentionScheduler(
        repository,
        emergency_stop=lambda: False,
        observer_tombstone_verifier=Verifier(),
    )
    scheduler.schedule(
        scope,
        run_ref="RTR-DRY-VERIFY",
        idempotency_key="retention-dry-verify",
        projections=(projection,),
        dry_run=True,
        now=NOW,
    )

    completed = scheduler.run_once(scope, worker_id="worker", now=NOW, limit=1)

    assert completed is not None and completed.status == "completed"
    assert completed.expired_count == 0
