from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .evidence_store import EvidenceIntegrityError, SiteIsolationError
from .local_pilot_storage import (
    AuthenticatedIngressMetadata,
    InboundDeliveryMetadata,
    JobConflict,
    LocalPilotStorage,
    ProcessingJobMetadata,
)
from .models import ConnectorItem, ConnectorKey, RawDelivery, TenantScope, stable_ulid
from .protocols import (
    Clock,
    DeliveryDecoder,
    EvidenceStore,
    NormalizedObservationSink,
    ObservationNormalizer,
)

_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,79}$")


class DeliveryIntegrityError(ValueError):
    """Persisted delivery metadata does not match the claimed site-local job."""


class DeliveryQuarantine(ValueError):
    """A provider decoder rejected an envelope without treating it as transient."""

    def __init__(self, reason_code: str) -> None:
        if not _REASON_CODE.fullmatch(reason_code):
            raise ValueError("invalid delivery quarantine reason code")
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class DurableDeliveryAcceptance:
    delivery: InboundDeliveryMetadata
    job: ProcessingJobMetadata


@dataclass(frozen=True, slots=True)
class AuthenticatedDeliveryAcceptance:
    disposition: str

    def __post_init__(self) -> None:
        if self.disposition not in {"accepted", "duplicate"}:
            raise ValueError("invalid authenticated ingress disposition")


@dataclass(frozen=True, slots=True)
class DeliveryWorkResult:
    job_id: str
    status: str
    normalized_count: int


class DurableDeliveryInbox:
    """Persists authenticated exact bytes before atomically accepting durable work."""

    def __init__(
        self,
        *,
        storage: LocalPilotStorage,
        evidence_store: EvidenceStore,
    ) -> None:
        self._storage = storage
        self._evidence_store = evidence_store

    def accept(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        correlation_id: str,
        max_attempts: int = 3,
    ) -> DurableDeliveryAcceptance:
        stored = self._evidence_store.put(
            scope,
            delivery.exact_bytes,
            media_type=delivery.media_type,
        )
        identity_digest = hashlib.sha256(
            "\x1f".join(
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    delivery.delivery_id,
                )
            ).encode()
        ).hexdigest()
        accepted_delivery, job = self._storage.accept_and_enqueue_delivery(
            scope,
            key,
            delivery_id=delivery.delivery_id,
            exact_body_sha256=stored.sha256,
            object_ref=stored.object_ref,
            byte_size=stored.size,
            media_type=stored.media_type,
            received_at=delivery.received_at,
            correlation_id=correlation_id,
            job_id=stable_ulid("delivery-job", identity_digest, "0"),
            idempotency_key=f"delivery:{identity_digest}",
            max_attempts=max_attempts,
        )
        return DurableDeliveryAcceptance(delivery=accepted_delivery, job=job)

    def accept_authenticated(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        delivery: RawDelivery,
        *,
        correlation_id: str,
        nonce: str,
        nonce_expires_at: datetime,
        now: datetime,
        max_attempts: int = 3,
    ) -> AuthenticatedDeliveryAcceptance:
        stored = self._evidence_store.put(
            scope,
            delivery.exact_bytes,
            media_type=delivery.media_type,
        )
        job_identity = hashlib.sha256(
            "\x1f".join(
                (
                    scope.site_id,
                    key.connector,
                    key.instance_id,
                    nonce,
                )
            ).encode()
        ).hexdigest()
        accepted: AuthenticatedIngressMetadata = self._storage.accept_authenticated_delivery(
            scope,
            key,
            delivery_id=delivery.delivery_id,
            exact_body_sha256=stored.sha256,
            object_ref=stored.object_ref,
            byte_size=stored.size,
            media_type=stored.media_type,
            received_at=delivery.received_at,
            correlation_id=correlation_id,
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            now=now,
            job_id=stable_ulid("authenticated-delivery-job", job_identity),
            max_attempts=max_attempts,
        )
        return AuthenticatedDeliveryAcceptance(
            disposition=accepted.disposition,
        )


class DeliveryWorker:
    """Claims one durable delivery job and delegates provider semantics to adapters."""

    def __init__(
        self,
        *,
        storage: LocalPilotStorage,
        evidence_store: EvidenceStore,
        decoder: DeliveryDecoder,
        normalizer: ObservationNormalizer,
        sink: NormalizedObservationSink,
        worker_id: str,
        clock: Clock,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
    ) -> None:
        if not worker_id or len(worker_id) > 256:
            raise ValueError("invalid worker_id")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be a positive bounded integer")
        if not 1 <= retry_delay_seconds <= 86_400:
            raise ValueError("retry_delay_seconds must be a positive bounded integer")
        self._storage = storage
        self._evidence_store = evidence_store
        self._decoder = decoder
        self._normalizer = normalizer
        self._sink = sink
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    def run_once(self, scope: TenantScope) -> DeliveryWorkResult | None:
        claimed_at = self._clock()
        job = self._storage.claim_processing_job(
            scope,
            worker_id=self._worker_id,
            now=claimed_at,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return None
        key = ConnectorKey(job.connector, job.connector_instance_id)
        self._validate_claim_scope(scope, key, job)
        delivery = self._storage.get_inbound_delivery(
            scope,
            key,
            delivery_id=job.delivery_id,
        )
        self._validate_delivery_scope(scope, key, job, delivery)

        try:
            exact_bytes = self._evidence_store.read(scope, delivery.object_ref)
            self._verify_content(delivery, exact_bytes)
            heartbeat_at = self._clock()
            job = self._storage.heartbeat_processing_job(
                scope,
                job_id=job.job_id,
                worker_id=self._worker_id,
                expected_attempt=job.attempt_count,
                expected_lease_generation=job.lease_generation,
                now=heartbeat_at,
                lease_seconds=self._lease_seconds,
            )
            items = self._decoder.decode(exact_bytes)
            self._validate_items(items)
            for item in items:
                normalized = self._normalizer.normalize(item)
                self._sink.accept(scope, key, job, item, normalized)
        except JobConflict:
            raise
        except DeliveryQuarantine as exc:
            return self._quarantine(scope, job, exc.reason_code)
        except (
            DeliveryIntegrityError,
            EvidenceIntegrityError,
            SiteIsolationError,
            FileNotFoundError,
        ):
            return self._quarantine(scope, job, "evidence_integrity_mismatch")
        except Exception:
            return self._retry(scope, job)

        completed = self._storage.complete_processing_job(
            scope,
            job_id=job.job_id,
            worker_id=self._worker_id,
            expected_attempt=job.attempt_count,
            expected_lease_generation=job.lease_generation,
            now=self._clock(),
            provider_event_ids=tuple(item.provider_event_id for item in items),
        )
        return DeliveryWorkResult(
            job_id=completed.job_id,
            status=completed.status,
            normalized_count=len(items),
        )

    @staticmethod
    def _validate_claim_scope(
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
    ) -> None:
        if (
            job.site_id != scope.site_id
            or job.connector != key.connector
            or job.connector_instance_id != key.instance_id
            or job.status != "processing"
            or job.lease_owner is None
        ):
            raise DeliveryIntegrityError("claimed job is outside the requested scope")

    @staticmethod
    def _validate_delivery_scope(
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        delivery: InboundDeliveryMetadata,
    ) -> None:
        if (
            delivery.site_id != scope.site_id
            or delivery.connector != key.connector
            or delivery.connector_instance_id != key.instance_id
            or delivery.delivery_id != job.delivery_id
        ):
            raise DeliveryIntegrityError("delivery is outside the claimed job scope")

    @staticmethod
    def _verify_content(
        delivery: InboundDeliveryMetadata,
        exact_bytes: bytes,
    ) -> None:
        if len(exact_bytes) != delivery.byte_size:
            raise DeliveryIntegrityError("stored delivery size mismatch")
        digest = hashlib.sha256(exact_bytes).hexdigest()
        if not hmac.compare_digest(digest, delivery.exact_body_sha256):
            raise DeliveryIntegrityError("stored delivery digest mismatch")

    @staticmethod
    def _validate_items(items: tuple[ConnectorItem, ...]) -> None:
        if not isinstance(items, tuple):
            raise DeliveryQuarantine("invalid_decoder_result")
        if not all(isinstance(item, ConnectorItem) for item in items):
            raise DeliveryQuarantine("invalid_decoder_result")
        provider_event_ids = tuple(item.provider_event_id for item in items)
        if len(provider_event_ids) != len(set(provider_event_ids)):
            raise DeliveryQuarantine("duplicate_provider_event")

    def _quarantine(
        self,
        scope: TenantScope,
        job: ProcessingJobMetadata,
        reason_code: str,
    ) -> DeliveryWorkResult:
        quarantined = self._storage.quarantine_processing_job(
            scope,
            job_id=job.job_id,
            worker_id=self._worker_id,
            expected_attempt=job.attempt_count,
            expected_lease_generation=job.lease_generation,
            now=self._clock(),
            reason_code=reason_code,
        )
        return DeliveryWorkResult(
            job_id=quarantined.job_id,
            status=quarantined.status,
            normalized_count=0,
        )

    def _retry(
        self,
        scope: TenantScope,
        job: ProcessingJobMetadata,
    ) -> DeliveryWorkResult:
        now = self._clock()
        retried = self._storage.retry_processing_job(
            scope,
            job_id=job.job_id,
            worker_id=self._worker_id,
            expected_attempt=job.attempt_count,
            expected_lease_generation=job.lease_generation,
            now=now,
            next_retry_at=now + timedelta(seconds=self._retry_delay_seconds),
            error_code="delivery_processing_failed",
        )
        return DeliveryWorkResult(
            job_id=retried.job_id,
            status=retried.status,
            normalized_count=0,
        )
