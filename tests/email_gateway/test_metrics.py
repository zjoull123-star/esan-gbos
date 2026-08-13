from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 8, 13, 9, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def test_metrics_accept_only_fixed_low_cardinality_labels() -> None:
    from services.email_gateway.metrics import GatewayMetrics
    from services.email_gateway.models import ValidationError

    metrics = GatewayMetrics()
    metrics.set_gauge("gbos_email_gateway_publication_backlog", 2, labels={"state": "queued"})
    metrics.set_gauge("gbos_email_gateway_inbox_items", 3, labels={"queue_state": "unassigned"})
    metrics.increment(
        "gbos_email_gateway_authority_failures_total",
        labels={"safe_reason_code": "owner_unavailable"},
    )
    assert metrics.snapshot()["gbos_email_gateway_publication_backlog|state=queued"] == 2
    forbidden = (
        "mailbox",
        "address",
        "message_id",
        "participant",
        "identity_ref",
        "party_ref",
        "user_ref",
        "provider_payload",
        "error_payload",
    )
    for label in forbidden:
        with pytest.raises(ValidationError, match="label"):
            metrics.increment(
                "gbos_email_gateway_authority_failures_total", labels={label: "secret"}
            )


def test_metric_repr_never_contains_label_values() -> None:
    from services.email_gateway.metrics import GatewayMetrics

    metrics = GatewayMetrics()
    metrics.set_gauge(
        "gbos_email_gateway_worker_heartbeat_age_seconds",
        4,
        labels={"worker_kind": "retention"},
    )
    assert "retention" not in repr(metrics)


def test_metrics_freeze_exact_series_labels_and_fill_empty_publication_states() -> None:
    from services.email_gateway.metrics import GatewayMetrics
    from services.email_gateway.models import ValidationError

    metrics = GatewayMetrics()
    metrics.initialize_publication_states(backlog={"queued": 4}, oldest_age_seconds={"queued": 61})
    snapshot = metrics.snapshot()
    for state in ("queued", "retry", "leased", "dead_letter"):
        assert f"gbos_email_gateway_publication_backlog|state={state}" in snapshot
        assert f"gbos_email_gateway_publication_oldest_age_seconds|state={state}" in snapshot
    assert snapshot["gbos_email_gateway_publication_backlog|state=retry"] == 0
    assert snapshot["gbos_email_gateway_publication_oldest_age_seconds|state=retry"] == 0

    metrics.set_gauge("gbos_email_gateway_sla_overdue", 1, labels={})
    metrics.set_gauge("gbos_email_gateway_identity_pending", 1, labels={})
    metrics.set_gauge("gbos_email_gateway_unassigned", 1, labels={})
    metrics.increment("gbos_email_gateway_dead_letter_total", labels={"work_kind": "retention"})
    with pytest.raises(ValidationError, match="metric type"):
        metrics.increment("gbos_email_gateway_sla_overdue", labels={})
    with pytest.raises(ValidationError, match="metric type"):
        metrics.set_gauge(
            "gbos_email_gateway_dead_letter_total",
            1,
            labels={"work_kind": "retention"},
        )


def test_readiness_requires_persisted_heartbeat_no_older_than_thirty_seconds() -> None:
    from services.email_gateway.metrics import GatewayMetrics

    metrics = GatewayMetrics()
    assert metrics.readiness(now=NOW).ready is False
    metrics.record_persisted_heartbeat("retention", at=NOW - timedelta(seconds=30))
    readiness = metrics.readiness(now=NOW)
    assert readiness.ready is True
    assert readiness.oldest_heartbeat_age_seconds == 30
    metrics.record_persisted_heartbeat("publication", at=NOW - timedelta(seconds=31))
    assert metrics.readiness(now=NOW).ready is False


def test_alert_file_contains_exactly_the_four_frozen_email_gateway_rules() -> None:
    text = (ROOT / "infra/local/prometheus/alerts.yml").read_text()
    marker = "  - name: email-gateway\n"
    assert text.count(marker) == 1
    group = text.split(marker, maxsplit=1)[1]
    if "\n  - name: " in group:
        group = group.split("\n  - name: ", maxsplit=1)[0]
    assert group.count("      - alert:") == 4
    for expected in (
        "      - alert: EmailGatewayWorkerHeartbeatStale\n"
        "        expr: gbos_email_gateway_worker_heartbeat_age_seconds > 30\n"
        "        for: 2m\n",
        "      - alert: EmailGatewayDeadLetterIncrease\n"
        "        expr: increase(gbos_email_gateway_dead_letter_total[5m]) > 0\n"
        "        for: 5m\n",
        "      - alert: EmailGatewayPublicationBacklogStale\n"
        "        expr: max(gbos_email_gateway_publication_oldest_age_seconds"
        '{state=~"queued|retry"}) > 300\n'
        "        for: 10m\n",
        "      - alert: EmailGatewaySlaOverdue\n"
        "        expr: gbos_email_gateway_sla_overdue > 0\n"
        "        for: 15m\n",
    ):
        assert expected in group


def test_readiness_rejects_missing_worker_backlog_or_retention_failure() -> None:
    from services.email_gateway.metrics import GatewayMetrics

    metrics = GatewayMetrics(required_workers=frozenset({"publication", "retention"}))
    metrics.record_persisted_heartbeat("retention", at=NOW)
    assert metrics.readiness(now=NOW).ready is False

    metrics.record_persisted_heartbeat("publication", at=NOW)
    metrics.set_gauge(
        "gbos_email_gateway_publication_oldest_age_seconds",
        301,
        labels={"state": "queued"},
    )
    assert metrics.readiness(now=NOW).ready is False

    metrics.set_gauge(
        "gbos_email_gateway_publication_oldest_age_seconds",
        300,
        labels={"state": "queued"},
    )
    metrics.set_gauge("gbos_email_gateway_retention_failures", 1, labels={})
    assert metrics.readiness(now=NOW).ready is False

    metrics.set_gauge("gbos_email_gateway_retention_failures", 0, labels={})
    assert metrics.readiness(now=NOW).ready is True


def test_prometheus_render_has_frozen_names_and_never_renders_dynamic_content() -> None:
    from services.email_gateway.metrics import GatewayMetrics

    metrics = GatewayMetrics(required_workers=frozenset({"retention"}))
    metrics.record_persisted_heartbeat("retention", at=NOW)
    metrics.set_gauge("gbos_email_gateway_retention_backlog", 2, labels={})
    metrics.set_gauge("gbos_email_gateway_retention_failures", 0, labels={})

    rendered = metrics.render_prometheus(now=NOW)

    assert "gbos_email_gateway_retention_backlog 2" in rendered
    assert "gbos_email_gateway_retention_failures 0" in rendered
    assert "site.local" not in rendered
    assert "EVD-" not in rendered
