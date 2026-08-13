from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .models import INBOX_STATES, ValidationError

PUBLICATION_STATES = frozenset({"queued", "retry", "leased", "dead_letter"})
AUTHORITY_FAILURE_CODES = frozenset(
    {
        "authority_unavailable",
        "identity_unavailable",
        "owner_unavailable",
        "permission_denied",
        "revision_conflict",
        "scope_mismatch",
    }
)
WORKER_KINDS = frozenset(
    {"publication", "mailbox_config_projection", "identity", "routing", "retention"}
)
WORK_KINDS = frozenset({"publication", "mailbox_config_projection", "identity_route", "retention"})

_GAUGES: dict[str, Mapping[str, frozenset[str]]] = {
    "gbos_email_gateway_publication_backlog": {"state": PUBLICATION_STATES},
    "gbos_email_gateway_publication_oldest_age_seconds": {"state": PUBLICATION_STATES},
    "gbos_email_gateway_inbox_items": {"queue_state": INBOX_STATES},
    "gbos_email_gateway_sla_overdue": {},
    "gbos_email_gateway_identity_pending": {},
    "gbos_email_gateway_unassigned": {},
    "gbos_email_gateway_worker_heartbeat_age_seconds": {"worker_kind": WORKER_KINDS},
    "gbos_email_gateway_retention_backlog": {},
    "gbos_email_gateway_retention_failures": {},
}
_COUNTERS: dict[str, Mapping[str, frozenset[str]]] = {
    "gbos_email_gateway_authority_failures_total": {"safe_reason_code": AUTHORITY_FAILURE_CODES},
    "gbos_email_gateway_dead_letter_total": {"work_kind": WORK_KINDS},
}
_LABEL_ALLOWLIST = {**_GAUGES, **_COUNTERS}
_READINESS_WINDOW_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class GatewayReadiness:
    ready: bool
    oldest_heartbeat_age_seconds: float | None


class GatewayMetrics:
    """Content-free fixed-cardinality Gateway metrics and persisted-heartbeat readiness."""

    def __init__(self, *, required_workers: frozenset[str] = frozenset()) -> None:
        if not required_workers.issubset(WORKER_KINDS):
            raise ValidationError("metric label value rejected")
        self._values: dict[str, float] = {}
        self._persisted_heartbeats: dict[str, datetime] = {}
        self._required_workers = required_workers

    def increment(self, name: str, *, labels: Mapping[str, str]) -> None:
        if name not in _COUNTERS:
            self._reject_type(name)
        key = self._key(name, labels)
        self._values[key] = self._values.get(key, 0.0) + 1.0

    def set_gauge(self, name: str, value: float, *, labels: Mapping[str, str]) -> None:
        if name not in _GAUGES:
            self._reject_type(name)
        numeric = self._nonnegative(value)
        self._values[self._key(name, labels)] = numeric

    def initialize_publication_states(
        self,
        *,
        backlog: Mapping[str, float],
        oldest_age_seconds: Mapping[str, float],
    ) -> None:
        self._validate_state_mapping(backlog)
        self._validate_state_mapping(oldest_age_seconds)
        for state in sorted(PUBLICATION_STATES):
            self.set_gauge(
                "gbos_email_gateway_publication_backlog",
                backlog.get(state, 0),
                labels={"state": state},
            )
            self.set_gauge(
                "gbos_email_gateway_publication_oldest_age_seconds",
                oldest_age_seconds.get(state, 0),
                labels={"state": state},
            )

    def record_persisted_heartbeat(self, worker_kind: str, *, at: datetime) -> None:
        self._key(
            "gbos_email_gateway_worker_heartbeat_age_seconds",
            {"worker_kind": worker_kind},
        )
        self._aware(at, "heartbeat")
        self._persisted_heartbeats[worker_kind] = at

    def readiness(self, *, now: datetime) -> GatewayReadiness:
        self._aware(now, "readiness time")
        if not self._persisted_heartbeats:
            return GatewayReadiness(False, None)
        if not self._required_workers.issubset(self._persisted_heartbeats):
            return GatewayReadiness(False, None)
        ages = tuple((now - item).total_seconds() for item in self._persisted_heartbeats.values())
        if any(age < 0 for age in ages):
            raise ValidationError("heartbeat clock regression")
        oldest = max(ages)
        for worker_kind, heartbeat in sorted(self._persisted_heartbeats.items()):
            self.set_gauge(
                "gbos_email_gateway_worker_heartbeat_age_seconds",
                (now - heartbeat).total_seconds(),
                labels={"worker_kind": worker_kind},
            )
        backlog = self._values.get(
            "gbos_email_gateway_publication_oldest_age_seconds|state=queued", 0.0
        )
        retry_backlog = self._values.get(
            "gbos_email_gateway_publication_oldest_age_seconds|state=retry", 0.0
        )
        failures = self._values.get("gbos_email_gateway_retention_failures", 0.0)
        ready = (
            oldest <= _READINESS_WINDOW_SECONDS
            and max(backlog, retry_backlog) <= 300.0
            and failures == 0.0
        )
        return GatewayReadiness(ready, oldest)

    def snapshot(self) -> dict[str, float]:
        return dict(self._values)

    def render_prometheus(self, *, now: datetime) -> str:
        self.readiness(now=now)
        lines: list[str] = []
        for key, value in sorted(self._values.items()):
            name, separator, suffix = key.partition("|")
            labels = ""
            if separator:
                pairs = [item.split("=", 1) for item in suffix.split(",")]
                labels = "{" + ",".join(f'{label}="{item}"' for label, item in pairs) + "}"
            number = str(int(value)) if value.is_integer() else format(value, ".6g")
            lines.append(f"{name}{labels} {number}")
        return "\n".join(lines) + "\n"

    def __repr__(self) -> str:
        return f"GatewayMetrics(series_count={len(self._values)}, labels=<redacted>)"

    @staticmethod
    def _key(name: str, labels: Mapping[str, str]) -> str:
        allowed = _LABEL_ALLOWLIST.get(name)
        if allowed is None or set(labels) != set(allowed):
            raise ValidationError("metric label set rejected")
        for label, value in labels.items():
            if value not in allowed[label]:
                raise ValidationError("metric label value rejected")
        suffix = ",".join(f"{key}={labels[key]}" for key in sorted(labels))
        return name if not suffix else f"{name}|{suffix}"

    @staticmethod
    def _reject_type(name: str) -> None:
        if name in _LABEL_ALLOWLIST:
            raise ValidationError("metric type rejected")
        raise ValidationError("metric label set rejected")

    @staticmethod
    def _nonnegative(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("invalid metric value")
        numeric = float(value)
        if numeric < 0 or not math.isfinite(numeric):
            raise ValidationError("invalid metric value")
        return numeric

    @staticmethod
    def _aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError(f"invalid {name}")

    @staticmethod
    def _validate_state_mapping(values: Mapping[str, float]) -> None:
        if not set(values).issubset(PUBLICATION_STATES):
            raise ValidationError("metric label value rejected")
        for value in values.values():
            GatewayMetrics._nonnegative(value)
