from __future__ import annotations

from datetime import timedelta

from .conftest import DIGEST_A, NOW, OPAQUE_FROM, SITE


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
