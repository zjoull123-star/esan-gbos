from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import frappe

from esan_gbos.api.v1.common import BFFError, bff_endpoint, require_roles, success
from esan_gbos.domain.metrics_fixture import MetricsFixtureError, validate_dashboard

METRICS_READ_ROLES = frozenset({"CEO", "Finance Readonly", "GBOS Admin"})
_TRUE = frozenset({"1", "true", "yes"})


def _fixture_directory() -> Path:
    return Path(frappe.get_app_path("esan_gbos")) / "data" / "gate5"


def _load_synthetic_dashboard(directory: Path | None = None) -> dict[str, Any]:
    fixture_root = directory or _fixture_directory()
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    dashboard_bytes = (fixture_root / "dashboard.json").read_bytes()
    if not isinstance(manifest, dict):
        raise MetricsFixtureError("fixture manifest must be an object")
    if (
        manifest.get("dataset") != "gate5-governed-metrics"
        or manifest.get("synthetic") is not True
        or manifest.get("demo") is not True
        or manifest.get("network_calls") != 0
        or manifest.get("kingdee_calls") != 0
        or manifest.get("credentials_loaded") != 0
    ):
        raise MetricsFixtureError("fixture manifest is not synthetic and network-free")
    if hashlib.sha256(dashboard_bytes).hexdigest() != manifest.get("dashboard_sha256"):
        raise MetricsFixtureError("dashboard fixture checksum mismatch")
    dashboard = validate_dashboard(json.loads(dashboard_bytes))
    if len(dashboard["metrics"]) != manifest.get("metric_count"):
        raise MetricsFixtureError("dashboard metric count does not match manifest")
    return dashboard


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@bff_endpoint("GET")
def dashboard() -> dict[str, Any]:
    """Return the governed local Gate 5 dashboard without enabling a live source."""
    require_roles(METRICS_READ_ROLES)
    if os.environ.get("GBOS_PRODUCTION_ENABLED", "").strip().lower() in _TRUE:
        raise BFFError(
            "internal_error",
            "Synthetic metrics are disabled in production",
            status=503,
        )
    try:
        payload = _load_synthetic_dashboard()
    except (OSError, json.JSONDecodeError, MetricsFixtureError) as error:
        frappe.logger("esan_gbos").error(
            "Gate 5 metrics fixture unavailable exception=%s",
            type(error).__name__,
        )
        raise BFFError("internal_error", "Metrics source unavailable", status=503) from error
    if payload["site_id"] != frappe.local.site:
        raise BFFError("scope_mismatch", "Metrics site does not match the active site", status=403)
    return success(payload)
