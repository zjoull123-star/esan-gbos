from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
COMPOSE = ROOT / "infra" / "local" / "compose.yml"
ENTRYPOINTS = ROOT / "infra" / "local" / "runtime-entrypoints.json"
CLOUDFLARED = ROOT / "infra" / "local" / "cloudflared" / "config.yml"
MANIFEST = ROOT / "infra" / "local" / "local-pilot-manifest.json"
PREPARE = ROOT / "scripts" / "local-pilot" / "prepare-secrets"
RENDER = ROOT / "scripts" / "local-pilot" / "render-config"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:")
    match = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + match.start() if match else len(compose)
    return compose[start:end]


def test_wecom_callback_service_is_default_off_receive_only_and_least_secret() -> None:
    compose = _read(COMPOSE)
    block = _service_block(compose, "wecom-app-mail-webhook")

    assert 'profiles: ["wecom-app-mail"]' in block
    assert (
        "GBOS_WECOM_APP_MAIL_CALLBACK_ENABLED: ${GBOS_WECOM_APP_MAIL_CALLBACK_ENABLED:-false}"
    ) in block
    assert (
        "GBOS_WECOM_APP_MAIL_CALLBACK_KILL_SWITCH: "
        "${GBOS_WECOM_APP_MAIL_CALLBACK_KILL_SWITCH:-true}"
    ) in block
    assert "GBOS_GLOBAL_KILL_SWITCH: ${GBOS_GLOBAL_KILL_SWITCH:-true}" in block
    assert 'GBOS_EXTERNAL_SEND_ENABLED: "false"' in block
    assert "local-internal" in block and "webhook-tunnel" in block
    assert "controlled-egress" not in block
    assert "postgres" not in block.lower()
    assert "ports:" not in block
    assert "read_only: true" in block
    assert 'cap_drop: ["ALL"]' in block
    assert "no-new-privileges:true" in block
    for required in (
        "wecom_app_mail_callback_token",
        "wecom_app_mail_callback_aes_key",
        "observer_email_signal_bearer",
    ):
        assert f"- {required}" in block
    for forbidden in (
        "wecom_app_mail_app_secret",
        "postgres_observer_password",
        "identity_hmac_key",
        "deepseek_api_key",
        "smtp",
        "email_send",
        "mailbox_projection_bearer",
    ):
        assert forbidden not in block
    assert "../../.runtime/local-pilot:/run/gbos:ro" in block
    assert "runtime-wecom-app-mail-webhook.json:/config/wecom-app-mail-webhook.json:ro" in block

    observer = _service_block(compose, "observer-api")
    assert "observer_email_signal_bearer" in observer


def test_wecom_callback_has_exact_tunnel_route_and_runtime_inventory() -> None:
    tunnel = _read(CLOUDFLARED)
    assert "path: ^/webhooks/wecom-app-mail$" in tunnel
    assert "service: http://wecom-app-mail-webhook:8005" in tunnel
    assert tunnel.index("^/webhooks/wecom-app-mail$") < tunnel.index("http_status:404")
    assert "/internal/" not in tunnel

    entrypoints = json.loads(_read(ENTRYPOINTS))
    item = entrypoints["services"]["wecom-app-mail-webhook"]
    assert item == {
        "path": "services/local_pilot_runtime/wecom_app_mail_webhook.py",
        "status": "default_off_callback_signal_only",
        "network": "local-internal-and-webhook-tunnel",
        "database_role": None,
        "external_send": False,
        "provider_pull": False,
        "secrets": [
            "wecom_app_mail_callback_token",
            "wecom_app_mail_callback_aes_key",
            "observer_email_signal_bearer",
        ],
    }


def test_renderer_emits_closed_disabled_callback_config(tmp_path: Path) -> None:
    output = tmp_path / "rendered"
    result = subprocess.run(
        [
            str(RENDER),
            "--manifest",
            str(MANIFEST),
            "--output-dir",
            str(output),
            "--synthetic",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    value = json.loads(_read(output / "runtime-wecom-app-mail-webhook.json"))
    assert value == {
        "schema_version": "1.0",
        "enabled": False,
        "kill_switch": True,
        "external_send": False,
        "site_id": "gbos.localhost",
        "observer_connector_instance_ref": None,
        "mailbox_ref": None,
        "mailbox_config_revision": None,
        "activation_not_before": None,
        "corp_id": None,
        "agent_id": None,
        "callback_path": "/webhooks/wecom-app-mail",
        "observer_signal_url": "http://observer-api:8003/internal/v1/email-signals/accept",
        "max_body_bytes": 65536,
        "max_query_bytes": 1024,
    }
    assert value["external_send"] is False


def test_secret_preparation_declares_only_three_new_logical_callback_secrets() -> None:
    script = _read(PREPARE)
    for name in (
        "wecom_app_mail_callback_token",
        "wecom_app_mail_callback_aes_key",
        "observer_email_signal_bearer",
    ):
        assert name in script
        assert f"/run/secrets/{name}" not in script
        account = name.replace("_", "-")
        assert f"keychain://com.esan.gbos.local-pilot/{account}" in script
    assert "wecom-app-mail-app-secret" not in script
