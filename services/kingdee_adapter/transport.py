from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from .models import QueryPlan, ValidatedRequest


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportResult:
    available_status: bool
    rows: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    reason_code: str | None
    network_calls: int
    startup_available: bool
    metadata_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rows",
            tuple(MappingProxyType(deepcopy(dict(row))) for row in self.rows),
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(deepcopy(dict(self.metadata))),
        )
        if self.network_calls < 0:
            raise ValueError("network_calls cannot be negative")

    @classmethod
    def available(
        cls,
        *,
        rows: tuple[Mapping[str, Any], ...],
        metadata: Mapping[str, Any],
        network_calls: int = 0,
    ) -> TransportResult:
        return cls(
            available_status=True,
            rows=rows,
            metadata=metadata,
            reason_code=None,
            network_calls=network_calls,
            startup_available=True,
            metadata_available=True,
        )

    @classmethod
    def unavailable(
        cls,
        *,
        reason_code: str,
        network_calls: int = 0,
        startup_available: bool,
        metadata_available: bool,
    ) -> TransportResult:
        return cls(
            available_status=False,
            rows=(),
            metadata={},
            reason_code=reason_code,
            network_calls=network_calls,
            startup_available=startup_available,
            metadata_available=metadata_available,
        )


class Transport(Protocol):
    synthetic: bool

    def execute(self, *, plan: QueryPlan, request: ValidatedRequest) -> TransportResult: ...


class SyntheticTransport:
    """Deterministic local transport with no fixture, network, env, or subprocess use."""

    synthetic = True
    _total_rows = 100

    def execute(self, *, plan: QueryPlan, request: ValidatedRequest) -> TransportResult:
        if plan.tool_name == "metadata.get":
            return TransportResult.available(
                rows=(),
                metadata={
                    "logical_object": plan.logical_object,
                    "source_form": plan.source_form,
                    "fields": [
                        {
                            "logical_name": logical_name,
                            "source_field": source_field,
                            "data_type": data_type,
                            "verification_status": "synthetic_only",
                        }
                        for logical_name, source_field, data_type in zip(
                            plan.fields,
                            plan.source_fields,
                            plan.field_types,
                            strict=True,
                        )
                    ],
                },
            )
        end = min(request.offset + request.limit, self._total_rows)
        rows = tuple(self._row(plan, index) for index in range(request.offset, end))
        return TransportResult.available(
            rows=rows,
            metadata={"source": "gate5_deterministic_synthetic"},
        )

    @staticmethod
    def _row(plan: QueryPlan, index: int) -> Mapping[str, Any]:
        ordinal = index + 1
        values: dict[str, object] = {}
        for field, data_type in zip(plan.fields, plan.field_types, strict=True):
            if data_type == "number":
                value: object = float(ordinal * 100)
            elif data_type == "date":
                value = f"2026-08-{((ordinal - 1) % 28) + 1:02d}"
            else:
                value = f"Synthetic {plan.logical_object} {field} {ordinal:04d}"
            values[field] = value
        return {
            "record_ref": f"{plan.logical_object}-synthetic-{ordinal:04d}",
            "synthetic": True,
            "values": values,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveDestination:
    url: str
    allowlisted_urls: frozenset[str]

    def __post_init__(self) -> None:
        if self.url not in self.allowlisted_urls:
            raise ValueError("live Kingdee destination is not exactly allowlisted")
        if any(character.isspace() for character in self.url):
            raise ValueError("live Kingdee destination contains whitespace")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https":
            raise ValueError("live Kingdee destination must use https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("live Kingdee destination cannot contain user information")
        if parsed.query or parsed.fragment:
            raise ValueError("live Kingdee destination cannot contain query or fragment")
        if parsed.hostname is None:
            raise ValueError("live Kingdee destination requires a hostname")
        if parsed.port not in (None, 443):
            raise ValueError("live Kingdee destination port is not allowed")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
            raise ValueError("local live Kingdee destinations are forbidden")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP-literal live Kingdee destinations are forbidden")


@dataclass(frozen=True, slots=True, kw_only=True)
class LiveEntryGates:
    enabled: bool
    gate: int
    runtime_mode: str
    network_allowed: bool
    credentials_available: bool
    metadata_verified: bool
    business_reads_enabled: bool

    def __post_init__(self) -> None:
        boolean_values = (
            self.enabled,
            self.network_allowed,
            self.credentials_available,
            self.metadata_verified,
            self.business_reads_enabled,
        )
        if any(type(value) is not bool for value in boolean_values):
            raise ValueError("live entry gate flags must be boolean")
        if type(self.gate) is not int:
            raise ValueError("live entry gate must be an integer")
        if not isinstance(self.runtime_mode, str):
            raise ValueError("live runtime_mode must be a string")

    @property
    def ready(self) -> bool:
        return (
            self.enabled
            and self.gate >= 5
            and self.runtime_mode == "live"
            and self.network_allowed
            and self.credentials_available
            and self.metadata_verified
            and self.business_reads_enabled
        )


class LiveBackend(Protocol):
    def __call__(
        self,
        *,
        destination: LiveDestination,
        plan: QueryPlan,
        request: ValidatedRequest,
    ) -> TransportResult: ...


class LiveTransport:
    """Gate-locked live boundary.

    The actual MCP/client operation is injected. This module has no HTTP client
    and cannot silently switch to a synthetic implementation.
    """

    synthetic = False

    def __init__(
        self,
        *,
        destination: LiveDestination,
        gates: LiveEntryGates,
        backend: LiveBackend,
    ) -> None:
        self._destination = destination
        self._gates = gates
        self._backend = backend

    def execute(self, *, plan: QueryPlan, request: ValidatedRequest) -> TransportResult:
        if not self._gates.ready:
            return TransportResult.unavailable(
                reason_code="live_entry_gates_closed",
                startup_available=False,
                metadata_available=False,
            )
        try:
            result = self._backend(
                destination=self._destination,
                plan=plan,
                request=request,
            )
        except Exception:
            return TransportResult.unavailable(
                reason_code="live_transport_unavailable",
                network_calls=1,
                startup_available=True,
                metadata_available=self._gates.metadata_verified,
            )
        if not isinstance(result, TransportResult):
            return TransportResult.unavailable(
                reason_code="live_transport_invalid_response",
                network_calls=1,
                startup_available=True,
                metadata_available=self._gates.metadata_verified,
            )
        return result
