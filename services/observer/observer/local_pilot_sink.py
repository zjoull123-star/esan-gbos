from __future__ import annotations

from typing import Protocol

from .local_pilot_storage import (
    PersistedNormalizedBatch,
    ProcessingJobMetadata,
)
from .models import (
    ConnectorItem,
    ConnectorKey,
    NormalizedObservationInput,
    TenantScope,
)


class NormalizedBatchStorage(Protocol):
    def persist_normalized_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch: ...


class PostgresNormalizedObservationSink:
    """Thin batch-only handoff to the transaction-owning PostgreSQL repository."""

    __slots__ = ("_storage",)

    def __init__(self, *, storage: NormalizedBatchStorage) -> None:
        self._storage = storage

    def __repr__(self) -> str:
        return f"{type(self).__name__}(storage=<redacted>)"

    def accept_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch:
        return self._storage.persist_normalized_batch(
            scope,
            key,
            job,
            items,
            normalized,
        )
