from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .models import (
    ConnectorBatch,
    ConnectorItem,
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


class ObservationNormalizer(Protocol):
    def normalize(self, item: ConnectorItem) -> NormalizedObservationInput: ...


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
