"""Connector-dispatching Observer worker composition."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from services.observer.observer.evidence_store import (
    EvidenceIntegrityError,
    SiteIsolationError,
)
from services.observer.observer.local_pilot_ingestion import (
    DeliveryIntegrityError,
    DeliveryQuarantine,
    DeliveryWorkResult,
)
from services.observer.observer.local_pilot_storage import (
    InboundDeliveryMetadata,
    JobConflict,
    LocalPilotStorage,
    NormalizedBatchConflict,
    ProcessingJobMetadata,
)
from services.observer.observer.models import (
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
)
from services.observer.observer.normalizers import NormalizationRejected
from services.observer.observer.protocols import (
    DeliveryDecoder,
    DeliveryObservationNormalizer,
    EvidenceStore,
    NormalizedObservationSink,
)

_SUPPORTED_CONNECTORS = frozenset({"email", "wecom", "whatsapp"})


@dataclass(frozen=True, slots=True, repr=False)
class ConnectorPipeline:
    """Credential-free decode/normalize pair for one claimed connector."""

    decoder: DeliveryDecoder
    normalizer: DeliveryObservationNormalizer

    def __post_init__(self) -> None:
        if not callable(getattr(self.decoder, "decode", None)):
            raise TypeError("connector decoder must be callable")
        if not callable(getattr(self.normalizer, "normalize", None)):
            raise TypeError("connector normalizer must be callable")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(decoder=<redacted>, normalizer=<redacted>)"


class ObserverConnectorWorker:
    """Claim once, dispatch by persisted connector, and commit one normalized batch."""

    __slots__ = (
        "_clock",
        "_evidence_store",
        "_lease_seconds",
        "_pipelines",
        "_retry_delay_seconds",
        "_sink",
        "_storage",
        "_worker_id",
    )

    def __init__(
        self,
        *,
        storage: LocalPilotStorage,
        evidence_store: EvidenceStore,
        pipelines: Mapping[str, ConnectorPipeline],
        sink: NormalizedObservationSink,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip() or len(worker_id) > 256:
            raise ValueError("invalid worker_id")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be a positive bounded integer")
        if not 1 <= retry_delay_seconds <= 86_400:
            raise ValueError("retry_delay_seconds must be a positive bounded integer")
        copied = dict(pipelines)
        if set(copied) - _SUPPORTED_CONNECTORS or not all(
            isinstance(value, ConnectorPipeline) for value in copied.values()
        ):
            raise ValueError("invalid connector pipeline map")
        self._storage = storage
        self._evidence_store = evidence_store
        self._pipelines = MappingProxyType(copied)
        self._sink = sink
        self._worker_id = worker_id
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(storage=<redacted>, evidence_store=<redacted>, "
            f"pipeline_count={len(self._pipelines)}, sink=<redacted>, worker_id=<redacted>)"
        )

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
        pipeline = self._pipelines.get(key.connector)
        if pipeline is None:
            return self._quarantine(scope, job, "unsupported_connector")
        delivery = self._storage.get_inbound_delivery(
            scope,
            key,
            delivery_id=job.delivery_id,
        )
        self._validate_delivery_scope(scope, key, job, delivery)

        try:
            exact_bytes = self._evidence_store.read(scope, delivery.object_ref)
            self._verify_content(delivery, exact_bytes)
            job = self._storage.heartbeat_processing_job(
                scope,
                job_id=job.job_id,
                worker_id=self._worker_id,
                expected_attempt=job.attempt_count,
                expected_lease_generation=job.lease_generation,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
            items = pipeline.decoder.decode(exact_bytes)
            self._validate_items(items)
            normalized = tuple(
                pipeline.normalizer.normalize(
                    item,
                    source_ref=delivery.object_ref,
                )
                for item in items
            )
            self._validate_normalized(items, normalized)
            self._sink.accept_batch(scope, key, job, items, normalized)
        except JobConflict:
            raise
        except NormalizedBatchConflict:
            return self._quarantine(
                scope,
                job,
                "normalized_payload_conflict",
            )
        except NormalizationRejected as exc:
            return self._quarantine(scope, job, exc.code)
        except DeliveryQuarantine as exc:
            return self._quarantine(scope, job, exc.reason_code)
        except (
            DeliveryIntegrityError,
            EvidenceIntegrityError,
            SiteIsolationError,
            FileNotFoundError,
        ):
            return self._quarantine(
                scope,
                job,
                "evidence_integrity_mismatch",
            )
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
        if not isinstance(items, tuple) or not all(
            isinstance(item, ConnectorItem) for item in items
        ):
            raise DeliveryQuarantine("invalid_decoder_result")
        provider_event_ids = tuple(item.provider_event_id for item in items)
        if len(provider_event_ids) != len(set(provider_event_ids)):
            raise DeliveryQuarantine("duplicate_provider_event")

    @staticmethod
    def _validate_normalized(
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> None:
        if len(items) != len(normalized) or not all(
            isinstance(value, NormalizedObservationInput) for value in normalized
        ):
            raise DeliveryQuarantine("invalid_normalizer_result")

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


def main() -> int:
    """Refuse standalone processing without injected durable dependencies."""

    return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConnectorPipeline",
    "ObserverConnectorWorker",
    "main",
]
