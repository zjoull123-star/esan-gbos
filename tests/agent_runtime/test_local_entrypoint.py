from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.agent_runtime.local_entrypoint import (
    LocalEntrypointDisabled,
    load_local_manifest,
    require_component_enabled,
)


def _manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": "local_pilot",
        "site_id": "gbos.localhost",
        "local_pilot_go": False,
        "local_pilot_status": "disabled",
        "deepseek": {"enabled": False, "kill_switch": True},
    }


def test_manifest_loader_is_closed_and_component_default_is_disabled(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    manifest = load_local_manifest(path)
    assert manifest["site_id"] == "gbos.localhost"
    with pytest.raises(LocalEntrypointDisabled, match="disabled"):
        require_component_enabled(
            manifest,
            component="agent-worker",
            environ={},
        )


def test_manifest_loader_accepts_the_closed_email_gateway_section(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    value = _manifest()
    value["email_gateway"] = {
        "enabled": False,
        "kill_switch": True,
        "external_send": False,
    }
    path.write_text(json.dumps(value), encoding="utf-8")

    assert load_local_manifest(path)["email_gateway"] == value["email_gateway"]


def test_local_runtime_entrypoint_imports_do_not_start_db_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("import attempted an external side effect")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)

    agent_api = importlib.import_module("services.local_pilot_runtime.agent_api")
    agent_worker = importlib.import_module("services.local_pilot_runtime.agent_worker")
    model_worker = importlib.import_module("services.local_pilot_runtime.model_worker")

    response = TestClient(agent_api.build_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert callable(agent_worker.main)
    assert callable(model_worker.main)
