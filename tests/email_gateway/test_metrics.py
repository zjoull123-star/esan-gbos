from __future__ import annotations

import pytest


def test_metrics_accept_only_fixed_low_cardinality_labels() -> None:
    from services.email_gateway.metrics import GatewayMetrics
    from services.email_gateway.models import ValidationError

    metrics = GatewayMetrics()
    metrics.increment("publication_total", labels={"outcome": "accepted"})
    metrics.set_gauge("inbox_items", 2, labels={"state": "unassigned"})
    assert metrics.snapshot()["publication_total|outcome=accepted"] == 1
    for label in ("mailbox", "address", "message_id", "participant", "identity_ref"):
        with pytest.raises(ValidationError, match="label"):
            metrics.increment("publication_total", labels={label: "secret"})


def test_metric_repr_never_contains_label_values() -> None:
    from services.email_gateway.metrics import GatewayMetrics

    metrics = GatewayMetrics()
    metrics.increment("publication_total", labels={"outcome": "accepted"})
    assert "accepted" not in repr(metrics)
