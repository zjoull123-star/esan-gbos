from __future__ import annotations

from collections.abc import Mapping

from .models import ValidationError

_LABEL_ALLOWLIST = {
    "publication_total": {"outcome": {"accepted", "duplicate", "conflict", "rejected"}},
    "inbox_items": {
        "state": {
            "identity_pending",
            "unassigned",
            "assigned",
            "draft",
            "waiting_internal",
            "waiting_customer",
            "converted",
            "closed",
            "quarantined",
            "send_queued",
            "send_uncertain",
        }
    },
}


class GatewayMetrics:
    def __init__(self) -> None:
        self._values: dict[str, float] = {}

    def increment(self, name: str, *, labels: Mapping[str, str]) -> None:
        key = self._key(name, labels)
        self._values[key] = self._values.get(key, 0) + 1

    def set_gauge(self, name: str, value: float, *, labels: Mapping[str, str]) -> None:
        if value < 0:
            raise ValidationError("invalid metric value")
        self._values[self._key(name, labels)] = value

    def snapshot(self) -> dict[str, float]:
        return dict(self._values)

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
        return f"{name}|{suffix}"
