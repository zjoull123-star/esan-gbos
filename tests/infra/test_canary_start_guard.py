from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts" / "local-pilot"
COMPOSE = ROOT / "infra" / "local" / "compose.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _service_block(compose: str, service: str) -> str:
    start = compose.index(f"  {service}:\n")
    match = re.search(r"(?m)^  [a-zA-Z0-9][^:\n]*:\s*$", compose[start + 1 :])
    return compose[start:] if match is None else compose[start : start + 1 + match.start()]


def test_start_guard_runs_after_migrations_and_before_model_profile_or_egress() -> None:
    start = _read(SCRIPTS / "start")

    migrate = start.index("run --rm migrations")
    guard = start.index("run --rm --no-deps canary-start-guard")
    enable_model = start.index('GBOS_DEEPSEEK_EGRESS_ENABLED="true"')
    model_profile = start.index("profile_args+=(--profile model-projection)")
    final_up = start.rindex(" up -d --wait")

    assert migrate < guard < enable_model < model_profile < final_up
    assert 'GBOS_DEEPSEEK_EGRESS_ENABLED="false"' in start[:guard]
    assert 'GBOS_MODEL_KILL_SWITCH="true"' in start[:guard]
    assert 'GBOS_MODEL_PROJECTION_KILL_SWITCH="true"' in start[:guard]
    running_guard = start.index("compose ps --status running --services")
    prepare_secrets = start.index('"${SCRIPT_DIR}/prepare-secrets"')
    assert running_guard < prepare_secrets < migrate
    assert "model-projection-worker" in start[running_guard:prepare_secrets]
    assert "agent-worker" in start[running_guard:prepare_secrets]


def test_guard_and_chain_verifier_are_private_one_shot_compose_services() -> None:
    compose = _read(COMPOSE)
    guard = _service_block(compose, "canary-start-guard")
    verifier = _service_block(compose, "canary-verifier")

    assert 'profiles: ["canary-start-guard"]' in guard
    assert "services.local_pilot_runtime.canary_start_guard" in guard
    assert "networks: [local-internal]" in guard
    assert "controlled-egress" not in guard
    assert "ports:" not in guard
    assert 'restart: "no"' in guard
    assert "canary-start-guard.json:/config/canary-start-guard.json:ro" in guard
    assert "target: postgres_observer_password" in guard
    assert "mode: 0600" in guard

    assert 'profiles: ["canary-verifier"]' in verifier
    assert "services.local_pilot_runtime.canary_verifier_runtime" in verifier
    assert "networks: [local-internal]" in verifier
    assert "controlled-egress" not in verifier
    assert "ports:" not in verifier
    assert 'restart: "no"' in verifier
    assert "projection-connections.json:/config/projection-connections.json:ro" in verifier
    assert "local-pilot-evidence-cas:/var/lib/gbos/evidence:ro" in verifier
    assert "local-pilot-tokenizer-vault:/var/lib/gbos/tokenizer-vault:ro" in verifier
    for secret in (
        "postgres_observer_password",
        "postgres_context_password",
        "postgres_agent_password",
    ):
        assert f"target: {secret}" in verifier
    networks = compose[compose.index("networks:\n") :]
    local_internal = networks[: networks.index("  controlled-egress:")]
    assert "com.docker.network.bridge.enable_ip_masquerade" in local_internal
    assert '"false"' in local_internal


def test_verifier_launcher_uses_compose_runtime_not_host_docker_dns_or_secret_paths() -> None:
    launcher = _read(SCRIPTS / "canary_verifier_runtime")

    assert "--profile canary-verifier" in launcher
    assert "run --rm --no-deps canary-verifier" in launcher
    assert "read_secret_dir" in launcher
    assert "read_config_dir" in launcher
    assert "postgres:5432" not in launcher
    assert "/run/secrets" not in launcher


def test_runtime_entrypoints_publish_guard_and_executable_verifier_paths() -> None:
    entrypoints = json.loads(_read(ROOT / "infra" / "local" / "runtime-entrypoints.json"))

    guard = entrypoints["services"]["canary-start-guard"]
    verifier = entrypoints["services"]["canary-verifier"]
    assert guard["status"] == "executable"
    assert guard["network"] == "local-internal-only"
    assert verifier["status"] == "executable"
    assert verifier["launcher"] == "scripts/local-pilot/canary_verifier_runtime"
    assert verifier["network"] == "local-internal-only"
