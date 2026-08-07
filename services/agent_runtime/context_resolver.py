"""Fail-closed local HTTP resolver for Context-to-Agent bundles."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .worker import ContextResolutionRequest, ResolvedAgentContext

_RESPONSE_FIELDS = frozenset({"schema_version", "request_id", "context"})
_CONTEXT_FIELDS = frozenset(
    {
        "schema_version",
        "site_id",
        "processing_purpose",
        "subject_type",
        "subject_ref",
        "decision_ref",
        "fact_version_refs",
        "evidence_refs",
        "facts",
    }
)
_FACT_REF_FIELDS = frozenset({"fact_id", "fact_version"})
_FACT_FIELDS = frozenset(
    {
        "fact_id",
        "fact_version",
        "predicate",
        "value",
        "valid_time",
        "recorded_time",
        "review_status",
    }
)


class ContextResolutionError(ValueError):
    """The local Context endpoint failed a transport or exact-ref invariant."""


@dataclass(frozen=True, slots=True)
class ContextEndpoint:
    base_url: str
    unix_socket: Path | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Context endpoint must be a closed HTTP origin")
        if self.unix_socket is not None:
            socket_path = Path(self.unix_socket)
            if not socket_path.is_absolute() or ".." in socket_path.parts:
                raise ValueError("Context unix socket must be an absolute normalized path")
            object.__setattr__(self, "unix_socket", socket_path)
            return
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("Context endpoint must use a literal loopback address") from exc
        if not address.is_loopback or parsed.port is None:
            raise ValueError("Context endpoint must use a literal loopback address and port")


@dataclass(frozen=True, slots=True)
class ContextBinding:
    processing_purpose: str
    decision_ref: str
    request_id: str

    def __post_init__(self) -> None:
        for name in ("processing_purpose", "decision_ref", "request_id"):
            value = getattr(self, name)
            if not value or len(value) > 256:
                raise ValueError(f"{name} must be non-empty and at most 256 characters")


BindingResolver = Callable[[ContextResolutionRequest], ContextBinding]


class HttpContextResolver:
    """Resolve through UDS or literal loopback without proxies or redirects."""

    def __init__(
        self,
        *,
        endpoint: ContextEndpoint,
        bearer_token: str,
        auth_ref: str,
        binding_resolver: BindingResolver,
        timeout_seconds: float = 3.0,
        max_body_bytes: int = 65_536,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not bearer_token or not auth_ref:
            raise ValueError("Context bearer token and auth_ref are required")
        if timeout_seconds <= 0 or max_body_bytes < 1:
            raise ValueError("Context timeout and body limit must be positive")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._auth_ref = auth_ref
        self._binding_resolver = binding_resolver
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_body_bytes = max_body_bytes
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"HttpContextResolver(endpoint={self._endpoint!r}, "
            "bearer_token=<redacted>, auth_ref=<redacted>)"
        )

    def resolve(self, request: ContextResolutionRequest) -> ResolvedAgentContext:
        binding = self._binding_resolver(request)
        document = _request_document(request, binding, auth_ref=self._auth_ref)
        transport = self._transport
        if transport is None:
            transport = httpx.HTTPTransport(
                uds=(
                    None if self._endpoint.unix_socket is None else str(self._endpoint.unix_socket)
                ),
                retries=0,
            )
        try:
            with (
                httpx.Client(
                    base_url=self._endpoint.base_url,
                    transport=transport,
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    "/internal/v1/agent-context",
                    json=document,
                    headers={
                        "Authorization": f"Bearer {self._bearer_token}",
                        "X-Auth-Ref": self._auth_ref,
                        "X-Site-ID": request.site_id,
                        "X-Processing-Purpose": binding.processing_purpose,
                        "X-Request-ID": binding.request_id,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise ContextResolutionError(f"Context returned status {response.status_code}")
                if response.headers.get("cache-control", "").casefold() != "no-store":
                    raise ContextResolutionError("Context response is missing no-store")
                body = _bounded_response_body(
                    response,
                    max_body_bytes=self._max_body_bytes,
                )
        except ContextResolutionError:
            raise
        except httpx.HTTPError as exc:
            raise ContextResolutionError("Context transport failed closed") from exc
        payload = _closed_response(body)
        context = payload["context"]
        assert isinstance(context, dict)
        _validate_exact_response(request, binding, payload, context)
        return ResolvedAgentContext(
            site_id=request.site_id,
            subject_type=request.subject_type,
            subject_ref=request.subject_ref,
            evidence_refs=request.evidence_refs,
            fact_version_refs=request.fact_version_refs,
            raw_context=json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


def _request_document(
    request: ContextResolutionRequest,
    binding: ContextBinding,
    *,
    auth_ref: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "auth_ref": auth_ref,
        "request_id": binding.request_id,
        "site_id": request.site_id,
        "processing_purpose": binding.processing_purpose,
        "subject_type": request.subject_type,
        "subject_ref": request.subject_ref,
        "decision_ref": binding.decision_ref,
        "fact_version_refs": [
            {"fact_id": item.fact_id, "fact_version": item.fact_version}
            for item in request.fact_version_refs
        ],
        "evidence_refs": list(request.evidence_refs),
    }


def _bounded_response_body(response: httpx.Response, *, max_body_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise ContextResolutionError("Context returned invalid content length") from exc
        if declared_size < 0 or declared_size > max_body_bytes:
            raise ContextResolutionError("Context response is too large")
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > max_body_bytes:
            raise ContextResolutionError("Context response is too large")
    return bytes(body)


def _closed_response(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextResolutionError("Context response is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _RESPONSE_FIELDS
        or payload.get("schema_version") != "1.0"
        or not isinstance(payload.get("context"), dict)
    ):
        raise ContextResolutionError("Context response is not a closed schema")
    context = payload["context"]
    assert isinstance(context, dict)
    if set(context) != _CONTEXT_FIELDS or context.get("schema_version") != "1.0":
        raise ContextResolutionError("Context bundle is not a closed schema")
    facts = context.get("facts")
    if not isinstance(facts, list) or not all(
        isinstance(fact, dict) and set(fact) == _FACT_FIELDS for fact in facts
    ):
        raise ContextResolutionError("Context facts are not a closed schema")
    return payload


def _validate_exact_response(
    request: ContextResolutionRequest,
    binding: ContextBinding,
    payload: dict[str, object],
    context: dict[str, object],
) -> None:
    expected_refs = [
        {"fact_id": item.fact_id, "fact_version": item.fact_version}
        for item in request.fact_version_refs
    ]
    if (
        payload.get("request_id") != binding.request_id
        or context.get("site_id") != request.site_id
        or context.get("processing_purpose") != binding.processing_purpose
        or context.get("subject_type") != request.subject_type
        or context.get("subject_ref") != request.subject_ref
        or context.get("decision_ref") != binding.decision_ref
        or context.get("fact_version_refs") != expected_refs
        or context.get("evidence_refs") != list(request.evidence_refs)
    ):
        raise ContextResolutionError("Context response binding mismatch")
    facts = context.get("facts")
    assert isinstance(facts, list)
    emitted_refs = [
        {
            "fact_id": fact.get("fact_id"),
            "fact_version": fact.get("fact_version"),
        }
        for fact in facts
        if isinstance(fact, dict)
    ]
    if emitted_refs != expected_refs:
        raise ContextResolutionError("Context response fact mismatch")
    if any(not isinstance(item, dict) or set(item) != _FACT_REF_FIELDS for item in expected_refs):
        raise ContextResolutionError("Context request refs are invalid")


__all__ = [
    "ContextBinding",
    "ContextEndpoint",
    "ContextResolutionError",
    "HttpContextResolver",
]
