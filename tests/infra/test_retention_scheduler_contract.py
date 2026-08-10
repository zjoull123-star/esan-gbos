from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
INFRA = ROOT / "infra" / "local"
SCRIPTS = ROOT / "scripts" / "local-pilot"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:")
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + match.start() if match else len(compose)
    return compose[start:end]


def test_scheduler_service_is_default_killed_internal_and_least_privilege() -> None:
    compose = _read(INFRA / "compose.yml")
    block = _service_block(compose, "retention-scheduler")

    assert 'profiles: ["retention-scheduler"]' in block
    assert "services.local_pilot_runtime.retention_scheduler" in block
    assert "GBOS_RETENTION_SCHEDULER_ENABLED: ${GBOS_RETENTION_SCHEDULER_ENABLED:-false}" in block
    assert (
        "GBOS_RETENTION_SCHEDULER_KILL_SWITCH: ${GBOS_RETENTION_SCHEDULER_KILL_SWITCH:-true}"
    ) in block
    assert "GBOS_RETENTION_ENABLED: ${GBOS_RETENTION_ENABLED:-false}" in block
    assert "GBOS_RETENTION_DRY_RUN: ${GBOS_RETENTION_DRY_RUN:-true}" in block
    assert "GBOS_RETENTION_INTERVAL_SECONDS: ${GBOS_RETENTION_INTERVAL_SECONDS:-86400}" in block
    assert 'GBOS_RETENTION_METRICS_PORT: "9101"' in block
    assert "runtime-retention.json:/config/local-pilot-runtime.json:ro" in block
    assert "local-pilot-evidence-cas:/var/lib/gbos/evidence" in block
    assert "local-pilot-tokenizer-vault:/var/lib/gbos/tokenizer-vault" in block
    assert "networks: [local-internal]" in block
    assert "controlled-egress" not in block
    assert "ports:" not in block
    assert "network_mode: host" not in block
    assert "read_only: true" in block
    assert "tmpfs:" in block
    assert 'cap_drop: ["ALL"]' in block
    assert "no-new-privileges:true" in block
    assert "restart: unless-stopped" in block
    for forbidden in (
        "postgres_context_password",
        "postgres_agent_password",
        "deepseek_api_key",
        "context_api_bearer",
        "agent_api_bearer",
    ):
        assert forbidden not in block


def test_start_requires_double_explicit_opt_in_and_keeps_startup_dry_run() -> None:
    start = _read(SCRIPTS / "start")
    emergency_stop = _read(SCRIPTS / "emergency-stop")

    assert "--enable-retention-scheduler" in start
    assert "--acknowledge-periodic-expired-local-data-deletion" in start
    assert 'ENABLE_RETENTION_SCHEDULER="false"' in start
    assert 'ACKNOWLEDGE_PERIODIC_RETENTION="false"' in start
    assert 'GBOS_RETENTION_SCHEDULER_ENABLED="true"' in start
    assert 'GBOS_RETENTION_SCHEDULER_KILL_SWITCH="false"' in start
    assert 'GBOS_RETENTION_ENABLED="true"' in start
    assert 'GBOS_RETENTION_DRY_RUN="false"' in start
    assert "profile_args+=(--profile retention-scheduler)" in start
    assert '"${SCRIPT_DIR}/run-retention" --dry-run' in start
    assert "--execute-expired-data" not in start
    assert start.index('"${SCRIPT_DIR}/run-retention" --dry-run') < start.rindex(" up -d --wait")
    assert "retention-scheduler" in emergency_stop


def test_scheduler_entrypoint_and_prometheus_alerts_are_content_free() -> None:
    entrypoints = json.loads(_read(INFRA / "runtime-entrypoints.json"))
    scheduler = entrypoints["services"]["retention-scheduler"]
    prometheus = _read(INFRA / "prometheus" / "prometheus.yml")
    alerts = _read(INFRA / "prometheus" / "alerts.yml")
    retention_alerts = alerts.split("  - name: retention-scheduler", 1)[1]

    assert scheduler == {
        "path": "services/local_pilot_runtime/retention_scheduler.py",
        "status": "executable",
        "network": "internal_only",
        "execution": "default_off_periodic_execute",
    }
    assert "job_name: retention-scheduler" in prometheus
    assert 'targets: ["retention-scheduler:9101"]' in prometheus
    assert "metrics_path: /metrics" in prometheus
    assert "RetentionSchedulerStale" in alerts
    assert "gbos_retention_scheduler_last_success_age_seconds > 172800" in alerts
    assert "RetentionSchedulerFailure" in alerts
    assert (
        "gbos_retention_scheduler_last_failure_timestamp_seconds > "
        "gbos_retention_scheduler_last_success_timestamp_seconds"
    ) in alerts
    assert 'code="retention_run_failed"' in alerts
    for forbidden in ("site_id", "identity", "party_ref", "team_ref", "object_ref", "sha256"):
        assert forbidden not in retention_alerts.lower()
