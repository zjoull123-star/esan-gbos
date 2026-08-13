from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from .conftest import DIGEST_A, DIGEST_B, NOW, OPAQUE_FROM, SITE


def _projection():
    from services.email_gateway.models import IdentityProjection

    return IdentityProjection(
        site_id=SITE,
        processing_purpose="sales_follow_up",
        opaque_address_ref=OPAQUE_FROM,
        external_identity_ref="EXT-01",
        external_identity_revision=1,
        identity_type="Party",
        team_ref="TEM-01",
        status="confirmed",
        projection_receipt_ref="IPR-01",
        observed_at=NOW,
        payload_digest=DIGEST_A,
    )


def test_projection_replay_and_monotonic_revision(scope) -> None:
    from services.email_gateway.identity_projection import IdentityProjectionService
    from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository

    service = IdentityProjectionService(InMemoryIdentityProjectionRepository())
    first = service.apply(scope, _projection())
    assert service.apply(scope, _projection()) == first
    revoked = replace(
        _projection(),
        external_identity_revision=2,
        status="revoked",
        projection_receipt_ref="IPR-02",
        payload_digest=DIGEST_B,
    )
    assert service.apply(scope, revoked).status == "revoked"
    assert service.get(scope, OPAQUE_FROM) == revoked


def test_projection_same_revision_drift_and_stale_replay_fail(scope) -> None:
    from services.email_gateway.identity_projection import IdentityProjectionService
    from services.email_gateway.models import IdempotencyConflict, RevisionConflict
    from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository

    service = IdentityProjectionService(InMemoryIdentityProjectionRepository())
    service.apply(scope, _projection())
    with pytest.raises(IdempotencyConflict):
        service.apply(scope, replace(_projection(), payload_digest=DIGEST_B))
    service.apply(
        scope,
        replace(
            _projection(),
            external_identity_revision=2,
            status="revoked",
            projection_receipt_ref="IPR-02",
            payload_digest=DIGEST_B,
        ),
    )
    with pytest.raises(RevisionConflict):
        service.apply(scope, _projection())


def test_projection_cross_site_is_rejected(scope) -> None:
    from services.email_gateway.identity_projection import IdentityProjectionService
    from services.email_gateway.models import ScopeViolation
    from services.email_gateway.repositories.identity import InMemoryIdentityProjectionRepository

    service = IdentityProjectionService(InMemoryIdentityProjectionRepository())
    with pytest.raises(ScopeViolation):
        service.apply(scope, replace(_projection(), site_id="other.local"))


def test_projection_wire_is_exact_frappe_contract() -> None:
    from services.email_gateway.models import IdentityProjection, ValidationError

    examples = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts/email_gateway/examples/provider-neutral-v1.json"
        ).read_text()
    )
    wire = examples["cases"]["frappe-identity-projection-v1.0.schema.json"]["valid"]["customer"]
    projection = IdentityProjection.from_wire(wire, payload_digest="sha256:" + "f" * 64)
    assert projection.to_wire() == wire
    with pytest.raises(ValidationError, match="unknown"):
        IdentityProjection.from_wire(
            {**wire, "target_ref": "customer@example.invalid"},
            payload_digest="sha256:" + "f" * 64,
        )
