from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


class _RestartRepository:
    def __init__(self) -> None:
        self.receipts: dict[tuple[str, str, str, str], tuple[str, dict[str, object]]] = {}
        self.drafts: dict[tuple[str, str, str], dict[str, object]] = {}
        self.finals: dict[tuple[str, str, str], dict[str, object]] = {}

    def replay(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, object] | None:
        saved = self.receipts.get((scope.site_id, purpose, operation, idempotency_key))
        if saved is None:
            return None
        if saved[0] != request_digest:
            raise ValueError("draft material replay drift")
        return dict(saved[1])

    def commit_save(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        key = (scope.site_id, purpose, "save", idempotency_key)
        existing = self.receipts.get(key)
        if existing is not None:
            if existing[0] != request_digest:
                raise ValueError("draft material replay drift")
            return dict(existing[1])
        self.receipts[key] = (request_digest, dict(receipt))
        self.drafts[(scope.site_id, purpose, str(binding["evidence_ref"]))] = dict(binding)
        return dict(receipt)

    def commit_finalize(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        idempotency_key: str,
        request_digest: str,
        receipt: dict[str, object],
        binding: dict[str, object],
    ) -> dict[str, object]:
        key = (scope.site_id, purpose, "finalize", idempotency_key)
        existing = self.receipts.get(key)
        if existing is not None:
            if existing[0] != request_digest:
                raise ValueError("draft material replay drift")
            return dict(existing[1])
        self.receipts[key] = (request_digest, dict(receipt))
        self.finals[(scope.site_id, purpose, str(binding["evidence_ref"]))] = dict(binding)
        return dict(receipt)

    def resolve_draft(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None:
        value = self.drafts.get((scope.site_id, purpose, evidence_ref))
        return None if value is None else dict(value)

    def resolve_final(
        self,
        scope: TenantScope,
        *,
        purpose: str,
        evidence_ref: str,
    ) -> dict[str, object] | None:
        value = self.finals.get((scope.site_id, purpose, evidence_ref))
        return None if value is None else dict(value)


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


def _service(
    root: Path,
    *,
    repository: Any | None = None,
    resolve_calls: list[str] | None = None,
) -> EmailDraftMaterialService:
    def resolve(_scope, authorization, roles):
        if resolve_calls is not None:
            resolve_calls.append(authorization.receipt_ref)
        if authorization.participant_binding_digest != "sha256:" + "c" * 64:
            raise PermissionError("participant authority binding mismatch")
        return {
            "from": "sales@example.invalid",
            "to": ["customer@example.invalid"],
            "roles": roles,
            "participant_projection": [
                {
                    "address_role": "sender",
                    "opaque_address_ref": "extid:v1:email:" + "a" * 43,
                },
                {
                    "address_role": "to",
                    "opaque_address_ref": "extid:v1:email:" + "b" * 43,
                },
            ],
        }

    return EmailDraftMaterialService(
        store=ContentAddressedEvidenceStore(root),
        repository=repository or _RestartRepository(),
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
    assert str(result["evidence_ref"]).startswith("EVR-")
    assert "obs:v1:" not in repr(result)
    assert result["digest"] == digest
    assert result["revision"] == 1
    assert "Thank you" not in repr((authorization, result, service))


def test_save_exact_replay_survives_service_restart_and_payload_drift_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _RestartRepository()
    content = "Durable draft receipt"
    digest = EmailDraftMaterialService.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    first = _service(tmp_path / "cas", repository=repository).save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-restart-01",
    )

    restarted = _service(tmp_path / "cas", repository=repository)
    replay = restarted.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-restart-01",
    )

    assert replay == first
    drifted = "Different durable draft"
    drifted_digest = restarted.digest_text(drifted)
    with pytest.raises(ValueError, match="replay drift"):
        restarted.save(
            SCOPE,
            authorization=DraftAuthorizationReceipt.from_wire(
                _receipt(request_digest=drifted_digest)
            ),
            content=drifted,
            content_digest=drifted_digest,
            idempotency_key="draft-save-restart-01",
        )


def test_save_replay_rejects_durable_binding_or_cas_digest_drift(tmp_path: Path) -> None:
    repository = _RestartRepository()
    root = tmp_path / "cas"
    service = _service(root, repository=repository)
    content = "Durable save integrity"
    digest = service.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    saved = service.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-integrity-01",
    )
    assert (SITE, "email_draft_material", str(saved["evidence_ref"])) in repository.drafts
    path = next(root.rglob(digest.removeprefix("sha256:")))
    path.chmod(0o600)
    path.write_bytes(b"corrupt draft")

    with pytest.raises(ValueError, match="integrity"):
        _service(root, repository=repository).save(
            SCOPE,
            authorization=authorization,
            content=content,
            content_digest=digest,
            idempotency_key="draft-save-integrity-01",
        )


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

    assert set(result) == {"evidence_ref", "digest", "role_binding", "participants"}
    assert str(result["evidence_ref"]).startswith("EVR-")
    assert len(str(result["evidence_ref"])) == 30
    assert str(result["role_binding"]).startswith("sha256:")
    assert result["participants"] == [
        {
            "address_role": "sender",
            "opaque_address_ref": "extid:v1:email:" + "a" * 43,
        },
        {
            "address_role": "to",
            "opaque_address_ref": "extid:v1:email:" + "b" * 43,
        },
    ]
    assert "@" not in repr(result)


def test_finalize_rejects_provisional_ref_against_canonical_saved_binding(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path / "cas")
    content = "Canonical draft reference"
    digest = service.digest_text(content)
    canonical = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    saved = service.save(
        SCOPE,
        authorization=canonical,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-canonical-ref-01",
    )
    provisional = DraftAuthorizationReceipt.from_wire(
        _receipt(request_digest=digest, draft_ref="DRF-ui-provisional-01")
    )

    with pytest.raises(ValueError, match="draft evidence binding drift"):
        service.finalize(
            SCOPE,
            authorization=provisional,
            draft_evidence_ref=str(saved["evidence_ref"]),
            draft_digest=digest,
            draft_revision=1,
            participant_roles=PARTICIPANT_ROLES,
            idempotency_key="draft-finalize-provisional-ref-01",
        )


def test_finalize_exact_replay_rechecks_current_authority_and_final_cas_after_restart(
    tmp_path: Path,
) -> None:
    repository = _RestartRepository()
    resolve_calls: list[str] = []
    root = tmp_path / "cas"
    content = "Restart-safe final MIME"
    digest = EmailDraftMaterialService.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    service = _service(root, repository=repository, resolve_calls=resolve_calls)
    saved = service.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-final-restart-01",
    )
    first = service.finalize(
        SCOPE,
        authorization=authorization,
        draft_evidence_ref=str(saved["evidence_ref"]),
        draft_digest=digest,
        draft_revision=1,
        participant_roles=PARTICIPANT_ROLES,
        idempotency_key="draft-finalize-restart-01",
    )

    restarted = _service(root, repository=repository, resolve_calls=resolve_calls)
    replay = restarted.finalize(
        SCOPE,
        authorization=authorization,
        draft_evidence_ref=str(saved["evidence_ref"]),
        draft_digest=digest,
        draft_revision=1,
        participant_roles=PARTICIPANT_ROLES,
        idempotency_key="draft-finalize-restart-01",
    )

    assert replay == first
    assert resolve_calls == [authorization.receipt_ref, authorization.receipt_ref]
    binding = repository.finals[(SITE, "email_draft_material", str(first["evidence_ref"]))]
    assert binding == {
        **binding,
        "inbox_item_ref": authorization.inbox_item_ref,
        "draft_ref": authorization.draft_ref,
        "draft_revision": 1,
        "authorization_receipt_ref": authorization.receipt_ref,
        "gateway_receipt_ref": authorization.gateway_receipt_ref,
        "publication_ref": authorization.publication_ref,
        "message_ref": authorization.message_ref,
        "mailbox_ref": authorization.mailbox_ref,
        "mailbox_config_revision": authorization.mailbox_config_revision,
        "observer_delivery_ref": authorization.observer_delivery_ref,
        "payload_digest": authorization.payload_digest,
        "participant_binding_digest": authorization.participant_binding_digest,
        "evidence_binding_digest": authorization.evidence_binding_digest,
        "participant_roles_digest": authorization.participant_roles_digest,
        "source_draft_evidence_ref": saved["evidence_ref"],
        "source_draft_digest": digest,
        "media_type": "message/rfc822",
    }
    assert "@" not in repr(binding)

    final_path = next(root.rglob(str(binding["digest"]).removeprefix("sha256:")))
    final_path.chmod(0o600)
    final_path.write_bytes(b"corrupt final MIME")
    with pytest.raises(ValueError, match="integrity"):
        restarted.finalize(
            SCOPE,
            authorization=authorization,
            draft_evidence_ref=str(saved["evidence_ref"]),
            draft_digest=digest,
            draft_revision=1,
            participant_roles=PARTICIPANT_ROLES,
            idempotency_key="draft-finalize-restart-01",
        )


def test_finalize_database_failure_leaves_only_unbound_cas_orphan(tmp_path: Path) -> None:
    class FailingRepository(_RestartRepository):
        def commit_finalize(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            raise RuntimeError("database unavailable")

    repository = FailingRepository()
    root = tmp_path / "cas"
    service = _service(root, repository=repository)
    content = "CAS first, binding second"
    digest = service.digest_text(content)
    authorization = DraftAuthorizationReceipt.from_wire(_receipt(request_digest=digest))
    saved = service.save(
        SCOPE,
        authorization=authorization,
        content=content,
        content_digest=digest,
        idempotency_key="draft-save-orphan-01",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        service.finalize(
            SCOPE,
            authorization=authorization,
            draft_evidence_ref=str(saved["evidence_ref"]),
            draft_digest=digest,
            draft_revision=1,
            participant_roles=PARTICIPANT_ROLES,
            idempotency_key="draft-finalize-orphan-01",
        )

    assert repository.finals == {}
    assert not any(key[2] == "finalize" for key in repository.receipts)
    assert len([path for path in root.rglob("*") if path.is_file()]) == 2


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
