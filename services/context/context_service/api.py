from __future__ import annotations

import hmac
import os
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import Body, FastAPI, Header, HTTPException

from .contracts import ContextContractValidator, ContractValidationError
from .models import (
    GovernedEnvelope,
    IdempotencyConflict,
    RecordKind,
    RecordMetadata,
    TenantScope,
    ValidationError,
)
from .repositories import ContextRepository, InMemoryContextRepository
from .storage import PostgresContextRepository, connect_postgres_components

HeaderValue = Annotated[str | None, Header()]


def _success(data: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "request_id": request_id,
            "schema_version": "1.0",
            "runtime": "gate3-local",
        },
    }


def _metadata(metadata: RecordMetadata) -> dict[str, Any]:
    value = asdict(metadata)
    value["kind"] = metadata.kind.value
    value["recorded_at"] = metadata.recorded_at.isoformat()
    return value


def _authorize(
    *,
    configured_token: str | None,
    authorization: str | None,
    site_id: str | None,
    purpose: str | None,
    request_id: str | None,
    idempotency_key: str | None,
) -> tuple[TenantScope, str, str]:
    if configured_token is None:
        raise HTTPException(status_code=503, detail="local context identity is disabled")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer identity")
    supplied = authorization[len(prefix) :]
    if not hmac.compare_digest(supplied, configured_token):
        raise HTTPException(status_code=401, detail="invalid bearer identity")
    if not site_id or not purpose or not request_id or not idempotency_key:
        raise HTTPException(status_code=400, detail="required governed header is missing")
    try:
        scope = TenantScope(site_id=site_id, processing_purpose=purpose)
    except ValidationError as exc:
        raise HTTPException(status_code=403, detail="invalid tenant scope") from exc
    return scope, request_id, idempotency_key


def create_context_app(
    *,
    repository: ContextRepository | None = None,
    local_token: str | None = None,
    contract_validator: ContextContractValidator | None = None,
) -> FastAPI:
    context_repository = repository or InMemoryContextRepository()
    validator = contract_validator or ContextContractValidator.repository_default()
    application = FastAPI(
        title="ESAN GBOS Gate 3 Context Service",
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
            "capabilities": {
                "proposal_only": True,
                "agent_runtime": False,
                "frappe_write": False,
                "kingdee": False,
                "model_network": False,
                "external_side_effects": False,
            },
        }

    def save_record(
        *,
        kind: RecordKind,
        payload: dict[str, Any],
        authorization: str | None,
        site_id: str | None,
        purpose: str | None,
        request_id: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        scope, resolved_request_id, resolved_key = _authorize(
            configured_token=local_token,
            authorization=authorization,
            site_id=site_id,
            purpose=purpose,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        try:
            validator.validate(kind, payload)
            envelope = GovernedEnvelope.from_payload(
                site_id=scope.site_id,
                processing_purpose=scope.processing_purpose,
                idempotency_key=resolved_key,
                payload=payload,
            )
            metadata = context_repository.save(scope, kind, envelope)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
        except (ContractValidationError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _success(_metadata(metadata), resolved_request_id)

    def get_record(
        *,
        kind: RecordKind,
        record_id: str,
        authorization: str | None,
        site_id: str | None,
        purpose: str | None,
        request_id: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        scope, resolved_request_id, _resolved_key = _authorize(
            configured_token=local_token,
            authorization=authorization,
            site_id=site_id,
            purpose=purpose,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        metadata = context_repository.get(scope, kind, record_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="record not found")
        return _success(_metadata(metadata), resolved_request_id)

    @application.post("/internal/v1/context/evidence-records")
    def create_evidence_record(
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
        x_processing_purpose: Annotated[str | None, Header(alias="X-Processing-Purpose")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return save_record(
            kind=RecordKind.EVIDENCE,
            payload=payload,
            authorization=authorization,
            site_id=x_site_id,
            purpose=x_processing_purpose,
            request_id=x_request_id,
            idempotency_key=idempotency_key,
        )

    @application.post("/internal/v1/context/fact-proposals")
    def create_fact_proposal(
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
        x_processing_purpose: Annotated[str | None, Header(alias="X-Processing-Purpose")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return save_record(
            kind=RecordKind.FACT_PROPOSAL,
            payload=payload,
            authorization=authorization,
            site_id=x_site_id,
            purpose=x_processing_purpose,
            request_id=x_request_id,
            idempotency_key=idempotency_key,
        )

    @application.post("/internal/v1/context/entity-resolution-proposals")
    def create_entity_resolution_proposal(
        payload: Annotated[dict[str, Any], Body()],
        authorization: Annotated[str | None, Header()] = None,
        x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
        x_processing_purpose: Annotated[str | None, Header(alias="X-Processing-Purpose")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return save_record(
            kind=RecordKind.ENTITY_RESOLUTION_PROPOSAL,
            payload=payload,
            authorization=authorization,
            site_id=x_site_id,
            purpose=x_processing_purpose,
            request_id=x_request_id,
            idempotency_key=idempotency_key,
        )

    def register_get(path: str, kind: RecordKind, parameter_name: str) -> None:
        def endpoint(
            record_id: str,
            authorization: Annotated[str | None, Header()] = None,
            x_site_id: Annotated[str | None, Header(alias="X-Site-ID")] = None,
            x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
            x_processing_purpose: Annotated[
                str | None, Header(alias="X-Processing-Purpose")
            ] = None,
            idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        ) -> dict[str, Any]:
            return get_record(
                kind=kind,
                record_id=record_id,
                authorization=authorization,
                site_id=x_site_id,
                purpose=x_processing_purpose,
                request_id=x_request_id,
                idempotency_key=idempotency_key,
            )

        endpoint.__name__ = f"get_{kind.value}"
        endpoint.__annotations__["record_id"] = str
        application.get(path.replace(f"{{{parameter_name}}}", "{record_id}"))(endpoint)

    register_get(
        "/v1/context/evidence-records/{evidence_record_id}",
        RecordKind.EVIDENCE,
        "evidence_record_id",
    )
    register_get(
        "/v1/context/fact-proposals/{fact_proposal_record_id}",
        RecordKind.FACT_PROPOSAL,
        "fact_proposal_record_id",
    )
    register_get(
        "/v1/context/entity-resolution-proposals/{entity_resolution_proposal_id}",
        RecordKind.ENTITY_RESOLUTION_PROPOSAL,
        "entity_resolution_proposal_id",
    )
    return application


def _repository_from_environment() -> ContextRepository:
    if os.getenv("GBOS_CONTEXT_DATABASE_ENABLED", "false").lower() not in {
        "true",
        "1",
        "yes",
    }:
        return InMemoryContextRepository()
    if os.getenv("GBOS_PRODUCTION_ENABLED", "false").lower() in {"true", "1", "yes"}:
        raise RuntimeError("Gate 3 local Context runtime refuses production mode")
    connection = connect_postgres_components(
        host=os.environ["GBOS_CONTEXT_DATABASE_HOST"],
        port=int(os.environ["GBOS_CONTEXT_DATABASE_PORT"]),
        database=os.environ["GBOS_CONTEXT_DATABASE_NAME"],
        user=os.environ["GBOS_CONTEXT_DATABASE_USER"],
        password=os.environ["GBOS_CONTEXT_DATABASE_PASSWORD"],
    )
    return PostgresContextRepository(connection)


app = create_context_app(
    repository=_repository_from_environment(),
    local_token=os.getenv("GBOS_CONTEXT_LOCAL_TOKEN"),
)
