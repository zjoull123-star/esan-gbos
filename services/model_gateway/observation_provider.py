from __future__ import annotations

import re
from typing import Protocol

from services.observer.observer.model_projection import (
    CommunicationIntelligenceResponse,
    ObservationModelRequest,
)

from .deepseek import (
    DEEPSEEK_MODEL,
    GatewayFailure,
    GatewayResult,
    TokenizedModelRequest,
)

COMMUNICATION_PROMPT_VERSION = "communication-intelligence-local-pilot-v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class _ObservationGateway(Protocol):
    def invoke(self, request: TokenizedModelRequest) -> GatewayResult: ...


class DeepSeekObservationProvider:
    """Bridge locally tokenized observations into the governed DeepSeek gateway."""

    def __init__(self, *, gateway: _ObservationGateway) -> None:
        self._gateway = gateway

    def __repr__(self) -> str:
        return "DeepSeekObservationProvider(gateway=<redacted>)"

    def project(
        self,
        request: ObservationModelRequest,
    ) -> CommunicationIntelligenceResponse:
        self._validate_request(request)
        assert request.tokenizer_version is not None
        assert request.mapping_digest is not None

        try:
            model_request = TokenizedModelRequest(
                request_id=request.idempotency_key,
                site_id=request.site_id,
                purpose=request.processing_purpose,
                agent_kind="communication",
                subject_ref=request.observation_id,
                evidence_refs=request.evidence_refs,
                prompt_version=COMMUNICATION_PROMPT_VERSION,
                tokenized_context=request.input_text,
                tokenization_receipt_id=request.tokenization_refs[0],
                tokenizer_version=request.tokenizer_version,
                mapping_digest=request.mapping_digest,
                complex_multi_entity=(
                    len(request.participant_refs) > 2 or len(request.evidence_refs) > 4
                ),
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise GatewayFailure(
                "observation model request failed closed validation",
                error_code="request_binding_failed",
            ) from exc

        result = self._gateway.invoke(model_request)
        if result.observed_model != DEEPSEEK_MODEL:
            raise GatewayFailure(
                "model response identity did not match the approved model",
                error_code="model_mismatch",
                observed_model=result.observed_model,
            )

        invocation_refs = tuple(record.invocation_id for record in result.invocations)
        if (
            not invocation_refs
            or len(invocation_refs) != len(set(invocation_refs))
            or any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 256
                for value in invocation_refs
            )
        ):
            raise GatewayFailure(
                "model response audit identity was invalid",
                error_code="internal_error",
            )

        return CommunicationIntelligenceResponse(
            output=result.output,
            model_name=DEEPSEEK_MODEL,
            model_version=result.observed_model,
            invocation_refs=invocation_refs,
        )

    @staticmethod
    def _validate_request(request: ObservationModelRequest) -> None:
        if (
            request.input_mode != "local_tokenized"
            or request.output_schema_version != "1.0"
            or not isinstance(request.processing_purpose, str)
            or not request.processing_purpose
            or request.processing_purpose != request.processing_purpose.strip()
            or len(request.tokenization_refs) != 1
            or not isinstance(request.tokenization_refs[0], str)
            or not request.tokenization_refs[0]
            or request.tokenization_refs[0] != request.tokenization_refs[0].strip()
            or not isinstance(request.tokenizer_version, str)
            or not request.tokenizer_version
            or request.tokenizer_version != request.tokenizer_version.strip()
            or not isinstance(request.mapping_digest, str)
            or _SHA256.fullmatch(request.mapping_digest) is None
        ):
            raise GatewayFailure(
                "observation input was not valid locally tokenized content",
                error_code="request_binding_failed",
            )
