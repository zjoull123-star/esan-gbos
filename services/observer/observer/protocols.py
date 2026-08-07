from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .local_pilot_storage import ProcessingJobMetadata
from .models import (
    ConnectorBatch,
    ConnectorItem,
    ConnectorKey,
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    NormalizedObservationInput,
    Participant,
    ProcessingResult,
    RawDelivery,
    StoredObject,
    TenantScope,
    TranscriptSegments,
)

Clock = Callable[[], datetime]


class EvidenceStore(Protocol):
    def put(self, scope: TenantScope, content: bytes, *, media_type: str) -> StoredObject: ...

    def read(self, scope: TenantScope, object_ref: str) -> bytes: ...

    def delete(self, scope: TenantScope, object_ref: str) -> None: ...

    def exists(self, scope: TenantScope, object_ref: str) -> bool: ...


class PullConnector(Protocol):
    def fetch(self, checkpoint: str | None, limit: int) -> ConnectorBatch: ...


class DeliveryAuthenticator(Protocol):
    def verify(self, exact_request: bytes) -> RawDelivery: ...


class DeliveryDecoder(Protocol):
    """Provider adapter boundary; implementations own envelope parsing."""

    def decode(self, exact_bytes: bytes) -> tuple[ConnectorItem, ...]: ...


class ObservationNormalizer(Protocol):
    def normalize(self, item: ConnectorItem) -> NormalizedObservationInput: ...


class DeliveryObservationNormalizer(Protocol):
    """Normalizer that receives the opaque source object reference for one delivery."""

    def normalize(
        self,
        item: ConnectorItem,
        *,
        source_ref: str,
    ) -> NormalizedObservationInput: ...


class NormalizedObservationSink(Protocol):
    """Durable handoff for normalized envelopes emitted by a delivery job."""

    def accept_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> object: ...


class SpeechProvider(Protocol):
    def transcribe(self, evidence_ref: str) -> TranscriptSegments: ...


class ManifestValidationHook(Protocol):
    def validate(
        self,
        scope: TenantScope,
        manifest: ManualImportManifest,
        members: tuple[ManualImportMember, ...],
    ) -> None: ...


class ObservationProcessor(Protocol):
    def process(
        self,
        *,
        scope: TenantScope,
        evidence_id: str,
        text: str,
        participants: tuple[Participant, ...],
        data_classification: str,
        source_lineage: tuple[str, ...],
        recorded_at: datetime,
    ) -> ProcessingResult: ...


class ReviewCaseBridge(Protocol):
    @property
    def call_count(self) -> int: ...

    def create_review_case(self, *_args: object, **_kwargs: object) -> None: ...


class ContextPublication(Protocol):
    def publish(
        self,
        result: ImportResult,
        *,
        correlation_id: str,
        recorded_at: datetime | None = None,
    ) -> tuple[object, ...]: ...
