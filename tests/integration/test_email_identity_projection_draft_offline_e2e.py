from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import httpx
import pytest

from services.agent_runtime.frappe_client import HttpFrappeDraftClient
from services.agent_runtime.invocations import ModelInvocationRecord
from services.agent_runtime.materialization import FrappeDraftReceipt
from services.agent_runtime.models import canonical_payload_digest
from services.context.context_service.communication_intelligence import (
    CommunicationDraftClaim,
    CommunicationDraftRunResult,
)
from services.local_pilot_runtime.communication_draft_worker import CommunicationDraftWorker
from services.local_pilot_runtime.identity_resolution_worker import (
    DEFAULT_FRAPPE_BASE_URL,
    FrappeIdentityResolverClient,
    IdentityResolutionRunStatus,
    IdentityResolutionWorker,
)
from services.local_pilot_runtime.model_projection_worker import (
    ModelProjectionWorker,
    ProjectionOutboxClaim,
    ProjectionRunStatus,
    TrustedPhraseResolution,
    TrustedProjectionTokenizer,
)
from services.local_pilot_runtime.observer_worker import (
    ConnectorPipeline,
    ObserverConnectorWorker,
)
from services.model_gateway.deepseek import (
    DEEPSEEK_MODEL,
    DeepSeekAdapter,
    InMemoryUsageLedger,
)
from services.model_gateway.observation_provider import DeepSeekObservationProvider
from services.model_gateway.runtime import (
    ConservativeTokenCounter,
    DeepSeekV4FlashPriceCalculator,
)
from services.model_gateway.tokenization import InMemoryMappingVault, StableTokenizer
from services.observer.observer.connectors.email_delivery import EmailRawDeliveryDecoder
from services.observer.observer.connectors.email_imap import EmailImapConfig, EmailImapConnector
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.identity_resolution import (
    InMemoryIdentityResolutionRepository,
)
from services.observer.observer.identity_resolution_work import (
    InMemoryIdentityResolutionWorkRepository,
)
from services.observer.observer.identity_tokens import HmacSha256IdentityTokenResolver
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_sink import PostgresNormalizedObservationSink
from services.observer.observer.local_pilot_storage import (
    InboundDeliveryMetadata,
    LocalPilotStorage,
    PersistedNormalizedBatch,
    PersistedNormalizedObservation,
    ProcessingJobMetadata,
)
from services.observer.observer.model_projection import (
    ContentAddressedEvidenceTextLoader,
    ContextIntelligencePublication,
    EvidenceLineage,
    ObservationProjectionPublisher,
    ObservationProjectionSource,
)
from services.observer.observer.models import (
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
    stable_ulid,
)
from services.observer.observer.normalizers import EmailObservationNormalizer
from services.observer.observer.read_service import CommunicationDetail

NOW = datetime(2026, 8, 11, 2, 0, tzinfo=UTC)
SCOPE = TenantScope("gbos.localhost", "observation_processing")
KEY = ConnectorKey("email", "email-primary")
TEAM = "TEM-OFFLINE-SALES"
SENDER_EMAIL = "ada.private@example.invalid"
RECIPIENT_EMAIL = "owner.private@example.invalid"
PERSON_NAME = "Ada Private"
ORGANIZATION = "Northwind Sensitive GmbH"
PHONE = "+86 138 0013 8000"
MODEL_API_KEY = "offline-model-api-key"
FRAPPE_API_SECRET = "offline-frappe-api-secret"


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _InlineHeartbeat:
    def run(self, execute: Any, heartbeat: Any) -> Any:
        heartbeat()
        return execute()


def _raw_email(*, uid: int, body_suffix: str) -> bytes:
    body = (
        f"{PERSON_NAME} from {ORGANIZATION} asks for a private follow-up {body_suffix}. "
        f"Call {PHONE} or reply to {SENDER_EMAIL}."
    )
    return (
        f"From: {PERSON_NAME} <{SENDER_EMAIL}>\r\n"
        f"To: Account Owner <{RECIPIENT_EMAIL}>\r\n"
        f"Message-ID: <offline-{uid}@example.invalid>\r\n"
        "Date: Tue, 01 Jan 2000 00:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}"
    ).encode()


class _FakeImapSession:
    def __init__(self, server: _FakeImapServer) -> None:
        self._server = server

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self._server.commands.append(("LOGIN", "<redacted>", "<redacted>"))
        assert username and password
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self._server.commands.append(("SELECT", mailbox, readonly))
        return "OK", [str(len(self._server.messages)).encode()]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self._server.commands.append(("RESPONSE", code))
        return "UIDVALIDITY", [b"42"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self._server.commands.append(("UID", command, *args))
        if command == "SEARCH":
            lower = 1
            if "UID" in args:
                range_value = str(args[args.index("UID") + 1])
                lower = int(range_value.partition(":")[0])
            values = " ".join(str(uid) for uid in sorted(self._server.messages) if uid >= lower)
            return "OK", [values.encode()]
        if command == "FETCH":
            uid = int(str(args[0]))
            raw, received_at = self._server.messages[uid]
            internal_date = received_at.strftime("%d-%b-%Y %H:%M:%S %z")
            metadata = (
                f'1 (UID {uid} INTERNALDATE "{internal_date}" BODY[] {{{len(raw)}}}'
            ).encode()
            return "OK", [(metadata, raw), b")"]
        raise AssertionError(f"unexpected IMAP command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self._server.commands.append(("LOGOUT",))
        return "BYE", [b"closed"]


class _FakeImapServer:
    def __init__(self) -> None:
        self.messages: dict[int, tuple[bytes, datetime]] = {}
        self.commands: list[tuple[object, ...]] = []
        self.tls_calls: list[tuple[str, int]] = []

    def tls_factory(self, host: str, port: int) -> _FakeImapSession:
        self.tls_calls.append((host, port))
        return _FakeImapSession(self)


@dataclass
class _NormalizedRecord:
    item: ConnectorItem
    normalized: NormalizedObservationInput
    source: ObservationProjectionSource
    result: PersistedNormalizedObservation


class _MemoryProjectionOutbox:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def enqueue(self, observation_id: str, outbox_id: str, now: datetime) -> None:
        self.rows.setdefault(
            outbox_id,
            {
                "observation_id": observation_id,
                "status": "queued",
                "attempt": 0,
                "max_attempts": 3,
                "next_retry_at": now,
            },
        )

    def claim(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> ProjectionOutboxClaim | None:
        for outbox_id, row in self.rows.items():
            if row["status"] not in {"queued", "retry"} or row["next_retry_at"] > now:
                continue
            row["status"] = "leased"
            row["attempt"] += 1
            row["worker_id"] = worker_id
            row["fence"] = f"fence-{row['attempt']}"
            return ProjectionOutboxClaim(
                site_id=scope.site_id,
                outbox_id=outbox_id,
                observation_id=row["observation_id"],
                idempotency_key=f"context-normalized:{row['observation_id']}",
                status="leased",
                attempt=row["attempt"],
                max_attempts=row["max_attempts"],
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
                fence_token=row["fence"],
            )
        return None

    def heartbeat(self, scope: TenantScope, outbox_id: str, **kwargs: Any) -> None:
        del scope
        row = self.rows[outbox_id]
        assert row["status"] == "leased"
        assert row["worker_id"] == kwargs["worker_id"]
        assert row["fence"] == kwargs["fence_token"]

    def mark_published(self, scope: TenantScope, outbox_id: str, **kwargs: Any) -> None:
        self.heartbeat(scope, outbox_id, **kwargs)
        self.rows[outbox_id]["status"] = "published"

    def mark_failed(
        self,
        scope: TenantScope,
        outbox_id: str,
        *,
        retry_at: datetime,
        error_code: str,
        **kwargs: Any,
    ) -> Literal["retry", "dead_letter"]:
        self.heartbeat(scope, outbox_id, **kwargs)
        row = self.rows[outbox_id]
        row["status"] = "retry"
        row["next_retry_at"] = retry_at
        row["error_code"] = error_code
        return "retry"


class _MemoryObserverRepository:
    """In-memory implementation of the public durable and projection boundaries."""

    def __init__(
        self,
        *,
        work: InMemoryIdentityResolutionWorkRepository,
        outbox: _MemoryProjectionOutbox,
    ) -> None:
        self._work = work
        self._outbox = outbox
        self.deliveries: dict[str, InboundDeliveryMetadata] = {}
        self.jobs: dict[str, ProcessingJobMetadata] = {}
        self.normalized: dict[str, _NormalizedRecord] = {}
        self.projections: dict[str, CommunicationDetail] = {}
        self.accept_count = 0

    def accept_and_enqueue_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
        exact_body_sha256: str,
        object_ref: str,
        byte_size: int,
        media_type: str,
        received_at: datetime,
        correlation_id: str,
        job_id: str,
        idempotency_key: str,
        max_attempts: int,
    ) -> tuple[InboundDeliveryMetadata, ProcessingJobMetadata]:
        existing = self.deliveries.get(delivery_id)
        if existing is not None:
            assert existing.exact_body_sha256 == exact_body_sha256
            return existing, self.jobs[job_id]
        delivery = InboundDeliveryMetadata(
            site_id=scope.site_id,
            connector=key.connector,
            connector_instance_id=key.instance_id,
            delivery_id=delivery_id,
            exact_body_sha256=exact_body_sha256,
            object_ref=object_ref,
            byte_size=byte_size,
            media_type=media_type,
            received_at=received_at,
            processing_status="received",
            attempt_count=0,
            correlation_id=correlation_id,
            last_attempt_at=None,
            last_error_code=None,
            created_at=received_at,
            updated_at=received_at,
        )
        job = ProcessingJobMetadata(
            site_id=scope.site_id,
            job_id=job_id,
            connector=key.connector,
            connector_instance_id=key.instance_id,
            delivery_id=delivery_id,
            stage="normalize",
            status="queued",
            attempt_count=0,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            generation=0,
            lease_owner=None,
            lease_expires_at=None,
            lease_generation=0,
            next_retry_at=None,
            last_error_code=None,
            created_at=received_at,
            updated_at=received_at,
        )
        self.deliveries[delivery_id] = delivery
        self.jobs[job_id] = job
        self.accept_count += 1
        return delivery, job

    def claim_processing_job(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata | None:
        del scope
        for job_id, job in self.jobs.items():
            if job.status not in {"queued", "retry_wait"}:
                continue
            claimed = replace(
                job,
                status="processing",
                attempt_count=job.attempt_count + 1,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                lease_generation=job.lease_generation + 1,
                updated_at=now,
            )
            self.jobs[job_id] = claimed
            self.deliveries[job.delivery_id] = replace(
                self.deliveries[job.delivery_id],
                processing_status="processing",
                attempt_count=claimed.attempt_count,
                last_attempt_at=now,
                updated_at=now,
            )
            return claimed
        return None

    def get_inbound_delivery(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        delivery_id: str,
    ) -> InboundDeliveryMetadata:
        delivery = self.deliveries[delivery_id]
        assert delivery.site_id == scope.site_id
        assert (delivery.connector, delivery.connector_instance_id) == (
            key.connector,
            key.instance_id,
        )
        return delivery

    def heartbeat_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> ProcessingJobMetadata:
        del scope
        job = self.jobs[job_id]
        assert job.lease_owner == worker_id
        assert job.attempt_count == expected_attempt
        assert job.lease_generation == expected_lease_generation
        job = replace(job, lease_expires_at=now + timedelta(seconds=lease_seconds))
        self.jobs[job_id] = job
        return job

    def complete_processing_job(
        self,
        scope: TenantScope,
        *,
        job_id: str,
        worker_id: str,
        expected_attempt: int,
        expected_lease_generation: int,
        now: datetime,
        provider_event_ids: tuple[str, ...],
    ) -> ProcessingJobMetadata:
        del scope, provider_event_ids
        job = self.jobs[job_id]
        assert job.lease_owner == worker_id
        assert job.attempt_count == expected_attempt
        assert job.lease_generation == expected_lease_generation
        completed = replace(
            job,
            status="succeeded",
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        self.jobs[job_id] = completed
        self.deliveries[job.delivery_id] = replace(
            self.deliveries[job.delivery_id],
            processing_status="succeeded",
            updated_at=now,
        )
        return completed

    def retry_processing_job(self, scope: TenantScope, **kwargs: Any) -> ProcessingJobMetadata:
        del scope
        job = self.jobs[kwargs["job_id"]]
        retried = replace(
            job,
            status="retry_wait",
            lease_owner=None,
            lease_expires_at=None,
            next_retry_at=kwargs["next_retry_at"],
            last_error_code=kwargs["error_code"],
        )
        self.jobs[job.job_id] = retried
        return retried

    def quarantine_processing_job(self, scope: TenantScope, **kwargs: Any) -> ProcessingJobMetadata:
        del scope
        job = self.jobs[kwargs["job_id"]]
        quarantined = replace(
            job,
            status="quarantined",
            lease_owner=None,
            lease_expires_at=None,
            last_error_code=kwargs["reason_code"],
        )
        self.jobs[job.job_id] = quarantined
        return quarantined

    def persist_normalized_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch:
        results: list[PersistedNormalizedObservation] = []
        for item, value in zip(items, normalized, strict=True):
            existing = self.normalized.get(item.provider_event_id)
            if existing is not None:
                assert existing.item == item and existing.normalized == value
                results.append(replace(existing.result, replayed=True))
                continue
            assert key == KEY and job.status == "processing"
            assert all(artifact.reference is not None for artifact in value.evidence)
            event_id = stable_ulid(
                "offline-normalized-event", scope.site_id, item.provider_event_id
            )
            outbox_id = stable_ulid("offline-model-outbox", scope.site_id, event_id)
            evidence = tuple(
                EvidenceLineage(
                    evidence_ref=stable_ulid(
                        "offline-evidence", scope.site_id, event_id, str(index)
                    ),
                    content_object_ref=str(artifact.reference),
                    media_type=artifact.media_type,
                    raw_sha256=str(artifact.reference).rsplit(":", 1)[-1],
                )
                for index, artifact in enumerate(value.evidence)
            )
            source = ObservationProjectionSource(
                site_id=scope.site_id,
                observation_id=event_id,
                channel=value.channel,
                occurred_at=item.occurred_at,
                classification=value.data_classification,
                team_ref=TEAM,
                party_ref=None,
                participant_refs=tuple(
                    participant.identity_ref for participant in value.participants
                ),
                evidence=evidence,
            )
            result = PersistedNormalizedObservation(
                provider_event_id=item.provider_event_id,
                event_id=event_id,
                outbox_id=outbox_id,
                payload_sha256=hashlib.sha256(repr(value).encode()).hexdigest(),
                replayed=False,
            )
            self.normalized[item.provider_event_id] = _NormalizedRecord(
                item=item,
                normalized=value,
                source=source,
                result=result,
            )
            self._outbox.enqueue(event_id, outbox_id, job.created_at)
            for participant in value.participants:
                if participant.identity_ref.startswith("extid:v1:email:"):
                    self._work.enqueue(
                        scope,
                        identity_provider="email",
                        identity_ref=participant.identity_ref,
                        team_ref=TEAM,
                        now=job.created_at,
                    )
            results.append(result)
        return PersistedNormalizedBatch(observations=tuple(results))

    def load_projection_source(
        self, scope: TenantScope, observation_id: str
    ) -> ObservationProjectionSource:
        for record in self.normalized.values():
            if (
                record.source.site_id == scope.site_id
                and record.source.observation_id == observation_id
            ):
                return record.source
        raise LookupError("observation unavailable")

    def store_projection(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        projected_at: datetime,
    ) -> None:
        del projected_at
        observation_id = detail.summary.observation_id
        existing = self.projections.get(observation_id)
        if existing is not None and existing != detail:
            raise ValueError("projection conflict")
        assert scope.site_id == SCOPE.site_id
        self.projections[observation_id] = detail


class _GovernedIdentityTransport:
    def __init__(self) -> None:
        self.active = False
        self.targets: dict[str, tuple[str, str]] = {}
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        lookup = kwargs["payload"]["payload"]["lookups"][0]
        identity_ref = lookup["external_subject_ref"]
        assert lookup["expected_team_ref"] == TEAM
        if not self.active:
            return 404, {"message": {"error": {"code": "mapping_not_resolved"}}}
        target_type, target_ref = self.targets[identity_ref]
        return 200, {
            "message": {
                "resolutions": [
                    {
                        "schema_version": "1.0",
                        "site_id": SCOPE.site_id,
                        "identity_provider": "email",
                        "external_subject_ref": identity_ref,
                        "mapping_ref": f"EID-{stable_ulid('offline-mapping', identity_ref)}",
                        "mapping_revision": 1,
                        "team_ref": TEAM,
                        "target_type": target_type,
                        "target_ref": target_ref,
                        "status": "confirmed",
                        "resolved_at": (NOW + timedelta(minutes=4))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                ]
            }
        }


class _MemoryContextDraftRepository:
    def __init__(self) -> None:
        self.publications: dict[str, ContextIntelligencePublication] = {}
        self.states: dict[str, str] = {}
        self.receipts: dict[str, FrappeDraftReceipt] = {}

    def publish(
        self,
        scope: TenantScope,
        publication: ContextIntelligencePublication,
        *,
        idempotency_key: str,
    ) -> None:
        assert publication.site_id == scope.site_id
        assert publication.team_ref == TEAM
        existing = self.publications.get(idempotency_key)
        if existing is not None:
            assert existing == publication
            return
        self.publications[idempotency_key] = publication
        self.states[idempotency_key] = "queued"

    def claim_draft(
        self,
        site_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> CommunicationDraftClaim | None:
        for key, publication in self.publications.items():
            if self.states[key] != "queued":
                continue
            self.states[key] = "leased"
            draft_id = stable_ulid("offline-communication-draft", key)
            values = {
                "subject": f"Communication event {publication.observation_id}",
                "summary_zh": publication.summary_zh,
                "team": TEAM,
                "evidence_refs": [
                    {"evidence_ref": ref, "locator_ref": ref} for ref in publication.evidence_refs
                ],
                "model_name": publication.model["name"],
                "model_version": publication.model["version"],
                "is_official_metric": False,
                "origin": "AI",
                "origin_reference": publication.observation_id,
                "review_status": "AI Draft",
            }
            return CommunicationDraftClaim(
                site_id=site_id,
                draft_id=draft_id,
                intelligence_id=stable_ulid("offline-intelligence", key),
                observation_id=publication.observation_id,
                processing_purpose=SCOPE.processing_purpose,
                subject=str(values["subject"]),
                summary_zh=publication.summary_zh,
                team_ref=TEAM,
                evidence_refs=publication.evidence_refs,
                model_name=publication.model["name"],
                model_version=publication.model["version"],
                payload_digest=canonical_payload_digest(
                    {
                        "operation": "create",
                        "doctype": "GBOS Informal Observation",
                        "values": values,
                    }
                ),
                attempt=1,
                max_attempts=3,
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
            )
        return None

    def heartbeat_draft(self, site_id: str, draft_id: str, **kwargs: Any) -> None:
        del site_id, draft_id, kwargs
        assert "leased" in self.states.values()

    def acknowledge_draft(
        self,
        site_id: str,
        draft_id: str,
        *,
        receipt: FrappeDraftReceipt,
        **kwargs: Any,
    ) -> FrappeDraftReceipt:
        del site_id, kwargs
        key = next(key for key, state in self.states.items() if state == "leased")
        self.states[key] = "acknowledged"
        self.receipts[draft_id] = receipt
        return receipt

    def fail_draft(
        self, site_id: str, draft_id: str, **kwargs: Any
    ) -> Literal["retry", "dead_letter"]:
        del site_id, draft_id, kwargs
        key = next(key for key, state in self.states.items() if state == "leased")
        self.states[key] = "queued"
        return "retry"


class _FrappeDraftTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.documents: dict[str, dict[str, Any]] = {}

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        request = kwargs["payload"]["payload"]
        intent = request["intent"]
        assert intent["operation"] == "create"
        assert intent["doctype"] == "GBOS Informal Observation"
        assert intent["values"]["review_status"] == "AI Draft"
        assert intent["values"]["origin"] == "AI"
        assert "business_status" not in intent["values"]
        self.documents.setdefault(request["request_id"], intent)
        return 200, {
            "message": {
                "site_id": SCOPE.site_id,
                "doctype": intent["doctype"],
                "name": "INF-OFFLINE-0001",
                "revision": 1,
                "request_id": request["request_id"],
                "request_digest": request["request_digest"],
            }
        }


def _identity_worker(
    work: InMemoryIdentityResolutionWorkRepository,
    projections: InMemoryIdentityResolutionRepository,
    transport: _GovernedIdentityTransport,
    clock: _Clock,
    *,
    worker_id: str,
) -> IdentityResolutionWorker:
    client = FrappeIdentityResolverClient(
        base_url=DEFAULT_FRAPPE_BASE_URL,
        unix_socket=None,
        site_id=SCOPE.site_id,
        auth_ref="observer-identity-resolver-v1",
        api_key="offline-resolver-key",
        api_secret="offline-resolver-secret",
        timeout_seconds=2,
        lease_duration=timedelta(seconds=10),
        transport=transport,
    )
    return IdentityResolutionWorker(
        work_repository=work,
        projection_repository=projections,
        client=client,
        worker_id=worker_id,
        clock=clock,
        lease_duration=timedelta(seconds=10),
        unresolved_recheck=timedelta(minutes=5),
        successful_recheck=timedelta(hours=1),
        heartbeat_runner=_InlineHeartbeat(),
    )


def _communication_output(*, observation_id: str, evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "site_id": SCOPE.site_id,
        "observation_id": observation_id,
        "evidence_refs": evidence_refs,
        "summary_zh": "客户请求就样品交付进行内部跟进。",
        "original_language": "en",
        "confidence": 0.93,
        "review_status": "AI Draft",
        "fact_proposals": [
            {
                "subject_ref": "PTY-OFFLINE-001",
                "predicate": "sample_follow_up_requested",
                "value_display": "请求内部跟进",
                "type": "text",
                "unit": None,
                "confidence": 0.93,
                "evidence_refs": evidence_refs,
                "status": "proposed",
            }
        ],
        "association_suggestions": [
            {
                "type": "user",
                "target_ref": "USR-OFFLINE-001",
                "confidence": 0.9,
                "evidence_refs": evidence_refs,
            }
        ],
    }


def test_email_identity_projection_draft_offline_e2e(
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    server = _FakeImapServer()
    server.messages[7] = (_raw_email(uid=7, body_suffix="one"), NOW)
    connector = EmailImapConnector(
        connector_instance_id=KEY.instance_id,
        config=EmailImapConfig(
            host="imap.example.invalid",
            port=993,
            mailbox="pilot-primary",
            folder="INBOX",
            enabled_at=NOW - timedelta(minutes=1),
            poll_limit=10,
            max_message_bytes=1_000_000,
            max_attachment_bytes=100_000,
            max_attachments=5,
            rescan_max_window=timedelta(days=7),
            rescan_max_uids=100,
        ),
        tls_client_factory=server.tls_factory,
        clock=clock,
    )
    first_poll = connector.poll(
        None,
        username="offline-imap-user",
        password="offline-imap-secret",
    )
    assert first_poll.status == "ok" and len(first_poll.messages) == 1
    assert ("SELECT", "INBOX", True) in server.commands
    assert ("UID", "FETCH", "7", "(UID INTERNALDATE BODY.PEEK[])") in server.commands
    assert all(
        command[:2] not in {("UID", "STORE"), ("UID", "MOVE")} for command in server.commands
    )

    evidence_store = ContentAddressedEvidenceStore(tmp_path / "cas")
    work = InMemoryIdentityResolutionWorkRepository()
    identity_projections = InMemoryIdentityResolutionRepository()
    model_outbox = _MemoryProjectionOutbox()
    observer_repository = _MemoryObserverRepository(work=work, outbox=model_outbox)
    storage = cast(LocalPilotStorage, observer_repository)
    inbox = DurableDeliveryInbox(storage=storage, evidence_store=evidence_store)
    first_message = first_poll.messages[0]
    accepted = inbox.accept(
        SCOPE,
        KEY,
        first_message.raw_delivery,
        correlation_id="corr-offline-email-7",
    )
    assert evidence_store.read(SCOPE, accepted.delivery.object_ref) == server.messages[7][0]

    identity_tokenizer = HmacSha256IdentityTokenResolver(b"identity-hmac-offline-key" * 2)
    sink = PostgresNormalizedObservationSink(
        storage=storage,
        evidence_store=evidence_store,
    )
    observer = ObserverConnectorWorker(
        storage=storage,
        evidence_store=evidence_store,
        pipelines={
            "email": ConnectorPipeline(
                decoder=EmailRawDeliveryDecoder(),
                normalizer=EmailObservationNormalizer(
                    identity_resolver=identity_tokenizer,
                    site_id=SCOPE.site_id,
                    purpose=SCOPE.processing_purpose,
                ),
            )
        },
        sink=sink,
        worker_id="observer-offline-1",
        clock=clock,
    )
    observed = observer.run_once(SCOPE)
    assert observed is not None and observed.status == "succeeded"
    first_record = next(iter(observer_repository.normalized.values()))
    participant_refs = first_record.source.participant_refs
    assert len(participant_refs) == 2
    assert all(value.startswith("extid:v1:email:") for value in participant_refs)
    assert SENDER_EMAIL not in json.dumps(participant_refs)
    assert RECIPIENT_EMAIL not in json.dumps(participant_refs)
    assert first_record.source.team_ref == TEAM

    identity_transport = _GovernedIdentityTransport()
    identity_worker = _identity_worker(
        work,
        identity_projections,
        identity_transport,
        clock,
        worker_id="identity-offline-1",
    )
    unresolved = {identity_worker.run_once(SCOPE).status for _ in range(2)}
    assert unresolved == {IdentityResolutionRunStatus.UNRESOLVED}
    assert all(identity_projections.history(SCOPE, "email", ref) == () for ref in participant_refs)

    identity_transport.targets = {
        participant_refs[0]: ("Party", "PTY-OFFLINE-001"),
        participant_refs[1]: ("User", "USR-OFFLINE-001"),
    }
    identity_transport.active = True
    clock.advance(timedelta(minutes=5))
    confirmed = {identity_worker.run_once(SCOPE).status for _ in range(2)}
    assert confirmed == {IdentityResolutionRunStatus.CONFIRMED}
    latest = [identity_projections.latest(SCOPE, "email", ref) for ref in participant_refs]
    assert {(item.target_type, item.target_ref) for item in latest if item is not None} == {
        ("Party", "PTY-OFFLINE-001"),
        ("User", "USR-OFFLINE-001"),
    }

    model_payloads: list[dict[str, Any]] = []
    invocation_audits: list[ModelInvocationRecord] = []
    response_mode = {"model": DEEPSEEK_MODEL}

    def model_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model_payloads.append(payload)
        user = json.loads(payload["messages"][1]["content"])
        output = _communication_output(
            observation_id=user["subject_ref"],
            evidence_refs=list(user["evidence_refs"]),
        )
        return httpx.Response(
            200,
            json={
                "id": f"offline-response-{len(model_payloads)}",
                "model": response_mode["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "total_tokens": 200,
                },
            },
        )

    ledger = InMemoryUsageLedger(monthly_cost_usd=Decimal("0"))
    gateway = DeepSeekAdapter(
        api_key=MODEL_API_KEY,
        transport=httpx.MockTransport(model_handler),
        token_counter=ConservativeTokenCounter(),
        price_calculator=DeepSeekV4FlashPriceCalculator(),
        usage_ledger=ledger,
        network_enabled=True,
        retry_delay=lambda _: None,
        clock=clock,
        monotonic=lambda: 1.0,
        audit_recorder=invocation_audits.append,
    )
    context_repository = _MemoryContextDraftRepository()
    trusted_tokenizer = TrustedProjectionTokenizer(
        tokenizer=StableTokenizer(
            hmac_key=b"projection-tokenizer-offline-key" * 2,
            vault=InMemoryMappingVault(),
        ),
        phrase_resolver=lambda _scope, _observation_id, _raw: TrustedPhraseResolution(
            names=(PERSON_NAME,),
            organizations=(ORGANIZATION,),
            names_complete=True,
            organizations_complete=True,
            resolver_version="offline-trusted-phrases-v1",
        ),
        clock=clock,
    )
    publisher = ObservationProjectionPublisher(
        repository=observer_repository,
        raw_loader=ContentAddressedEvidenceTextLoader(evidence_store),
        tokenizer=trusted_tokenizer,
        provider=DeepSeekObservationProvider(gateway=gateway),
        context_publisher=context_repository,
        clock=clock,
        restricted_policy="local_tokenized",
    )
    model_worker = ModelProjectionWorker(
        outbox=model_outbox,
        publisher=publisher,
        worker_id="model-offline-1",
        clock=clock,
        lease_duration=timedelta(seconds=30),
        heartbeat_runner=_InlineHeartbeat(),
    )
    projected = model_worker.run_once(SCOPE)
    assert projected.status is ProjectionRunStatus.PUBLISHED
    assert len(observer_repository.projections) == 1
    assert len(context_repository.publications) == 1
    assert len(model_payloads) == 1
    assert len(ledger.records) == 1
    assert invocation_audits[0].observed_model == DEEPSEEK_MODEL
    assert invocation_audits[0].external_send_count == 0
    assert invocation_audits[0].tool_call_count == 0
    assert "tools" not in model_payloads[0]

    frappe_transport = _FrappeDraftTransport()

    def frappe_client_factory(purpose: str) -> HttpFrappeDraftClient:
        assert purpose == SCOPE.processing_purpose
        return HttpFrappeDraftClient(
            base_url="http://127.0.0.1:8000",
            api_key="offline-frappe-key",
            api_secret=FRAPPE_API_SECRET,
            auth_ref="communication-draft-offline-v1",
            site_id=SCOPE.site_id,
            processing_purpose=purpose,
            timeout_seconds=2,
            transport=frappe_transport,
        )

    draft_worker = CommunicationDraftWorker(
        repository=context_repository,
        client_factory=frappe_client_factory,
        worker_id="communication-draft-offline-1",
        clock=clock,
    )
    draft_result = draft_worker.run_once(SCOPE.site_id)
    assert draft_result == CommunicationDraftRunResult(
        status="succeeded",
        draft_id=next(iter(context_repository.receipts)),
        attempt=1,
    )
    assert len(frappe_transport.documents) == 1
    assert next(iter(frappe_transport.documents.values()))["values"]["review_status"] == "AI Draft"

    replay_acceptance = inbox.accept(
        SCOPE,
        KEY,
        first_message.raw_delivery,
        correlation_id="corr-offline-email-7",
    )
    assert replay_acceptance.job.job_id == accepted.job.job_id
    assert observer_repository.accept_count == 1
    assert (
        ObserverConnectorWorker(
            storage=storage,
            evidence_store=evidence_store,
            pipelines={
                "email": ConnectorPipeline(
                    decoder=EmailRawDeliveryDecoder(),
                    normalizer=EmailObservationNormalizer(
                        identity_resolver=identity_tokenizer,
                        site_id=SCOPE.site_id,
                        purpose=SCOPE.processing_purpose,
                    ),
                )
            },
            sink=sink,
            worker_id="observer-offline-restarted",
            clock=clock,
        ).run_once(SCOPE)
        is None
    )
    restarted_model = ModelProjectionWorker(
        outbox=model_outbox,
        publisher=publisher,
        worker_id="model-offline-restarted",
        clock=clock,
        lease_duration=timedelta(seconds=30),
        heartbeat_runner=_InlineHeartbeat(),
    )
    assert restarted_model.run_once(SCOPE).status is ProjectionRunStatus.IDLE
    restarted_draft = CommunicationDraftWorker(
        repository=context_repository,
        client_factory=frappe_client_factory,
        worker_id="communication-draft-offline-restarted",
        clock=clock,
    )
    assert restarted_draft.run_once(SCOPE.site_id).status == "idle"
    restarted_identity = _identity_worker(
        work,
        identity_projections,
        identity_transport,
        clock,
        worker_id="identity-offline-restarted",
    )
    assert restarted_identity.run_once(SCOPE).status is IdentityResolutionRunStatus.IDLE
    assert len(identity_transport.calls) == 4
    assert len(model_payloads) == 1
    assert len(ledger.records) == 1
    assert len(frappe_transport.documents) == 1
    assert all(
        len(identity_projections.history(SCOPE, "email", ref)) == 1 for ref in participant_refs
    )

    clock.advance(timedelta(minutes=1))
    server.messages[8] = (_raw_email(uid=8, body_suffix="two"), clock.value)
    second_poll = connector.poll(
        first_poll.checkpoint_candidate,
        username="offline-imap-user",
        password="offline-imap-secret",
    )
    assert second_poll.status == "ok" and len(second_poll.messages) == 1
    second_message = second_poll.messages[0]
    inbox.accept(
        SCOPE,
        KEY,
        second_message.raw_delivery,
        correlation_id="corr-offline-email-8",
    )
    second_observed = observer.run_once(SCOPE)
    assert second_observed is not None and second_observed.status == "succeeded"
    second_record = observer_repository.normalized[second_message.provider_event_id]
    assert second_record.source.participant_refs == participant_refs
    assert all(
        len(identity_projections.history(SCOPE, "email", ref)) == 1 for ref in participant_refs
    )
    assert restarted_identity.run_once(SCOPE).status is IdentityResolutionRunStatus.IDLE
    assert len(identity_transport.calls) == 4

    response_mode["model"] = "deepseek-chat"
    mismatch = model_worker.run_once(SCOPE)
    assert mismatch.status is ProjectionRunStatus.RETRY
    assert invocation_audits[-1].status == "failed"
    assert invocation_audits[-1].error_code == "model_mismatch"
    assert invocation_audits[-1].observed_model == "deepseek-chat"
    assert len(context_repository.publications) == 1
    assert len(context_repository.receipts) == 1
    assert len(frappe_transport.documents) == 1

    rendered_provider_requests = json.dumps(model_payloads, ensure_ascii=False, sort_keys=True)
    rendered_identity_requests = json.dumps(
        identity_transport.calls, ensure_ascii=False, sort_keys=True, default=str
    )
    rendered_surfaces = "\n".join(
        (
            rendered_provider_requests,
            rendered_identity_requests,
            repr(connector),
            repr(observer),
            repr(identity_worker),
            repr(publisher),
            repr(model_worker),
            repr(draft_worker),
            repr(mismatch),
            caplog.text,
        )
    )
    for raw_value in (
        SENDER_EMAIL,
        RECIPIENT_EMAIL,
        PERSON_NAME,
        ORGANIZATION,
        PHONE,
        MODEL_API_KEY,
        FRAPPE_API_SECRET,
    ):
        assert raw_value.casefold() not in rendered_surfaces.casefold()
    assert all(
        call["payload"]["payload"]["intent"]["doctype"] == "GBOS Informal Observation"
        for call in frappe_transport.calls
    )
    assert all(
        call["payload"]["payload"]["intent"]["values"]["review_status"] == "AI Draft"
        for call in frappe_transport.calls
    )
    gateway.close()
