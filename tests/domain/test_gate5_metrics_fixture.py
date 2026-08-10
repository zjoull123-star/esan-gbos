from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from esan_gbos.domain.metrics_fixture import (
    MetricsFixtureError,
    bind_synthetic_dashboard_site,
    validate_dashboard,
)

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "apps" / "esan_gbos" / "esan_gbos" / "data" / "gate5" / "dashboard.json"
MANIFEST = FIXTURE.with_name("manifest.json")


def _dashboard() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_synthetic_dashboard_fixture_is_governed_and_visible() -> None:
    dashboard = validate_dashboard(_dashboard())

    assert dashboard["source_mode"] == "synthetic"
    assert dashboard["synthetic"] is True
    assert dashboard["metrics"]
    assert all(item["synthetic"] is True for item in dashboard["metrics"])


def test_fixture_manifest_binds_exact_network_free_payload() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["synthetic"] is True
    assert manifest["demo"] is True
    assert manifest["network_calls"] == 0
    assert manifest["kingdee_calls"] == 0
    assert manifest["credentials_loaded"] == 0
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == manifest["dashboard_sha256"]


def test_synthetic_dashboard_can_bind_to_a_fresh_site_without_mutating_fixture() -> None:
    original = _dashboard()

    scoped = bind_synthetic_dashboard_site(original, "gbos-smoke.localhost")

    assert original["site_id"] == "gbos.localhost"
    assert scoped["site_id"] == "gbos-smoke.localhost"
    assert {metric["site_id"] for metric in scoped["metrics"]} == {"gbos-smoke.localhost"}
    assert scoped["synthetic"] is True
    assert scoped["source_mode"] == "synthetic"


def test_live_dashboard_can_never_be_rebound_to_another_site() -> None:
    dashboard = _dashboard()
    dashboard["source_mode"] = "live"
    dashboard["synthetic"] = False
    for metric in dashboard["metrics"]:
        metric["source_mode"] = "live"
        metric["synthetic"] = False

    with pytest.raises(MetricsFixtureError, match="synthetic"):
        bind_synthetic_dashboard_site(dashboard, "gbos-smoke.localhost")


@pytest.mark.parametrize(
    ("section", "status"),
    (
        ("freshness", "stale"),
        ("coverage", "insufficient"),
        ("reconciliation", "failed"),
    ),
)
def test_available_fixture_metric_rejects_failed_quality_gate(section: str, status: str) -> None:
    dashboard = _dashboard()
    dashboard["metrics"][0][section]["status"] = status  # type: ignore[index]

    with pytest.raises(MetricsFixtureError):
        validate_dashboard(dashboard)


def test_unavailable_fixture_metric_cannot_carry_a_number() -> None:
    dashboard = _dashboard()
    metric = deepcopy(dashboard["metrics"][0])  # type: ignore[index]
    metric["status"] = "unavailable"
    metric["unavailable_reason"] = "stale"
    dashboard["metrics"][0] = metric  # type: ignore[index]

    with pytest.raises(MetricsFixtureError):
        validate_dashboard(dashboard)


def test_fixture_cannot_mix_source_modes() -> None:
    dashboard = _dashboard()
    dashboard["metrics"][0]["source_mode"] = "live"  # type: ignore[index]
    dashboard["metrics"][0]["synthetic"] = False  # type: ignore[index]

    with pytest.raises(MetricsFixtureError):
        validate_dashboard(dashboard)
