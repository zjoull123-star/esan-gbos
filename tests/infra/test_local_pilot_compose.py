from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
LOCAL_INFRA = ROOT / "infra" / "local"
COMPOSE = LOCAL_INFRA / "compose.yml"


def _read(path: Path) -> str:
    assert path.is_file(), f"required local-pilot asset is missing: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:")
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + match.start() if match else len(compose)
    return compose[start:end]


def test_local_compose_isolated_and_remote_images_are_digest_pinned() -> None:
    compose = _read(COMPOSE)
    lock = json.loads(_read(LOCAL_INFRA / "images.lock.json"))

    assert "name: esan-gbos-local-pilot" in compose
    assert "infra/dev" not in compose
    assert "esan-gbos-dev" not in compose
    assert ":latest" not in compose
    assert "${ERPNEXT_IMAGE" not in compose

    for volume in (
        "local-pilot-postgres-data",
        "local-pilot-object-data",
    ):
        assert re.search(rf"(?m)^  {re.escape(volume)}:\s*$", compose)
        assert f"name: esan-gbos-{volume}" in compose

    assert (
        "pgvector/pgvector:0.8.2-pg17-bookworm"
        "@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966"
    ) in compose
    assert lock["images"]
    assert all(":latest" not in item["reference"] for item in lock["images"])
    assert all(
        "@sha256:" in item["reference"] for item in lock["images"] if item["source"] == "remote"
    )
    postgres = next(item for item in lock["images"] if item["service"] == "postgres")
    assert postgres["local_inspect_digest"].startswith("sha256:")
    assert postgres["local_repo_digest"].endswith(postgres["local_inspect_digest"])
    for service in (
        "postgres",
        "object-store",
        "prometheus",
        "webhook-ingress",
        "agent-runtime",
        "email-poller",
        "wecom-poller",
        "agent-worker",
        "deepseek-worker",
        "media-worker",
        "cloudflared",
    ):
        assert "pull_policy: never" in _service_block(compose, service)


def test_every_published_database_ui_and_monitoring_port_is_loopback_only() -> None:
    compose = _read(COMPOSE)

    assert "0.0.0.0:" not in compose
    assert ":::" not in compose
    published_ports = [
        line.strip().lstrip("-").strip().strip("\"'")
        for line in compose.splitlines()
        if re.fullmatch(r'\s*-\s*["\'][^"\']+:\d{2,5}(?:/tcp)?["\']\s*', line)
    ]
    assert published_ports
    assert all(port.startswith("127.0.0.1:") for port in published_ports)

    for service in ("postgres", "object-store", "prometheus"):
        assert "127.0.0.1:" in _service_block(compose, service)
    assert "pilot-ui:" not in compose
    assert "services.local_pilot_runtime.ui" not in compose


def test_external_capabilities_are_profile_gated_and_default_killed() -> None:
    compose = _read(COMPOSE)

    for service, profile in (
        ("email-poller", "email"),
        ("wecom-poller", "wecom"),
    ):
        block = _service_block(compose, service)
        assert f'profiles: ["{profile}"]' in block
        assert "GBOS_CONNECTOR_KILL_SWITCH: ${GBOS_CONNECTOR_KILL_SWITCH:-true}" in block
        assert "controlled-egress" in block

    model = _service_block(compose, "deepseek-worker")
    assert 'profiles: ["model"]' in model
    assert "GBOS_MODEL_KILL_SWITCH: ${GBOS_MODEL_KILL_SWITCH:-true}" in model
    assert "controlled-egress" in model

    tunnel = _service_block(compose, "cloudflared")
    assert 'profiles: ["tunnel"]' in tunnel
    assert "controlled-egress" in tunnel
    assert "local-internal" in tunnel
    assert 'profiles: ["whatsapp"]' in _service_block(compose, "webhook-ingress")

    assert 'GBOS_KINGDEE_ENABLED: "false"' in compose
    assert re.search(r"(?m)^  kingdee(?:-|_).*:\s*$", compose.lower()) is None
    assert "GBOS_EXTERNAL_SEND_ENABLED: ${GBOS_EXTERNAL_SEND_ENABLED:-false}" in compose
    assert re.search(r"(?ms)^  local-internal:.*?internal: true", compose)
    assert "whatsapp-poller:" not in compose
    assert '"whatsapp"]' not in compose.replace('profiles: ["whatsapp"]', "")


def test_cloudflared_can_reach_only_the_whatsapp_webhook_ingress() -> None:
    compose = _read(COMPOSE)
    config = _read(LOCAL_INFRA / "cloudflared" / "config.yml")

    assert "./cloudflared/config.yml:/etc/cloudflared/config.yml:ro" in compose
    assert "path: ^/webhooks/whatsapp(/.*)?$" in config
    assert "service: http://webhook-ingress:8000" in config
    assert "service: http_status:404" in config
    assert "/api/" not in config
    assert "/internal/" not in config


def test_media_runtime_is_offline_and_models_are_read_only() -> None:
    compose = _read(COMPOSE)
    media = _service_block(compose, "media-worker")

    assert 'profiles: ["media"]' in media
    assert "${GBOS_MEDIA_MODEL_DIR:-/tmp/gbos-local-pilot-models-unavailable}:/models:ro" in media
    assert 'HF_HUB_OFFLINE: "1"' in media
    assert 'TRANSFORMERS_OFFLINE: "1"' in media
    assert 'PIP_NO_INDEX: "1"' in media
    assert "controlled-egress" not in media
    assert "download" not in media.lower()


def test_health_dependencies_kill_switches_and_file_secrets_are_explicit() -> None:
    compose = _read(COMPOSE)

    assert compose.count("healthcheck:") >= 5
    assert compose.count("condition: service_healthy") >= 5
    assert "GBOS_EMERGENCY_STOP_FILE: /run/gbos/EMERGENCY_STOP" in compose
    assert "./../../.runtime/local-pilot:/run/gbos:ro" in compose
    assert "${GBOS_SECRET_DIR:-/tmp/gbos-local-pilot-secrets-unavailable}" in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in compose
    assert "MINIO_ROOT_PASSWORD_FILE: /run/secrets/object_store_password" in compose
    assert not re.search(r"(?i)(password|token|api_key):\s*[\"']?[A-Za-z0-9_-]{12,}", compose)


def test_prometheus_and_alert_baseline_cover_safety_and_dependencies() -> None:
    compose = _read(COMPOSE)
    prometheus = _read(LOCAL_INFRA / "prometheus" / "prometheus.yml")
    alerts = _read(LOCAL_INFRA / "prometheus" / "alerts.yml")

    assert "prometheus" in _service_block(compose, "prometheus")
    assert "local-pilot-alerts" in prometheus
    for job in ("webhook-ingress", "agent-runtime"):
        assert f"job_name: {job}" in prometheus
    assert "job_name: pilot-ui" not in prometheus
    for alert in (
        "LocalPilotTargetDown",
        "LocalPilotEmergencyStopActive",
        "LocalPilotConnectorUnexpectedlyEnabled",
        "LocalPilotModelUnexpectedlyEnabled",
        "LocalPilotDatabaseUnavailable",
        "LocalPilotObjectStoreUnavailable",
    ):
        assert f"alert: {alert}" in alerts
    assert 'up{job="agent-runtime"} == 0' in alerts
