from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.observer.observer.email_draft_material import (
    DraftAuthorizationReceipt,
    EmailDraftMaterialService,
)
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import TenantScope

SITE = "alpha.example"
SCOPE = TenantScope(SITE, "observation_processing")
NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _receipt(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "receipt_ref": "DAR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "site_id": SITE,
        "purpose": "email_draft_material",
        "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_ref": "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_revision": 1,
        "actor_ref": "sales-01",
        "team_ref": "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "request_digest": DIGEST,
        "issued_at": "2026-08-13T10:00:00Z",
        "expires_at": "2026-08-13T10:05:00Z",
    }
    value.update(changes)
    return value


def _service(root: Path) -> EmailDraftMaterialService:
    return EmailDraftMaterialService(
        store=ContentAddressedEvidenceStore(root),
        participant_resolver=lambda _scope, _inbox, roles: {
            "from": "sales@example.invalid",
            "to": ["customer@example.invalid"],
            "roles": roles,
        },
        clock=lambda: NOW,
    )


def test_save_accepts_only_fresh_closed_gateway_receipt_and_returns_opaque_projection(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "cas")
    content = "Thank you for your enquiry."
    digest = service.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))

    result = service.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-01",
    )

    assert set(result) == {"evidence_ref", "digest", "revision"}
    assert result["digest"] == digest
    assert result["revision"] == 1
    assert "Thank you" not in repr((authorization, result, service))


@pytest.mark.parametrize(
    "change",
    [
        {"site_id": "other.example"},
        {"purpose": "communication_projection"},
        {"draft_revision": 0},
        {"expires_at": "2026-08-13T09:59:59Z"},
        {"unexpected": "field"},
    ],
)
def test_save_rejects_scope_freshness_revision_and_unknown_field_drift(
    tmp_path: Path, change: dict[str, object]
) -> None:
    service = _service(tmp_path / "cas")
    content = "Bounded draft"
    digest = service.digest_text(content)

    with pytest.raises((ValueError, PermissionError)):
        authorization = DraftAuthorizationReceipt.from_wire(
            _receipt(request_digest=digest, **change)
        )
        service.save(
            SCOPE,
            authorization=authorization,
            content=content,
            content_digest=digest,
            idempotency_key="draft-save-01",
        )


def test_finalize_resolves_addresses_from_opaque_roles_and_returns_only_cas_binding(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "cas")
    content = "Provider-neutral reply"
    digest = service.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    saved = service.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-01",
    )

    result = service.finalize(
        SCOPE,
        authorization=authorization,
        draft_evidence_ref=str(saved["evidence_ref"]),
        draft_digest=digest,
        draft_revision=1,
        participant_roles={"sender": "mailbox_owner", "recipients": ["original_sender"]},
        idempotency_key="draft-finalize-01",
    )

    assert set(result) == {"evidence_ref", "digest", "role_binding"}
    assert str(result["evidence_ref"]).startswith("obs:v1:")
    assert str(result["role_binding"]).startswith("sha256:")
    assert "@" not in repr(result)


def test_finalize_rejects_raw_browser_addresses_and_stale_authorization(tmp_path: Path) -> None:
    service = _service(tmp_path / "cas")
    content = "Provider-neutral reply"
    digest = service.digest_text(content)
    expired = DraftAuthorizationReceipt.from_wire(
        _receipt(
            request_digest=digest,
            issued_at=(NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            expires_at=(NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
    )

    with pytest.raises(PermissionError):
        service.finalize(
            SCOPE,
            authorization=expired,
            draft_evidence_ref="obs:v1:" + "0" * 32 + ":sha256:" + "0" * 64,
            draft_digest=digest,
            draft_revision=1,
            participant_roles={"from": "sales@example.invalid"},
            idempotency_key="draft-finalize-01",
        )
