from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra/local"


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:\n")
    following = re.search(r"^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 3 :], re.MULTILINE)
    return compose[start:] if following is None else compose[start : start + 3 + following.start()]


def test_initial_route_runtime_is_distinct_default_off_and_reuses_existing_secrets() -> None:
    manifest = json.loads((INFRA / "local-pilot-manifest.json").read_text())
    compose = (INFRA / "compose.yml").read_text()
    service = _service_block(compose, "email-initial-route-worker")
    entrypoints = json.loads((INFRA / "runtime-entrypoints.json").read_text())

    assert manifest["email_gateway"]["initial_route_kill_switch"] is True
    assert "GBOS_EMAIL_INITIAL_ROUTE_KILL_SWITCH" in service
    assert "${GBOS_EMAIL_INITIAL_ROUTE_KILL_SWITCH:-true}" in service
    assert "postgres_email_gateway_password" in service
    assert "frappe_email_gateway_authority_api_key" in service
    assert "frappe_email_gateway_authority_api_secret" in service
    assert (
        "external_send" not in service.lower() or 'GBOS_EXTERNAL_SEND_ENABLED: "false"' in service
    )
    assert entrypoints["services"]["email-initial-route-worker"]["database_role"] == (
        "gbos_email_gateway_worker"
    )
    assert entrypoints["services"]["email-initial-route-worker"]["external_send"] is False
