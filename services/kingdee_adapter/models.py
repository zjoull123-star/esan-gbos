from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

type Scalar = str | int | float | bool | None
type Row = Mapping[str, Any]


class AdapterStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthContext:
    """Authentication result supplied by the local service boundary.

    Deliberately contains no bearer token, credential, or raw header field.
    """

    authenticated: bool
    granted_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.authenticated, bool):
            raise ValueError("authenticated must be boolean")
        if len(self.granted_scopes) != len(set(self.granted_scopes)):
            raise ValueError("granted_scopes must be unique")
        if any(not isinstance(scope, str) or not scope for scope in self.granted_scopes):
            raise ValueError("granted_scopes must be non-empty strings")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedRequest:
    request_id: str
    site_id: str
    account_set_ref: str
    processing_purpose: str
    logical_object: str
    limit: int
    offset: int
    timeout_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryPlan:
    """Frozen internal plan; no caller-controlled query fragments are present."""

    tool_name: str
    logical_object: str
    source_form: str
    fields: tuple[str, ...]
    source_fields: tuple[str, ...]
    field_types: tuple[str, ...]
    filters: tuple[tuple[str, str, Scalar], ...]
    order: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationSnapshot:
    startup: VerificationStatus
    authentication: VerificationStatus
    metadata: VerificationStatus
    business: VerificationStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlMetrics:
    network_calls: int
    writer_tools_discovered: int = 0
    mutation_attempts: int = 0
    synthetic_fallbacks: int = 0


def _detached_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(value)))


@dataclass(frozen=True, slots=True, kw_only=True)
class AdapterResponse:
    status: AdapterStatus
    request_id: str
    site_id: str
    logical_object: str
    tool_name: str
    synthetic: bool
    rows: tuple[Row, ...]
    metadata: Mapping[str, Any]
    page: Mapping[str, int | bool]
    verification: VerificationSnapshot
    controls: ControlMetrics
    reason_code: str | None = None

    def __post_init__(self) -> None:
        detached_rows = tuple(_detached_mapping(row) for row in self.rows)
        object.__setattr__(self, "rows", detached_rows)
        object.__setattr__(self, "metadata", _detached_mapping(self.metadata))
        object.__setattr__(self, "page", _detached_mapping(self.page))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "request_id": self.request_id,
            "site_id": self.site_id,
            "logical_object": self.logical_object,
            "tool_name": self.tool_name,
            "synthetic": self.synthetic,
            "rows": deepcopy([dict(row) for row in self.rows]),
            "metadata": deepcopy(dict(self.metadata)),
            "page": deepcopy(dict(self.page)),
            "verification": {
                "startup": self.verification.startup.value,
                "authentication": self.verification.authentication.value,
                "metadata": self.verification.metadata.value,
                "business": self.verification.business.value,
            },
            "controls": {
                "network_calls": self.controls.network_calls,
                "writer_tools_discovered": self.controls.writer_tools_discovered,
                "mutation_attempts": self.controls.mutation_attempts,
                "synthetic_fallbacks": self.controls.synthetic_fallbacks,
            },
            "reason_code": self.reason_code,
        }
