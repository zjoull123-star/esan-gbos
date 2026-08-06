from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException

from services.context.context_service.client import (
    ContextClientError,
    HttpContextRepository,
)
from services.context.context_service.models import (
    IdempotencyConflict as ContextIdempotencyConflict,
)
from services.context.context_service.publisher import ContextPublisher

from .application import IdempotencyConflict, ManualImportPipeline, TenantAccessError
from .contracts import ContractValidationError, ContractValidator
from .evidence_store import ContentAddressedEvidenceStore
from .models import ManualImportManifest, ManualImportMember, Participant, TenantScope
from .processing import DeterministicProcessor, DisabledReviewCaseBridge
from .protocols import ContextPublication
from .security import (
    AuthenticationError,
    LocalRequestAuthenticator,
    NonceReplayError,
    NonceStore,
    SignedServiceRequest,
)
from .storage import (
    IdempotencyConflict as PersistenceIdempotencyConflict,
)
from .storage import (
    ObserverPersistence,
    PostgresObserverRepository,
    connect_postgres_components,
)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _required(value: str | None, name: str) -> str:
    if not value:
        raise HTTPException(status_code=401, detail=f"missing {name}")
    return value


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid signed timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(status_code=401, detail="signed timestamp must include timezone")
    return parsed


def _parse_payload(
    payload: dict[str, Any],
    *,
    scope: TenantScope,
    idempotency_key: str,
    contract_validator: ContractValidator,
) -> tuple[ManualImportManifest, tuple[ManualImportMember, ...]]:
    try:
        manifest_data = payload["manifest"]
        members_data = payload["members"]
        if not isinstance(manifest_data, dict) or not isinstance(members_data, list):
            raise ValueError("manifest and members must use governed object/list shapes")
        contract_validator.validate_gate3(
            "manual-import-manifest.schema.json",
            manifest_data,
        )
        if manifest_data["site_id"] != scope.site_id:
            raise ValueError("manifest site does not match authenticated site")
        if manifest_data["processing_purpose"] != scope.processing_purpose:
            raise ValueError("manifest purpose does not match authenticated purpose")
        if manifest_data["idempotency_key"] != idempotency_key:
            raise ValueError("manifest idempotency key does not match request")
        if len(members_data) != 1:
            raise ValueError("Gate 3 local runtime accepts exactly one inert fixture member")
        participants = tuple(
            Participant(
                role=str(item["role"]),
                identity_ref=str(item["identity_ref"]),
                display_name=(
                    str(item["display_name"]) if item.get("display_name") is not None else None
                ),
            )
            for item in manifest_data["participants"]
        )
        source = manifest_data["source"]
        manifest = ManualImportManifest(
            connector=str(source["connector"]),
            fixture_id=str(manifest_data["manifest_id"]),
            occurred_at=_parse_timestamp(str(manifest_data["occurred_at"])),
            consent_basis=str(manifest_data["consent_basis"]),
            data_classification=str(manifest_data["data_classification"]),
            retention_class=str(manifest_data["retention_class"]),
            participants=participants,
            correlation_id=str(manifest_data["correlation_id"]),
            provider_event_id=(
                str(source["provider_event_id"])
                if source.get("provider_event_id") is not None
                else None
            ),
        )
        members = tuple(
            ManualImportMember(
                name=str(item["name"]),
                media_type=str(item["media_type"]),
                content=str(item["content_utf8"]).encode("utf-8"),
            )
            for item in members_data
        )
        package = manifest_data["package"]
        member = members[0]
        if package["filename"] != member.name or package["media_type"] != member.media_type:
            raise ValueError("package metadata does not match fixture member")
        if package["size_bytes"] != len(member.content):
            raise ValueError("package size does not match fixture member")
        if package["sha256"] != hashlib.sha256(member.content).hexdigest():
            raise ValueError("package digest does not match fixture member")
        budgets = manifest_data["budgets"]
        if len(member.content) > budgets["body_bytes"]:
            raise ValueError("fixture exceeds body budget")
        if len(member.content) > budgets["decompressed_bytes"]:
            raise ValueError("fixture exceeds decompressed budget")
        if budgets["attachment_count"] != 0 or budgets["attachment_bytes"] != 0:
            raise ValueError("attachments are disabled in Gate 3 local runtime")
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid manual import fixture") from exc
    return manifest, members


def create_observer_app(
    *,
    pipeline: ManualImportPipeline | None = None,
    persistence: ObserverPersistence | None = None,
    context_publisher: ContextPublication | None = None,
    contract_validator: ContractValidator | None = None,
    checkpoint_id: str = "manual-import-main",
    replay_window_seconds: int = 86_400,
) -> FastAPI:
    validator = contract_validator or ContractValidator.repository_default()
    application = FastAPI(
        title="ESAN GBOS Gate 3 Observer",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "gate": 3,
            "runtime_enabled": (
                pipeline is not None and persistence is not None and context_publisher is not None
            ),
            "connectors": ["manual_import"],
            "capabilities": {
                "real_connectors": False,
                "model_network": False,
                "tools": False,
                "frappe_bridge": False,
                "kingdee": False,
                "external_side_effects": False,
            },
        }

    @application.post("/internal/v1/manual-imports")
    def manual_import(
        payload: Annotated[dict[str, Any], Body()],
        x_gbos_identity: Annotated[str | None, Header(alias="X-GBOS-Identity")] = None,
        x_gbos_timestamp: Annotated[str | None, Header(alias="X-GBOS-Timestamp")] = None,
        x_gbos_nonce: Annotated[str | None, Header(alias="X-GBOS-Nonce")] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_processing_purpose: Annotated[str | None, Header(alias="X-Processing-Purpose")] = None,
        x_gbos_body_sha256: Annotated[str | None, Header(alias="X-GBOS-Body-SHA256")] = None,
        x_gbos_signature: Annotated[str | None, Header(alias="X-GBOS-Signature")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> dict[str, Any]:
        if pipeline is None or persistence is None or context_publisher is None:
            raise HTTPException(status_code=503, detail="manual import runtime is disabled")
        identity = _required(x_gbos_identity, "service identity")
        timestamp = _parse_timestamp(_required(x_gbos_timestamp, "signed timestamp"))
        nonce = _required(x_gbos_nonce, "nonce")
        site_id = _required(x_site_id, "site")
        purpose = _required(x_processing_purpose, "processing purpose")
        body_sha256 = _required(x_gbos_body_sha256, "body digest")
        signature = _required(x_gbos_signature, "signature")
        command_key = _required(idempotency_key, "idempotency key")
        request_id = _required(x_request_id, "request ID")
        try:
            scope = TenantScope(site_id=site_id, processing_purpose=purpose)
            manifest, members = _parse_payload(
                payload,
                scope=scope,
                idempotency_key=command_key,
                contract_validator=validator,
            )
            signed = SignedServiceRequest(
                identity=identity,
                method="POST",
                path="/internal/v1/manual-imports",
                timestamp=timestamp,
                nonce=nonce,
                scope=scope,
                body_sha256=body_sha256,
                signature=signature,
            )
            result = pipeline.ingest(
                scope=scope,
                signed_request=signed,
                idempotency_key=command_key,
                manifest=manifest,
                members=members,
            )
            persisted = persistence.persist(
                scope,
                idempotency_key=command_key,
                payload_digest=result.observation.raw_sha256,
                result=result,
                provider_event_id=manifest.provider_event_id,
                checkpoint_id=checkpoint_id,
                replay_window_seconds=replay_window_seconds,
            )
            published_records: tuple[object, ...] = ()
            if persisted.status == "stored":
                published_records = context_publisher.publish(
                    result,
                    correlation_id=manifest.correlation_id,
                    recorded_at=persisted.ingested_at,
                )
        except (AuthenticationError, NonceReplayError) as exc:
            raise HTTPException(status_code=401, detail="service identity rejected") from exc
        except TenantAccessError as exc:
            raise HTTPException(status_code=403, detail="tenant scope rejected") from exc
        except (
            ContextIdempotencyConflict,
            IdempotencyConflict,
            PersistenceIdempotencyConflict,
        ) as exc:
            raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
        except ContextClientError as exc:
            raise HTTPException(
                status_code=503,
                detail="context_publication_failed",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "data": {
                "ingestion": _json_value(persisted),
                "proposal_counts": {
                    "fact": (len(result.fact_proposals) if persisted.status == "stored" else 0),
                    "entity_resolution": (
                        len(result.entity_resolution_proposals)
                        if persisted.status == "stored"
                        else 0
                    ),
                },
                "context_publication": {
                    "status": ("published" if persisted.status == "stored" else "skipped"),
                    "record_count": len(published_records),
                },
            },
            "meta": {
                "request_id": request_id,
                "schema_version": "1.0",
                "runtime": "gate3-local",
            },
        }

    return application


def _runtime_from_local_environment() -> tuple[
    ManualImportPipeline | None,
    ObserverPersistence | None,
    ContextPublication | None,
]:
    identity = os.getenv("GBOS_OBSERVER_LOCAL_IDENTITY")
    key = os.getenv("GBOS_OBSERVER_LOCAL_IDENTITY_KEY")
    object_root = os.getenv("GBOS_OBSERVER_OBJECT_ROOT")
    if not identity or not key or not object_root:
        return None, None, None
    if os.getenv("GBOS_PRODUCTION_ENABLED", "false").lower() in {"true", "1", "yes"}:
        return None, None, None
    if os.getenv("GBOS_OBSERVER_DATABASE_ENABLED", "false").lower() not in {
        "true",
        "1",
        "yes",
    }:
        return None, None, None
    connection = connect_postgres_components(
        host=os.environ["GBOS_OBSERVER_DATABASE_HOST"],
        port=int(os.environ["GBOS_OBSERVER_DATABASE_PORT"]),
        database=os.environ["GBOS_OBSERVER_DATABASE_NAME"],
        user=os.environ["GBOS_OBSERVER_DATABASE_USER"],
        password=os.environ["GBOS_OBSERVER_DATABASE_PASSWORD"],
    )
    pipeline = ManualImportPipeline(
        store=ContentAddressedEvidenceStore(Path(object_root)),
        authenticator=LocalRequestAuthenticator(
            identity=identity,
            secret=key.encode(),
            nonce_store=NonceStore(),
            clock=lambda: datetime.now(UTC),
        ),
        processor=DeterministicProcessor(),
        review_bridge=DisabledReviewCaseBridge(),
        clock=lambda: datetime.now(UTC),
    )
    publisher: ContextPublication | None = None
    if os.getenv("GBOS_CONTEXT_WRITE_ENABLED", "false").lower() in {
        "true",
        "1",
        "yes",
    }:
        context_url = os.getenv("GBOS_CONTEXT_LOCAL_URL")
        context_token = os.getenv("GBOS_CONTEXT_LOCAL_TOKEN")
        if not context_url or not context_token:
            raise RuntimeError("Gate 3 Context publication requires its local URL and token")
        publisher = ContextPublisher(
            HttpContextRepository(
                base_url=context_url,
                token=context_token,
            )
        )
    return pipeline, PostgresObserverRepository(connection), publisher


_runtime_pipeline, _runtime_persistence, _runtime_context_publisher = (
    _runtime_from_local_environment()
)
app = create_observer_app(
    pipeline=_runtime_pipeline,
    persistence=_runtime_persistence,
    context_publisher=_runtime_context_publisher,
)
