from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .evidence_store import ContentAddressedEvidenceStore
from .model_fatal_latch import (
    InMemoryModelFatalLatch,
    ModelFatalLatch,
    ModelFatalLatchError,
    is_fatal_model_error_code,
    sanitized_provider_error_code,
)
from .models import PROCESSING_PURPOSES, TenantScope, _require_aware
from .read_service import CommunicationDetail, CommunicationSummary
from .storage import Connection

_APPROVED_MODEL = "deepseek-v4-flash"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SCHEMA_PATH = (
    Path(__file__).parents[3]
    / "contracts"
    / "local_pilot"
    / "communication-intelligence-v1.0.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
Draft202012Validator.check_schema(_SCHEMA)
_VALIDATOR = Draft202012Validator(_SCHEMA)

RestrictedInputPolicy = Literal["deny", "local_tokenized"]
InputMode = Literal["raw", "local_tokenized"]


class ProjectionFailure(RuntimeError):
    """Safe failure whose code never contains raw or provider-controlled content."""

    __slots__ = ("code", "fatal_code")

    def __init__(self, code: str, *, fatal_code: str | None = None) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid projection failure code")
        if fatal_code is not None and not is_fatal_model_error_code(fatal_code):
            raise ValueError("invalid fatal projection failure code")
        self.code = code
        self.fatal_code = fatal_code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ProjectionFailure(code={self.code!r}, fatal_code={self.fatal_code!r})"


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceLineage:
    evidence_ref: str
    content_object_ref: str
    media_type: str
    raw_sha256: str

    def __post_init__(self) -> None:
        for value, name, maximum in (
            (self.evidence_ref, "evidence_ref", 512),
            (self.content_object_ref, "content_object_ref", 512),
            (self.media_type, "media_type", 255),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > maximum
            ):
                raise ValueError(f"invalid {name}")
        if _SHA256.fullmatch(self.raw_sha256) is None:
            raise ValueError("invalid raw_sha256")

    def __repr__(self) -> str:
        return (
            "EvidenceLineage("
            "evidence_ref=<redacted>, content_object_ref=<redacted>, "
            f"media_type={self.media_type!r}, raw_sha256=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LoadedEvidenceText:
    """Bounded model text proven to belong to one evidence lineage item."""

    evidence_ref: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_ref, str)
            or not self.evidence_ref
            or self.evidence_ref != self.evidence_ref.strip()
            or len(self.evidence_ref) > 512
            or not isinstance(self.text, str)
            or not self.text
            or len(self.text) > 1_000_000
        ):
            raise ValueError("invalid loaded evidence text")

    def __repr__(self) -> str:
        return "LoadedEvidenceText(evidence_ref=<redacted>, text=<redacted>)"


class _SafeHTMLTextParser(HTMLParser):
    __slots__ = ("_blocked_depth", "_parts")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocked_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._blocked_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._blocked_depth = max(0, self._blocked_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._blocked_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


class ContentAddressedEvidenceTextLoader:
    """Read hash-bound CAS bytes and expose only bounded, explicitly safe text media."""

    __slots__ = ("_max_mime_parts", "_max_object_bytes", "_max_text_characters", "_store")

    _JSON_TEXT_FIELDS = ("subject", "body", "text", "message", "content")

    def __init__(
        self,
        store: ContentAddressedEvidenceStore,
        *,
        max_object_bytes: int = 2_000_000,
        max_text_characters: int = 1_000_000,
        max_mime_parts: int = 100,
    ) -> None:
        if (
            not isinstance(store, ContentAddressedEvidenceStore)
            or not 1 <= max_object_bytes <= 10_000_000
            or not 1 <= max_text_characters <= 1_000_000
            or not 1 <= max_mime_parts <= 1_000
        ):
            raise ValueError("invalid evidence text loader configuration")
        self._store = store
        self._max_object_bytes = max_object_bytes
        self._max_text_characters = max_text_characters
        self._max_mime_parts = max_mime_parts

    def __repr__(self) -> str:
        return "ContentAddressedEvidenceTextLoader(store=<redacted>)"

    def __call__(
        self,
        scope: TenantScope,
        evidence: EvidenceLineage,
    ) -> LoadedEvidenceText | None:
        if not isinstance(evidence, EvidenceLineage):
            raise TypeError("evidence lineage is required")
        content = self._store.read(scope, evidence.content_object_ref)
        if len(content) > self._max_object_bytes:
            raise ValueError("evidence object is outside the bounded model input")
        actual_digest = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_digest, evidence.raw_sha256):
            raise ValueError("evidence digest does not match lineage")
        media_type = evidence.media_type.partition(";")[0].strip().lower()
        if media_type == "text/plain":
            text = self._decode_text(content, charset="utf-8")
        elif media_type == "text/html":
            parser = _SafeHTMLTextParser()
            parser.feed(self._decode_text(content, charset="utf-8"))
            parser.close()
            text = parser.text()
        elif media_type == "message/rfc822":
            text = self._email_text(content)
        elif media_type == "application/json":
            text = self._json_text(content)
        else:
            return None
        text = text.strip()
        if not text:
            return None
        if len(text) > self._max_text_characters:
            raise ValueError("evidence text is outside the bounded model input")
        return LoadedEvidenceText(evidence_ref=evidence.evidence_ref, text=text)

    @staticmethod
    def _decode_text(content: bytes, *, charset: str) -> str:
        normalized = charset.lower()
        if normalized not in {"utf-8", "us-ascii", "ascii"}:
            raise ValueError("evidence text charset is not allowed")
        try:
            text = content.decode("utf-8" if normalized == "utf-8" else "ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("evidence text is not valid declared text") from exc
        if "\x00" in text:
            raise ValueError("evidence text contains forbidden control bytes")
        return text

    def _email_text(self, content: bytes) -> str:
        try:
            message = BytesParser(policy=policy.default).parsebytes(content)
        except Exception as exc:
            raise ValueError("email evidence failed strict parsing") from exc
        if message.defects:
            raise ValueError("email evidence failed strict parsing")
        parts = tuple(message.walk())
        if len(parts) > self._max_mime_parts:
            raise ValueError("email evidence is a complex MIME message")
        values: list[str] = []
        total = 0
        for part in parts:
            if part.get_content_type().lower() == "message/rfc822":
                raise ValueError("email evidence is a complex MIME message")
            if part.is_multipart():
                continue
            if (
                part.get_content_type().lower() != "text/plain"
                or part.get_content_disposition() == "attachment"
                or part.get_filename() is not None
            ):
                continue
            decoded = part.get_payload(decode=True)
            if not isinstance(decoded, bytes):
                raise ValueError("email evidence failed strict parsing")
            total += len(decoded)
            if total > self._max_object_bytes:
                raise ValueError("email evidence is outside the bounded model input")
            charset = part.get_content_charset() or "us-ascii"
            values.append(self._decode_text(decoded, charset=charset))
        return "\n\n".join(values)

    def _json_text(self, content: bytes) -> str:
        try:
            value = json.loads(self._decode_text(content, charset="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON evidence failed strict parsing") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON evidence must be an object with controlled text fields")
        values: list[str] = []
        for field_name in self._JSON_TEXT_FIELDS:
            field_value = value.get(field_name)
            if field_value is None:
                continue
            if not isinstance(field_value, str):
                raise ValueError("JSON evidence controlled text field is invalid")
            values.append(field_value)
        if not values:
            raise ValueError("JSON evidence has no controlled text fields")
        return "\n\n".join(values)


@dataclass(frozen=True, slots=True, repr=False)
class ObservationProjectionSource:
    site_id: str
    observation_id: str
    channel: str
    occurred_at: datetime
    classification: str
    team_ref: str | None
    party_ref: str | None
    participant_refs: tuple[str, ...]
    evidence: tuple[EvidenceLineage, ...]

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")
        if self.classification not in {
            "Public",
            "Internal",
            "Confidential",
            "Restricted",
        }:
            raise ValueError("invalid classification")
        if (
            not self.site_id
            or not self.observation_id
            or not self.channel
            or not isinstance(self.participant_refs, tuple)
            or not isinstance(self.evidence, tuple)
            or not self.evidence
        ):
            raise ValueError("invalid observation projection source")
        if len(self.evidence) > 100:
            raise ValueError("observation evidence is outside the model budget")
        evidence_refs = tuple(item.evidence_ref for item in self.evidence)
        if len(evidence_refs) != len(set(evidence_refs)):
            raise ValueError("duplicate observation evidence")
        if any(
            not value or value != value.strip() or len(value) > 256
            for value in self.participant_refs
        ):
            raise ValueError("invalid participant reference")

    def __repr__(self) -> str:
        return (
            "ObservationProjectionSource("
            "identity=<redacted>, participants=<redacted>, evidence=<redacted>, "
            f"classification={self.classification!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LocalTokenizationResult:
    text: str = field(repr=False)
    receipt_ref: str
    tokenizer_version: str
    mapping_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > 1_000_000
            or not self.receipt_ref
            or self.receipt_ref != self.receipt_ref.strip()
            or len(self.receipt_ref) > 256
            or not self.tokenizer_version
            or self.tokenizer_version != self.tokenizer_version.strip()
            or len(self.tokenizer_version) > 80
        ):
            raise ValueError("invalid local tokenization result")
        if _SHA256.fullmatch(self.mapping_digest) is None:
            raise ValueError("invalid tokenization mapping digest")

    def __repr__(self) -> str:
        return (
            "LocalTokenizationResult("
            "text=<redacted>, receipt_ref=<redacted>, "
            "mapping_digest=<redacted>, "
            f"tokenizer_version={self.tokenizer_version!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ObservationModelRequest:
    site_id: str
    processing_purpose: str
    observation_id: str
    channel: str
    classification: str
    occurred_at: datetime
    input_mode: InputMode
    input_text: str = field(repr=False)
    evidence_refs: tuple[str, ...]
    participant_refs: tuple[str, ...]
    tokenization_refs: tuple[str, ...]
    tokenizer_version: str | None
    mapping_digest: str | None
    idempotency_key: str
    output_schema_version: Literal["1.0"] = "1.0"

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")
        if self.processing_purpose not in PROCESSING_PURPOSES:
            raise ValueError("invalid observation processing purpose")
        if (
            self.input_mode not in {"raw", "local_tokenized"}
            or not self.input_text
            or len(self.input_text) > 1_000_000
            or not self.evidence_refs
            or len(self.evidence_refs) != len(set(self.evidence_refs))
            or not self.idempotency_key
            or len(self.idempotency_key) > 256
        ):
            raise ValueError("invalid observation model request")
        if self.input_mode == "local_tokenized":
            if (
                len(self.tokenization_refs) != 1
                or not self.tokenization_refs[0]
                or self.tokenization_refs[0] != self.tokenization_refs[0].strip()
                or len(self.tokenization_refs[0]) > 256
                or not self.tokenizer_version
                or self.tokenizer_version != self.tokenizer_version.strip()
                or len(self.tokenizer_version) > 80
                or self.mapping_digest is None
                or _SHA256.fullmatch(self.mapping_digest) is None
            ):
                raise ValueError("tokenized model input requires a receipt")
        elif (
            self.tokenization_refs
            or self.tokenizer_version is not None
            or self.mapping_digest is not None
        ):
            raise ValueError("raw model input cannot claim tokenization")

    def __repr__(self) -> str:
        return (
            "ObservationModelRequest("
            "identity=<redacted>, input_text=<redacted>, refs=<redacted>, "
            f"input_mode={self.input_mode!r}, output_schema_version='1.0')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CommunicationIntelligenceResponse:
    output: Mapping[str, Any] = field(repr=False)
    model_name: str
    model_version: str
    invocation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.output, Mapping):
            raise TypeError("model output must be a mapping")
        if (
            not self.model_name
            or self.model_name != self.model_name.strip()
            or len(self.model_name) > 160
            or not self.model_version
            or self.model_version != self.model_version.strip()
            or len(self.model_version) > 160
            or not isinstance(self.invocation_refs, tuple)
            or not self.invocation_refs
            or len(self.invocation_refs) != len(set(self.invocation_refs))
            or any(
                not value or value != value.strip() or len(value) > 256
                for value in self.invocation_refs
            )
        ):
            raise ValueError("invalid communication intelligence response")

    def __repr__(self) -> str:
        return (
            "CommunicationIntelligenceResponse("
            "output=<redacted>, model=<redacted>, invocation_refs=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ContextIntelligencePublication:
    site_id: str
    observation_id: str
    team_ref: str | None
    evidence_refs: tuple[str, ...]
    summary_zh: str
    original_language: str
    confidence: float
    review_status: Literal["AI Draft"]
    fact_proposals: tuple[dict[str, Any], ...]
    association_suggestions: tuple[dict[str, Any], ...]
    model: dict[str, str]
    invocation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.site_id
            or self.site_id != self.site_id.strip()
            or len(self.site_id) > 140
            or not self.observation_id
            or self.observation_id != self.observation_id.strip()
            or len(self.observation_id) > 256
        ):
            raise ValueError("invalid Context site binding")
        if self.team_ref is not None and (
            not isinstance(self.team_ref, str)
            or not self.team_ref
            or self.team_ref != self.team_ref.strip()
            or len(self.team_ref) > 256
            or any(character in self.team_ref for character in ("\r", "\n", "\x00"))
        ):
            raise ValueError("invalid Context team binding")
        if (
            not isinstance(self.evidence_refs, tuple)
            or not self.evidence_refs
            or len(self.evidence_refs) > 100
            or len(self.evidence_refs) != len(set(self.evidence_refs))
            or any(
                not value or value != value.strip() or len(value) > 512
                for value in self.evidence_refs
            )
        ):
            raise ValueError("invalid Context evidence binding")
        if (
            not self.summary_zh
            or len(self.summary_zh) > 2_000
            or not self.original_language
            or len(self.original_language) > 35
            or isinstance(self.confidence, bool)
            or not isinstance(self.confidence, int | float)
            or not 0 <= self.confidence <= 1
            or self.review_status != "AI Draft"
        ):
            raise ValueError("invalid Context AI Draft projection")
        if (
            not isinstance(self.fact_proposals, tuple)
            or not self.fact_proposals
            or len(self.fact_proposals) > 100
            or any(
                not isinstance(proposal, dict) or proposal.get("status") != "proposed"
                for proposal in self.fact_proposals
            )
        ):
            raise ValueError("Context facts must remain proposed")
        if (
            not isinstance(self.association_suggestions, tuple)
            or len(self.association_suggestions) > 100
        ):
            raise ValueError("invalid Context association suggestions")
        if (
            not isinstance(self.model, dict)
            or set(self.model) != {"name", "version"}
            or self.model.get("name") != _APPROVED_MODEL
            or not self.model.get("version")
        ):
            raise ValueError("invalid Context model binding")
        if (
            not isinstance(self.invocation_refs, tuple)
            or not self.invocation_refs
            or len(self.invocation_refs) != len(set(self.invocation_refs))
            or any(
                not value or value != value.strip() or len(value) > 256
                for value in self.invocation_refs
            )
        ):
            raise ValueError("invalid Context invocation binding")

    def __repr__(self) -> str:
        return (
            "ContextIntelligencePublication("
            "observation=<redacted>, evidence=<redacted>, proposals=<redacted>, "
            "model=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ProjectionPublicationResult:
    observation_id: str
    status: Literal["projected"]
    model_version: str


class ObservationProjectionRepository(Protocol):
    def load_projection_source(
        self,
        scope: TenantScope,
        observation_id: str,
    ) -> ObservationProjectionSource: ...

    def store_projection(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        projected_at: datetime,
    ) -> None: ...


class ProjectionStore(Protocol):
    def store_projection(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        projected_at: datetime,
    ) -> None: ...


class ObservationModelProvider(Protocol):
    def project(
        self,
        request: ObservationModelRequest,
    ) -> CommunicationIntelligenceResponse: ...


class ContextIntelligencePublisher(Protocol):
    def publish(
        self,
        scope: TenantScope,
        publication: ContextIntelligencePublication,
        *,
        idempotency_key: str,
    ) -> None: ...


RawObservationLoader = Callable[
    [TenantScope, EvidenceLineage],
    LoadedEvidenceText | None,
]
LocalPiiTokenizer = Callable[
    [TenantScope, str, str],
    LocalTokenizationResult,
]
Clock = Callable[[], datetime]


class ObservationProjectionPublisher:
    """Outbox callback for normalized observation → model → Context → BFF."""

    __slots__ = (
        "_clock",
        "_context_publisher",
        "_fatal_latch",
        "_provider",
        "_raw_loader",
        "_repository",
        "_restricted_policy",
        "_tokenizer",
    )

    def __init__(
        self,
        *,
        repository: ObservationProjectionRepository,
        raw_loader: RawObservationLoader | None,
        tokenizer: LocalPiiTokenizer | None,
        provider: ObservationModelProvider | None,
        context_publisher: ContextIntelligencePublisher,
        clock: Clock,
        restricted_policy: RestrictedInputPolicy = "deny",
        fatal_latch: ModelFatalLatch | None = None,
    ) -> None:
        if raw_loader is not None and not callable(raw_loader):
            raise TypeError("raw_loader must be callable")
        if tokenizer is not None and not callable(tokenizer):
            raise TypeError("tokenizer must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if restricted_policy not in {"deny", "local_tokenized"}:
            raise ValueError("invalid Restricted input policy")
        self._repository = repository
        self._raw_loader = raw_loader
        self._tokenizer = tokenizer
        self._provider = provider
        self._context_publisher = context_publisher
        self._clock = clock
        self._restricted_policy = restricted_policy
        self._fatal_latch = fatal_latch or InMemoryModelFatalLatch()

    def __repr__(self) -> str:
        return (
            "ObservationProjectionPublisher("
            "repository=<redacted>, raw_loader=<redacted>, tokenizer=<redacted>, "
            "provider=<redacted>, context_publisher=<redacted>)"
        )

    def __call__(
        self,
        scope: TenantScope,
        observation_id: str,
        idempotency_key: str,
    ) -> ProjectionPublicationResult:
        if self._provider is None:
            raise ProjectionFailure("provider_unconfigured")
        self._assert_model_egress_open(scope)
        source = self._load_source(scope, observation_id)
        request = self._request(scope, source, idempotency_key)
        response, output = self._project_under_fatal_guard(scope, source, request)
        publication = _context_publication(source, output, response)
        try:
            self._context_publisher.publish(
                scope,
                publication,
                idempotency_key=idempotency_key,
            )
        except Exception:
            raise ProjectionFailure("context_publication_failed") from None
        detail = _communication_detail(source, output, response)
        projected_at = self._clock()
        _require_aware(projected_at, "clock")
        try:
            self._repository.store_projection(
                scope,
                detail,
                projected_at=projected_at,
            )
        except Exception:
            raise ProjectionFailure("projection_store_failed") from None
        return ProjectionPublicationResult(
            observation_id=source.observation_id,
            status="projected",
            model_version=response.model_version,
        )

    def _project_under_fatal_guard(
        self,
        scope: TenantScope,
        source: ObservationProjectionSource,
        request: ObservationModelRequest,
    ) -> tuple[CommunicationIntelligenceResponse, dict[str, Any]]:
        try:
            with self._fatal_latch.egress_guard(scope):
                return self._project_and_validate(scope, source, request)
        except ProjectionFailure:
            raise
        except ModelFatalLatchError:
            raise ProjectionFailure("model_fatal_latched") from None
        except Exception:
            raise ProjectionFailure("model_fatal_latch_unavailable") from None

    def _project_and_validate(
        self,
        scope: TenantScope,
        source: ObservationProjectionSource,
        request: ObservationModelRequest,
    ) -> tuple[CommunicationIntelligenceResponse, dict[str, Any]]:
        assert self._provider is not None
        try:
            response = self._provider.project(request)
        except Exception as error:
            code = sanitized_provider_error_code(error)
            if is_fatal_model_error_code(code):
                self._trip_fatal(scope, code)
            raise ProjectionFailure(code) from None
        if not isinstance(response, CommunicationIntelligenceResponse):
            self._trip_fatal(scope, "invalid_model_output")
        if response.model_name != _APPROVED_MODEL:
            self._trip_fatal(scope, "model_mismatch")
        try:
            output = _validated_output(response.output)
            _validate_binding(source, output, evidence_refs=request.evidence_refs)
        except ProjectionFailure as error:
            if is_fatal_model_error_code(error.code):
                self._trip_fatal(scope, error.code)
            raise
        return response, output

    def _assert_model_egress_open(self, scope: TenantScope) -> None:
        try:
            self._fatal_latch.assert_open(scope)
        except ModelFatalLatchError:
            raise ProjectionFailure("model_fatal_latched") from None
        except Exception:
            raise ProjectionFailure("model_fatal_latch_unavailable") from None

    def _trip_fatal(self, scope: TenantScope, error_code: str) -> None:
        try:
            self._fatal_latch.trip(
                scope,
                error_code=error_code,
                now=self._clock(),
            )
        except Exception:
            raise ProjectionFailure(
                "model_fatal_latch_unavailable",
                fatal_code=error_code,
            ) from None
        raise ProjectionFailure(error_code)

    def _load_source(
        self,
        scope: TenantScope,
        observation_id: str,
    ) -> ObservationProjectionSource:
        try:
            source = self._repository.load_projection_source(scope, observation_id)
        except Exception:
            raise ProjectionFailure("observation_source_unavailable") from None
        if source.site_id != scope.site_id or source.observation_id != observation_id:
            raise ProjectionFailure("observation_binding_mismatch")
        return source

    def _request(
        self,
        scope: TenantScope,
        source: ObservationProjectionSource,
        idempotency_key: str,
    ) -> ObservationModelRequest:
        if source.classification == "Restricted" and self._restricted_policy != "local_tokenized":
            raise ProjectionFailure("restricted_input_denied")
        if self._tokenizer is None:
            raise ProjectionFailure("pii_tokenization_failed")
        if self._raw_loader is None:
            raise ProjectionFailure("raw_loader_unconfigured")
        raw_parts: list[str] = []
        evidence_refs: list[str] = []
        raw_size = 0
        try:
            for evidence in source.evidence:
                loaded = self._raw_loader(scope, evidence)
                if loaded is None:
                    continue
                if (
                    not isinstance(loaded, LoadedEvidenceText)
                    or loaded.evidence_ref != evidence.evidence_ref
                ):
                    raise ValueError
                raw_size += len(loaded.text)
                if raw_size > 1_000_000:
                    raise ValueError
                raw_parts.append(loaded.text)
                evidence_refs.append(evidence.evidence_ref)
        except Exception:
            raise ProjectionFailure("raw_input_unavailable") from None
        if not raw_parts:
            raise ProjectionFailure("raw_input_unavailable")
        raw_text = "\n\n".join(raw_parts)
        try:
            tokenized = self._tokenizer(
                scope,
                source.observation_id,
                raw_text,
            )
        except Exception:
            raise ProjectionFailure("pii_tokenization_failed") from None
        if not isinstance(tokenized, LocalTokenizationResult):
            raise ProjectionFailure("pii_tokenization_failed")
        return ObservationModelRequest(
            site_id=scope.site_id,
            processing_purpose=scope.processing_purpose,
            observation_id=source.observation_id,
            channel=source.channel,
            classification=source.classification,
            occurred_at=source.occurred_at,
            input_mode="local_tokenized",
            input_text=tokenized.text,
            evidence_refs=tuple(evidence_refs),
            participant_refs=source.participant_refs,
            tokenization_refs=(tokenized.receipt_ref,),
            tokenizer_version=tokenized.tokenizer_version,
            mapping_digest=tokenized.mapping_digest,
            idempotency_key=idempotency_key,
        )


class PostgresObservationProjectionRepository:
    """RLS-scoped 005 lineage loader delegating 006 projection upserts."""

    __slots__ = ("_connection", "_projection_store", "_raw_loader")

    def __init__(
        self,
        *,
        connection: Connection,
        projection_store: ProjectionStore,
        raw_loader: Callable[..., object],
    ) -> None:
        if not callable(raw_loader):
            raise TypeError("raw_loader must be callable")
        self._connection = connection
        self._projection_store = projection_store
        self._raw_loader = cast(RawObservationLoader, raw_loader)

    def __repr__(self) -> str:
        return (
            "PostgresObservationProjectionRepository("
            "connection=<redacted>, projection_store=<redacted>, "
            "raw_loader=<redacted>)"
        )

    @property
    def raw_loader(self) -> RawObservationLoader:
        return self._raw_loader

    def load_projection_source(
        self,
        scope: TenantScope,
        observation_id: str,
    ) -> ObservationProjectionSource:
        with self._connection.transaction(), self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            cursor.execute(
                """
                SELECT event.site_id, event.event_id, event.channel,
                       event.occurred_at, event.data_classification,
                       event.team_ref, event.party_ref,
                       ARRAY(
                         SELECT participant.identity_ref
                         FROM observer.participants AS participant
                         WHERE participant.site_id = event.site_id
                           AND participant.event_id = event.event_id
                         ORDER BY participant.identity_ref ASC
                       )
                FROM observer.observation_events AS event
                WHERE event.site_id = %s
                  AND event.processing_purpose = %s
                  AND event.event_id = %s
                  AND (
                    event.retention_until IS NULL
                    OR event.retention_until > current_timestamp
                  )
                """,
                (
                    scope.site_id,
                    scope.processing_purpose,
                    observation_id,
                ),
            )
            event = cursor.fetchone()
            if event is None:
                raise LookupError("observation is unavailable")
            cursor.execute(
                """
                SELECT evidence.evidence_id, evidence.content_object_ref,
                       evidence.media_type, evidence.raw_sha256
                FROM observer.event_evidence AS edge
                JOIN observer.evidence_refs AS evidence
                  ON evidence.site_id = edge.site_id
                 AND evidence.evidence_id = edge.evidence_id
                WHERE edge.site_id = %s AND edge.event_id = %s
                ORDER BY edge.evidence_ordinal ASC, edge.evidence_id ASC
                LIMIT 101
                """,
                (scope.site_id, observation_id),
            )
            evidence_rows = cursor.fetchall()
        if not evidence_rows or len(evidence_rows) > 100:
            raise LookupError("observation evidence is unavailable or unbounded")
        if any(row[1] is None for row in evidence_rows):
            raise LookupError("observation evidence content is unavailable")
        return ObservationProjectionSource(
            site_id=str(event[0]),
            observation_id=str(event[1]),
            channel=str(event[2]),
            occurred_at=event[3],
            classification=str(event[4]),
            team_ref=None if event[5] is None else str(event[5]),
            party_ref=None if event[6] is None else str(event[6]),
            participant_refs=tuple(str(value) for value in (event[7] or ())),
            evidence=tuple(
                EvidenceLineage(
                    evidence_ref=str(row[0]),
                    content_object_ref=str(row[1]),
                    media_type=str(row[2]),
                    raw_sha256=str(row[3]),
                )
                for row in evidence_rows
            ),
        )

    def store_projection(
        self,
        scope: TenantScope,
        detail: CommunicationDetail,
        *,
        projected_at: datetime,
    ) -> None:
        self._projection_store.store_projection(
            scope,
            detail,
            projected_at=projected_at,
        )


def _validated_output(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        _VALIDATOR.validate(copied)
    except TypeError, ValueError, ValidationError:
        raise ProjectionFailure("invalid_model_output") from None
    if not isinstance(copied, dict):
        raise ProjectionFailure("invalid_model_output")
    return copied


def _validate_binding(
    source: ObservationProjectionSource,
    output: dict[str, Any],
    *,
    evidence_refs: tuple[str, ...],
) -> None:
    source_evidence_refs = {item.evidence_ref for item in source.evidence}
    if (
        output["site_id"] != source.site_id
        or output["observation_id"] != source.observation_id
        or tuple(output["evidence_refs"]) != evidence_refs
        or not set(evidence_refs).issubset(source_evidence_refs)
    ):
        raise ProjectionFailure("model_binding_mismatch")
    allowed = set(evidence_refs)
    nested = (
        *output["fact_proposals"],
        *output["association_suggestions"],
    )
    if any(not set(item["evidence_refs"]).issubset(allowed) for item in nested):
        raise ProjectionFailure("model_binding_mismatch")


def _context_publication(
    source: ObservationProjectionSource,
    output: dict[str, Any],
    response: CommunicationIntelligenceResponse,
) -> ContextIntelligencePublication:
    return ContextIntelligencePublication(
        site_id=str(output["site_id"]),
        observation_id=str(output["observation_id"]),
        team_ref=source.team_ref,
        evidence_refs=tuple(str(value) for value in output["evidence_refs"]),
        summary_zh=str(output["summary_zh"]),
        original_language=str(output["original_language"]),
        confidence=float(output["confidence"]),
        review_status="AI Draft",
        fact_proposals=tuple(dict(value) for value in output["fact_proposals"]),
        association_suggestions=tuple(dict(value) for value in output["association_suggestions"]),
        model={
            "name": response.model_name,
            "version": response.model_version,
        },
        invocation_refs=response.invocation_refs,
    )


def _communication_detail(
    source: ObservationProjectionSource,
    output: dict[str, Any],
    response: CommunicationIntelligenceResponse,
) -> CommunicationDetail:
    return CommunicationDetail(
        summary=CommunicationSummary(
            observation_id=source.observation_id,
            channel=source.channel,
            occurred_at=source.occurred_at,
            summary_zh=str(output["summary_zh"]),
            original_language=str(output["original_language"]),
            classification=source.classification,
            review_status="AI Draft",
            team_ref=source.team_ref,
            party_ref=source.party_ref,
            evidence_count=len(source.evidence),
            actor_refs=frozenset(source.participant_refs),
        ),
        evidence=tuple(
            {
                "ref": evidence.evidence_ref,
                "locator": evidence.content_object_ref,
            }
            for evidence in source.evidence
        ),
        fact_proposals=tuple(
            {
                "status": str(value["status"]),
                "confidence": float(value["confidence"]),
                "type": str(value["type"]),
                "value_display": str(value["value_display"]),
            }
            for value in output["fact_proposals"]
        ),
        association_suggestions=tuple(
            {
                "type": str(value["type"]),
                "target_ref": str(value["target_ref"]),
                "confidence": float(value["confidence"]),
            }
            for value in output["association_suggestions"]
        ),
        model={
            "name": response.model_name,
            "version": response.model_version,
        },
        original_text=None,
    )


__all__ = [
    "CommunicationIntelligenceResponse",
    "ContextIntelligencePublication",
    "ContextIntelligencePublisher",
    "ContentAddressedEvidenceTextLoader",
    "EvidenceLineage",
    "LoadedEvidenceText",
    "LocalPiiTokenizer",
    "LocalTokenizationResult",
    "ObservationModelProvider",
    "ObservationModelRequest",
    "ObservationProjectionPublisher",
    "ObservationProjectionSource",
    "PostgresObservationProjectionRepository",
    "ProjectionFailure",
    "ProjectionPublicationResult",
]
