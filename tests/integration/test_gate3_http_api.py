from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from services.context.context_service.api import create_context_app
from services.context.context_service.models import canonical_payload_digest
from services.context.context_service.repositories import InMemoryContextRepository
from services.observer.observer.api import create_observer_app
from services.observer.observer.application import ManualImportPipeline, canonical_import_body
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import (
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    Participant,
    TenantScope,
)
from services.observer.observer.processing import (
    DeterministicProcessor,
    DisabledReviewCaseBridge,
)
from services.observer.observer.security import (
    HMACServiceIdentity,
    LocalRequestAuthenticator,
    NonceStore,
)
from services.observer.observer.storage import ObservationMetadata

NOW = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)
SITE = "gbos.localhost"
PURPOSE = "observation_processing"
LOCAL_SECRET = b"synthetic-local-api-secret"


class RecordingObserverPersistence:
    def __init__(self, status: str = "stored") -> None:
        self.calls: list[dict[str, object]] = []
        self.status = status

    def persist(
        self,
        scope: TenantScope,
        *,
        idempotency_key: str,
        payload_digest: str,
        result: ImportResult,
        provider_event_id: str | None,
        checkpoint_id: str,
        replay_window_seconds: int,
    ) -> ObservationMetadata:
        observation = result.observation
        self.calls.append(
            {
                "scope": scope,
                "idempotency_key": idempotency_key,
                "payload_digest": payload_digest,
                "provider_event_id": provider_event_id,
                "checkpoint_id": checkpoint_id,
                "replay_window_seconds": replay_window_seconds,
            }
        )
        return ObservationMetadata(
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            job_id="job-http-001",
            status=self.status,
            event_id=observation.event_id if self.status == "stored" else None,
            connector=observation.connector,
            provider_event_id=provider_event_id,
            raw_sha256=observation.raw_sha256,
            occurred_at=observation.occurred_at,
            ingested_at=observation.ingested_at,
            evidence_ids=observation.evidence_refs if self.status == "stored" else (),
            checkpoint_id=checkpoint_id,
            checkpoint_disposition=("advance" if self.status == "stored" else self.status),
        )


class RecordingContextPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(
        self,
        result: ImportResult,
        *,
        correlation_id: str,
        recorded_at: datetime | None = None,
    ) -> tuple[object, ...]:
        self.calls.append(
            {
                "result": result,
                "correlation_id": correlation_id,
                "recorded_at": recorded_at,
            }
        )
        return tuple(
            object()
            for _item in (
                *result.evidence,
                *result.fact_proposals,
                *result.entity_resolution_proposals,
            )
        )


def _observer_client(
    tmp_path: Path,
    *,
    persistence_status: str = "stored",
) -> tuple[
    TestClient,
    HMACServiceIdentity,
    RecordingObserverPersistence,
    RecordingContextPublisher,
]:
    pipeline = ManualImportPipeline(
        store=ContentAddressedEvidenceStore(tmp_path / "objects"),
        authenticator=LocalRequestAuthenticator(
            identity="observer-fixture",
            secret=LOCAL_SECRET,
            nonce_store=NonceStore(),
            clock=lambda: NOW,
        ),
        processor=DeterministicProcessor(),
        review_bridge=DisabledReviewCaseBridge(),
        clock=lambda: NOW,
    )
    persistence = RecordingObserverPersistence(persistence_status)
    publisher = RecordingContextPublisher()
    return (
        TestClient(
            create_observer_app(
                pipeline=pipeline,
                persistence=persistence,
                context_publisher=publisher,
            )
        ),
        HMACServiceIdentity("observer-fixture", LOCAL_SECRET),
        persistence,
        publisher,
    )


def _observer_request() -> tuple[
    TenantScope, ManualImportManifest, tuple[ManualImportMember, ...], dict[str, object]
]:
    scope = TenantScope(SITE, PURPOSE)
    manifest = ManualImportManifest(
        connector="manual_import",
        fixture_id="fixture-http-001",
        occurred_at=NOW,
        consent_basis="consent",
        data_classification="Restricted",
        retention_class="R1-operational",
        participants=(Participant("external", "party:synthetic-http"),),
        correlation_id="corr-http-001",
        provider_event_id="provider-http-001",
    )
    members = (
        ManualImportMember(
            "message.txt",
            "text/plain",
            "客户要求下周安排样品。".encode(),
        ),
    )
    package = members[0]
    payload: dict[str, object] = {
        "manifest": {
            "schema_version": "1.0",
            "manifest_id": manifest.fixture_id,
            "synthetic": True,
            "site_id": SITE,
            "processing_purpose": PURPOSE,
            "occurred_at": manifest.occurred_at.isoformat(),
            "original_language": "zh-CN",
            "consent_basis": manifest.consent_basis,
            "data_classification": manifest.data_classification,
            "retention_class": manifest.retention_class,
            "participants": [
                {
                    "role": participant.role,
                    "identity_ref": participant.identity_ref,
                }
                for participant in manifest.participants
            ],
            "source": {
                "connector": manifest.connector,
                "package_type": "message_fixture",
                "provider_event_id": "provider-http-001",
            },
            "package": {
                "filename": package.name,
                "media_type": package.media_type,
                "size_bytes": len(package.content),
                "sha256": hashlib.sha256(package.content).hexdigest(),
            },
            "budgets": {
                "body_bytes": 1048576,
                "attachment_count": 0,
                "attachment_bytes": 0,
                "decompressed_bytes": 1048576,
            },
            "idempotency_key": "http-import-001",
            "correlation_id": manifest.correlation_id,
            "submitted_at": NOW.isoformat(),
        },
        "members": [
            {
                "name": member.name,
                "media_type": member.media_type,
                "content_utf8": member.content.decode(),
            }
            for member in members
        ],
    }
    return scope, manifest, members, payload


def test_observer_http_manual_import_uses_signed_local_identity(tmp_path: Path) -> None:
    client, identity, persistence, publisher = _observer_client(tmp_path)
    scope, manifest, members, payload = _observer_request()
    canonical = canonical_import_body(manifest, members)
    signed = identity.sign(
        method="POST",
        path="/internal/v1/manual-imports",
        timestamp=NOW,
        nonce="http-nonce-001",
        scope=scope,
        body=canonical,
    )
    response = client.post(
        "/internal/v1/manual-imports",
        headers={
            "X-GBOS-Identity": signed.identity,
            "X-GBOS-Timestamp": signed.timestamp.isoformat(),
            "X-GBOS-Nonce": signed.nonce,
            "X-Site-ID": SITE,
            "X-Processing-Purpose": PURPOSE,
            "X-GBOS-Body-SHA256": signed.body_sha256,
            "X-GBOS-Signature": signed.signature,
            "Idempotency-Key": "http-import-001",
            "X-Request-ID": "REQ-http-001",
        },
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["ingestion"]["site_id"] == SITE
    assert body["data"]["ingestion"]["status"] == "stored"
    assert body["data"]["proposal_counts"]["fact"] == 1
    assert body["data"]["proposal_counts"]["entity_resolution"] == 1
    assert body["data"]["context_publication"] == {
        "status": "published",
        "record_count": 3,
    }
    assert persistence.calls == [
        {
            "scope": scope,
            "idempotency_key": "http-import-001",
            "payload_digest": body["data"]["ingestion"]["raw_sha256"],
            "provider_event_id": "provider-http-001",
            "checkpoint_id": "manual-import-main",
            "replay_window_seconds": 86400,
        }
    ]
    assert len(publisher.calls) == 1
    assert publisher.calls[0]["correlation_id"] == manifest.correlation_id
    assert publisher.calls[0]["recorded_at"] == datetime.fromisoformat(
        body["data"]["ingestion"]["ingested_at"]
    )
    assert body["meta"]["request_id"] == "REQ-http-001"
    assert body["meta"]["schema_version"] == "1.0"


def test_observer_http_rejects_unsigned_before_side_effects(tmp_path: Path) -> None:
    client, _identity, persistence, publisher = _observer_client(tmp_path)
    _scope, _manifest, _members, payload = _observer_request()

    response = client.post(
        "/internal/v1/manual-imports",
        headers={
            "X-Site-ID": SITE,
            "X-Processing-Purpose": PURPOSE,
            "Idempotency-Key": "http-import-unsigned",
            "X-Request-ID": "REQ-http-unsigned",
        },
        json=payload,
    )

    assert response.status_code == 401
    assert persistence.calls == []
    assert publisher.calls == []
    assert not list((tmp_path / "objects").rglob("*"))


def test_observer_http_rejects_non_synthetic_manifest_before_storage(
    tmp_path: Path,
) -> None:
    client, identity, persistence, publisher = _observer_client(tmp_path)
    scope, manifest, members, payload = _observer_request()
    payload["manifest"]["synthetic"] = False  # type: ignore[index]
    canonical = canonical_import_body(manifest, members)
    signed = identity.sign(
        method="POST",
        path="/internal/v1/manual-imports",
        timestamp=NOW,
        nonce="http-nonce-real-data",
        scope=scope,
        body=canonical,
    )

    response = client.post(
        "/internal/v1/manual-imports",
        headers={
            "X-GBOS-Identity": signed.identity,
            "X-GBOS-Timestamp": signed.timestamp.isoformat(),
            "X-GBOS-Nonce": signed.nonce,
            "X-Site-ID": SITE,
            "X-Processing-Purpose": PURPOSE,
            "X-GBOS-Body-SHA256": signed.body_sha256,
            "X-GBOS-Signature": signed.signature,
            "Idempotency-Key": "http-import-001",
            "X-Request-ID": "REQ-http-real-data",
        },
        json=payload,
    )

    assert response.status_code == 422
    assert persistence.calls == []
    assert publisher.calls == []
    assert not list((tmp_path / "objects").rglob("*"))


def test_observer_http_signature_covers_provider_event_id_before_storage(
    tmp_path: Path,
) -> None:
    client, identity, persistence, publisher = _observer_client(tmp_path)
    scope, manifest, members, payload = _observer_request()
    canonical = canonical_import_body(manifest, members)
    signed = identity.sign(
        method="POST",
        path="/internal/v1/manual-imports",
        timestamp=NOW,
        nonce="http-nonce-provider-tamper",
        scope=scope,
        body=canonical,
    )
    payload["manifest"]["source"]["provider_event_id"] = "tampered-provider"  # type: ignore[index]

    response = client.post(
        "/internal/v1/manual-imports",
        headers={
            "X-GBOS-Identity": signed.identity,
            "X-GBOS-Timestamp": signed.timestamp.isoformat(),
            "X-GBOS-Nonce": signed.nonce,
            "X-Site-ID": SITE,
            "X-Processing-Purpose": PURPOSE,
            "X-GBOS-Body-SHA256": signed.body_sha256,
            "X-GBOS-Signature": signed.signature,
            "Idempotency-Key": "http-import-001",
            "X-Request-ID": "REQ-http-provider-tamper",
        },
        json=payload,
    )

    assert response.status_code == 401
    assert persistence.calls == []
    assert publisher.calls == []
    assert not list((tmp_path / "objects").rglob("*"))


def test_observer_http_does_not_publish_context_for_dead_letter(tmp_path: Path) -> None:
    client, identity, persistence, publisher = _observer_client(
        tmp_path,
        persistence_status="dead_letter",
    )
    scope, manifest, members, payload = _observer_request()
    signed = identity.sign(
        method="POST",
        path="/internal/v1/manual-imports",
        timestamp=NOW,
        nonce="http-nonce-dead-letter",
        scope=scope,
        body=canonical_import_body(manifest, members),
    )

    response = client.post(
        "/internal/v1/manual-imports",
        headers={
            "X-GBOS-Identity": signed.identity,
            "X-GBOS-Timestamp": signed.timestamp.isoformat(),
            "X-GBOS-Nonce": signed.nonce,
            "X-Site-ID": SITE,
            "X-Processing-Purpose": PURPOSE,
            "X-GBOS-Body-SHA256": signed.body_sha256,
            "X-GBOS-Signature": signed.signature,
            "Idempotency-Key": "http-import-001",
            "X-Request-ID": "REQ-http-dead-letter",
        },
        json=payload,
    )

    assert response.status_code == 200
    assert len(persistence.calls) == 1
    assert publisher.calls == []
    assert response.json()["data"]["proposal_counts"] == {
        "fact": 0,
        "entity_resolution": 0,
    }
    assert response.json()["data"]["context_publication"] == {
        "status": "skipped",
        "record_count": 0,
    }


def _context_headers(*, token: str = "synthetic-context-token") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Site-ID": SITE,
        "X-Request-ID": "REQ-context-001",
        "X-Processing-Purpose": PURPOSE,
        "Idempotency-Key": "context-idem-001",
    }


def test_context_http_enforces_scope_and_returns_metadata_only() -> None:
    repository = InMemoryContextRepository()
    client = TestClient(
        create_context_app(
            repository=repository,
            local_token="synthetic-context-token",
        )
    )
    payload = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts"
            / "examples"
            / "gate3"
            / "fact-proposal-record.json"
        ).read_text(encoding="utf-8")
    )
    payload["site_id"] = SITE
    payload["fact_proposal_record_id"] = "fact-http-001"

    created = client.post(
        "/internal/v1/context/fact-proposals",
        headers=_context_headers(),
        json=payload,
    )
    assert created.status_code == 200
    assert created.json()["data"]["record_id"] == "fact-http-001"
    assert "payload" not in created.json()["data"]

    read = client.get(
        "/v1/context/fact-proposals/fact-http-001",
        headers=_context_headers(),
    )
    assert read.status_code == 200
    assert read.json()["data"]["payload_digest"] == canonical_payload_digest(payload)

    denied = client.get(
        "/v1/context/fact-proposals/fact-http-001",
        headers={**_context_headers(token="wrong"), "X-Site-ID": "other.localhost"},
    )
    assert denied.status_code == 401


def test_context_http_rejects_incomplete_wire_record() -> None:
    client = TestClient(
        create_context_app(
            repository=InMemoryContextRepository(),
            local_token="synthetic-context-token",
        )
    )
    response = client.post(
        "/internal/v1/context/fact-proposals",
        headers=_context_headers(),
        json={
            "site_id": SITE,
            "processing_purpose": PURPOSE,
            "fact_proposal_record_id": "incomplete-fact",
            "fact": {"status": "proposed"},
        },
    )

    assert response.status_code == 422


def test_gate3_apps_expose_no_gate4_or_later_routes(tmp_path: Path) -> None:
    observer, _identity, _persistence, _publisher = _observer_client(tmp_path)
    context = TestClient(
        create_context_app(
            repository=InMemoryContextRepository(),
            local_token="synthetic-context-token",
        )
    )

    route_text = " ".join(
        route.path for app in (observer.app, context.app) for route in app.routes
    ).lower()
    for forbidden in (
        "/decisions",
        "/actions",
        "/review-cases",
        "/draft-mutations",
        "/approved-commands",
        "/kingdee",
        "/metrics",
    ):
        assert forbidden not in route_text
