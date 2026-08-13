from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.observer.observer.email_draft_material import (
    DraftAuthorizationReceipt,
    EmailDraftMaterialService,
)
from services.observer.observer.email_participant_authority import canonical_binding_digest
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import TenantScope

SITE = "alpha.example"
SCOPE = TenantScope(SITE, "observation_processing")
NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
PARTICIPANT_ROLES = {"sender": "mailbox_owner", "recipients": ["original_sender"]}


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
        "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "message_ref": "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 1,
        "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": "sha256:" + "b" * 64,
        "participant_binding_digest": "sha256:" + "c" * 64,
        "evidence_binding_digest": "sha256:" + "d" * 64,
        "participant_roles_digest": canonical_binding_digest(PARTICIPANT_ROLES),
        "issued_at": "2026-08-13T10:00:00Z",
        "expires_at": "2026-08-13T10:05:00Z",
    }
    value.update(changes)
    return value


def _service(root: Path) -> EmailDraftMaterialService:
    def resolve(_scope, authorization, roles):
        if authorization.participant_binding_digest != "sha256:" + "c" * 64:
            raise PermissionError("participant authority binding mismatch")
        return {
            "from": "sales@example.invalid",
            "to": ["customer@example.invalid"],
            "roles": roles,
        }

    return EmailDraftMaterialService(
        store=ContentAddressedEvidenceStore(root),
        participant_resolver=resolve,
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
        participant_roles=PARTICIPANT_ROLES,
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


def test_finalize_rejects_participant_role_digest_and_gateway_binding_drift(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "cas")
    content = "Provider-neutral reply"
    digest = service.digest_text(content)
    wrong_roles = DraftAuthorizationReceipt.from_wire(
        _receipt(
            request_digest=digest,
            participant_roles_digest="sha256:" + "9" * 64,
        )
    )
    wrong_binding = DraftAuthorizationReceipt.from_wire(
        _receipt(
            request_digest=digest,
            participant_binding_digest="sha256:" + "8" * 64,
        )
    )
    saved = service.save(
        SCOPE,
        authorization=wrong_roles,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-binding-01",
    )

    with pytest.raises(PermissionError, match="participant role binding"):
        service.finalize(
            SCOPE,
            authorization=wrong_roles,
            draft_evidence_ref=str(saved["evidence_ref"]),
            draft_digest=digest,
            draft_revision=1,
            participant_roles=PARTICIPANT_ROLES,
            idempotency_key="draft-finalize-binding-01",
        )
    with pytest.raises(PermissionError, match="participant authority"):
        service.finalize(
            SCOPE,
            authorization=wrong_binding,
            draft_evidence_ref=str(saved["evidence_ref"]),
            draft_digest=digest,
            draft_revision=1,
            participant_roles=PARTICIPANT_ROLES,
            idempotency_key="draft-finalize-binding-02",
        )
