from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .models import (
    AuditEntry,
    ByteLocator,
    CanonicalObservation,
    EntityResolutionProposal,
    EvidenceRecord,
    FactProposal,
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    TenantScope,
    stable_ulid,
)
from .protocols import (
    Clock,
    EvidenceStore,
    ManifestValidationHook,
    ObservationProcessor,
    ReviewCaseBridge,
)
from .security import LocalRequestAuthenticator, SignedServiceRequest

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLASSIFICATIONS = {"Public", "Internal", "Confidential", "Restricted"}
_RETENTION_CLASSES = {
    "R0-ephemeral",
    "R1-operational",
    "R2-record",
    "R3-legal-hold",
}
_CONSENT_BASES = {
    "consent",
    "contract",
    "legal_obligation",
    "legitimate_interest",
    "manual_import_pending_review",
}
_ARCHIVE_MAGIC = (
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07",
)


class IdempotencyConflict(ValueError):
    pass


class TenantAccessError(ValueError):
    pass


def _wire_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_import_body(
    manifest: ManualImportManifest,
    members: tuple[ManualImportMember, ...],
) -> bytes:
    payload = {
        "manifest": {
            **asdict(manifest),
            "occurred_at": _wire_datetime(manifest.occurred_at),
            "participants": [asdict(participant) for participant in manifest.participants],
        },
        "members": [
            {
                "name": member.name,
                "media_type": member.media_type,
                "size": len(member.content),
                "sha256": hashlib.sha256(member.content).hexdigest(),
            }
            for member in members
        ],
    }
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class DefaultManifestValidator:
    MAX_MEMBER_BYTES = 1_048_576
    MAX_TOTAL_BYTES = 1_048_576
    MAX_MEMBERS = 100

    def validate(
        self,
        scope: TenantScope,
        manifest: ManualImportManifest,
        members: tuple[ManualImportMember, ...],
    ) -> None:
        del scope
        if manifest.connector != "manual_import":
            raise ValueError("only manual_import is enabled")
        if manifest.consent_basis not in _CONSENT_BASES:
            raise ValueError("invalid consent_basis")
        if manifest.data_classification not in _CLASSIFICATIONS:
            raise ValueError("invalid data_classification")
        if manifest.retention_class not in _RETENTION_CLASSES:
            raise ValueError("invalid retention_class")
        if not manifest.participants:
            raise ValueError("at least one participant is required")
        if not 1 <= len(members) <= self.MAX_MEMBERS:
            raise ValueError("manual import member count outside allowed bounds")
        if sum(len(member.content) for member in members) > self.MAX_TOTAL_BYTES:
            raise ValueError("manual import exceeds total size budget")

        seen_names: set[str] = set()
        for member in members:
            self._validate_member(member)
            if member.name in seen_names:
                raise ValueError("duplicate member name")
            seen_names.add(member.name)

    def _validate_member(self, member: ManualImportMember) -> None:
        if (
            PurePosixPath(member.name).name != member.name
            or "\\" in member.name
            or not _SAFE_NAME.fullmatch(member.name)
        ):
            raise ValueError("unsafe manual import member name")
        if not member.content or len(member.content) > self.MAX_MEMBER_BYTES:
            raise ValueError("manual import member size outside allowed bounds")
        if any(member.content.startswith(magic) for magic in _ARCHIVE_MAGIC):
            raise ValueError("archive members are not accepted")

        expected_media_type = {
            ".txt": "text/plain",
            ".json": "application/json",
        }.get(PurePosixPath(member.name).suffix.lower())
        if expected_media_type is None or member.media_type != expected_media_type:
            raise ValueError("only matching UTF-8 text and JSON fixture members are accepted")
        try:
            text = member.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("manual import member is not valid UTF-8") from exc
        if "\x00" in text:
            raise ValueError("NUL is not accepted in inert fixture text")
        if member.media_type == "application/json":
            try:
                json.loads(
                    text,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON constant {value}")
                    ),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError("manual import JSON is invalid") from exc


class ManualImportPipeline:
    METHOD = "POST"
    PATH = "/internal/v1/manual-imports"

    def __init__(
        self,
        *,
        store: EvidenceStore,
        authenticator: LocalRequestAuthenticator,
        processor: ObservationProcessor,
        review_bridge: ReviewCaseBridge,
        clock: Clock,
        manifest_validator: ManifestValidationHook | None = None,
    ) -> None:
        self._store = store
        self._authenticator = authenticator
        self._processor = processor
        self._review_bridge = review_bridge
        self._clock = clock
        self._validator = manifest_validator or DefaultManifestValidator()
        self._idempotency: dict[tuple[str, str], tuple[str, ImportResult]] = {}
        self._observations: dict[str, CanonicalObservation] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._audit_entries: list[AuditEntry] = []

    @property
    def audit_entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._audit_entries)

    def ingest(
        self,
        *,
        scope: TenantScope,
        signed_request: SignedServiceRequest,
        idempotency_key: str,
        manifest: ManualImportManifest,
        members: tuple[ManualImportMember, ...],
    ) -> ImportResult:
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("invalid idempotency_key")
        body = canonical_import_body(manifest, members)
        authenticated_scope = self._authenticator.authenticate(signed_request, body)
        if signed_request.method != self.METHOD or signed_request.path != self.PATH:
            raise ValueError("manual import request target rejected")
        if authenticated_scope != scope:
            raise TenantAccessError("authenticated tenant scope mismatch")
        self._validator.validate(scope, manifest, members)

        payload_digest = hashlib.sha256(body).hexdigest()
        idempotency_scope = (scope.site_id, idempotency_key)
        existing = self._idempotency.get(idempotency_scope)
        if existing is not None:
            existing_digest, existing_result = existing
            if existing_digest != payload_digest:
                raise IdempotencyConflict("idempotency_conflict")
            return existing_result

        result = self._ingest_once(scope, manifest, members, payload_digest)
        self._idempotency[idempotency_scope] = (payload_digest, result)
        self._observations[result.observation.event_id] = result.observation
        self._evidence.update({record.evidence_id: record for record in result.evidence})
        self._audit_entries.append(
            AuditEntry(
                action="manual_import.stored",
                site_id=scope.site_id,
                processing_purpose=scope.processing_purpose,
                event_id=result.observation.event_id,
                evidence_ids=tuple(record.evidence_id for record in result.evidence),
                body_sha256=payload_digest,
                status=(
                    "pending_review"
                    if manifest.consent_basis == "manual_import_pending_review"
                    else "proposals_created"
                ),
                recorded_at=result.observation.ingested_at,
            )
        )
        return result

    def _ingest_once(
        self,
        scope: TenantScope,
        manifest: ManualImportManifest,
        members: tuple[ManualImportMember, ...],
        payload_digest: str,
    ) -> ImportResult:
        ingested_at = self._clock()
        event_id = stable_ulid(
            "observation",
            scope.site_id,
            scope.processing_purpose,
            payload_digest,
        )
        evidence_records: list[EvidenceRecord] = []
        fact_proposals: list[FactProposal] = []
        entity_proposals: dict[str, EntityResolutionProposal] = {}
        languages: list[str] = []

        for index, member in enumerate(members):
            stored = self._store.put(scope, member.content, media_type=member.media_type)
            evidence_id = stable_ulid(
                "evidence",
                scope.site_id,
                event_id,
                str(index),
                stored.sha256,
            )
            lineage = (
                f"manual_import:{manifest.fixture_id}",
                f"member:{member.name}",
            )
            evidence_records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    observation_event_id=event_id,
                    site_id=scope.site_id,
                    processing_purpose=scope.processing_purpose,
                    data_classification=manifest.data_classification,
                    source_lineage=lineage,
                    processor_version="manual-import-v1",
                    raw_sha256=stored.sha256,
                    object_ref=stored.object_ref,
                    media_type=stored.media_type,
                    locator=ByteLocator(0, stored.size),
                    created_at=ingested_at,
                    retention_class=manifest.retention_class,
                )
            )
            text = member.content.decode("utf-8")
            language_detector = getattr(self._processor, "detect_language", None)
            language = language_detector(text) if language_detector is not None else "und"
            languages.append(language)
            if manifest.consent_basis != "manual_import_pending_review":
                processed = self._processor.process(
                    scope=scope,
                    evidence_id=evidence_id,
                    text=text,
                    participants=manifest.participants,
                    data_classification=manifest.data_classification,
                    source_lineage=(event_id, *lineage),
                    recorded_at=ingested_at,
                )
                if any(proposal.status != "proposed" for proposal in processed.fact_proposals):
                    raise ValueError("processor emitted a non-proposed fact")
                if any(
                    proposal.status != "proposed"
                    for proposal in processed.entity_resolution_proposals
                ):
                    raise ValueError("processor emitted a non-proposed entity resolution")
                fact_proposals.extend(processed.fact_proposals)
                entity_proposals.update(
                    {
                        proposal.proposal_id: proposal
                        for proposal in processed.entity_resolution_proposals
                    }
                )

        evidence_ids = tuple(record.evidence_id for record in evidence_records)
        observation = CanonicalObservation(
            event_id=event_id,
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            connector="manual_import",
            channel="manual_import",
            occurred_at=manifest.occurred_at,
            ingested_at=ingested_at,
            original_language=languages[0] if len(set(languages)) == 1 else "mul",
            participants=manifest.participants,
            evidence_refs=evidence_ids,
            raw_sha256=payload_digest,
            consent_basis=manifest.consent_basis,
            data_classification=manifest.data_classification,
            retention_class=manifest.retention_class,
            correlation_id=manifest.correlation_id,
            source_lineage=(f"manual_import:{manifest.fixture_id}",),
            processor_version="manual-import-v1",
        )
        return ImportResult(
            observation=observation,
            evidence=tuple(evidence_records),
            fact_proposals=tuple(fact_proposals),
            entity_resolution_proposals=tuple(entity_proposals.values()),
        )

    def get_observation(self, scope: TenantScope, event_id: str) -> CanonicalObservation:
        observation = self._observations.get(event_id)
        if (
            observation is None
            or observation.site_id != scope.site_id
            or observation.processing_purpose != scope.processing_purpose
        ):
            raise TenantAccessError("observation not found in tenant scope")
        return observation

    def get_evidence(self, scope: TenantScope, evidence_id: str) -> EvidenceRecord:
        evidence = self._evidence.get(evidence_id)
        if (
            evidence is None
            or evidence.site_id != scope.site_id
            or evidence.processing_purpose != scope.processing_purpose
        ):
            raise TenantAccessError("evidence not found in tenant scope")
        return evidence
