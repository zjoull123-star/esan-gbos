from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Protocol

from .local_pilot_storage import (
    PersistedNormalizedBatch,
    ProcessingJobMetadata,
)
from .models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    TenantScope,
)
from .protocols import EvidenceStore

_CAS_REFERENCE = re.compile(r"^obs:v1:([a-f0-9]{32}):sha256:([a-f0-9]{64})$")


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

    __slots__ = ("_evidence_store", "_storage")

    def __init__(
        self,
        *,
        storage: NormalizedBatchStorage,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._storage = storage
        self._evidence_store = evidence_store

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(storage=<redacted>, "
            f"evidence_store={'<redacted>' if self._evidence_store is not None else 'None'})"
        )

    def accept_batch(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        job: ProcessingJobMetadata,
        items: tuple[ConnectorItem, ...],
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> PersistedNormalizedBatch:
        references_only = self._materialize_content(scope, key, normalized)
        return self._storage.persist_normalized_batch(
            scope,
            key,
            job,
            items,
            references_only,
        )

    def _materialize_content(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        normalized: tuple[NormalizedObservationInput, ...],
    ) -> tuple[NormalizedObservationInput, ...]:
        if not any(
            artifact.content is not None for value in normalized for artifact in value.evidence
        ):
            return normalized
        if self._evidence_store is None:
            raise ValueError("transient content requires an evidence store")
        materialized: list[NormalizedObservationInput] = []
        for value in normalized:
            artifacts: list[EvidenceArtifact] = []
            for artifact in value.evidence:
                if artifact.content is None:
                    artifacts.append(artifact)
                    continue
                self._validate_transient(key, artifact)
                content = artifact.content
                expected_digest = hashlib.sha256(content).hexdigest()
                stored = self._evidence_store.put(
                    scope,
                    content,
                    media_type=artifact.media_type,
                )
                match = _CAS_REFERENCE.fullmatch(stored.object_ref)
                expected_partition = hashlib.sha256(f"site:{scope.site_id}".encode()).hexdigest()[
                    :32
                ]
                if (
                    stored.media_type != artifact.media_type
                    or stored.size != len(content)
                    or stored.sha256 != expected_digest
                    or match is None
                    or match.group(1) != expected_partition
                    or match.group(2) != expected_digest
                ):
                    raise ValueError("materialized evidence integrity mismatch")
                artifacts.append(
                    replace(
                        artifact,
                        content=None,
                        reference=stored.object_ref,
                    )
                )
            materialized.append(replace(value, evidence=tuple(artifacts)))
        return tuple(materialized)

    @staticmethod
    def _validate_transient(
        key: ConnectorKey,
        artifact: EvidenceArtifact,
    ) -> None:
        is_body = (
            artifact.locator == "message-body"
            and artifact.role == "derived-text"
            and artifact.media_type == "text/plain; charset=utf-8"
        )
        is_attachment = (
            artifact.role == "attachment"
            and re.fullmatch(r"attachment:[1-9][0-9]{0,3}", artifact.locator) is not None
        )
        if key.connector != "email" or not (is_body or is_attachment):
            raise ValueError("transient evidence locator or role is invalid")
