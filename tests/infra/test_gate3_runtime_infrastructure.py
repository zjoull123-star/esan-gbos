from __future__ import annotations

import re
import stat
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
COMPOSE = REPO_ROOT / "infra" / "dev" / "compose.yml"
ENV_EXAMPLE = REPO_ROOT / "infra" / "dev" / ".env.example"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SCRIPTS = REPO_ROOT / "scripts" / "dev"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def test_gate3_runtime_dependencies_are_locked_in_the_root_project() -> None:
    project = tomllib.loads(_read(PYPROJECT))["project"]
    dependencies = project["dependencies"]

    for package in ("fastapi", "uvicorn", "psycopg[binary,pool]"):
        assert any(item.startswith(f"{package}>=") for item in dependencies)
    assert (REPO_ROOT / "uv.lock").is_file()


def test_observer_postgres_uses_internal_service_network_and_loopback_host_bridge() -> None:
    compose = _read(COMPOSE)
    observer = _service_block(compose, "observer-postgres")

    assert re.search(
        r"(?ms)^  observer-postgres:.*?^    networks:\n      - gate3-internal$",
        compose,
    )
    assert "      - gate3-loopback" in observer
    assert '"127.0.0.1:${OBSERVER_POSTGRES_PORT:-5432}:5432"' in observer
    assert re.search(
        r"(?ms)^networks:\n  gate3-internal:\n    internal: true$",
        compose,
    )
    assert re.search(r"(?ms)^  gate3-loopback:\s*$", compose)
    assert "0.0.0.0:" not in compose


def test_gate3_migration_service_is_fail_closed_and_uses_no_external_image() -> None:
    compose = _read(COMPOSE)

    assert "gate3-db-migrate:" in compose
    assert "condition: service_healthy" in compose
    assert "../../services/observer/migrations:/migrations/observer:ro" in compose
    assert "../../services/context/migrations:/migrations/context:ro" in compose
    assert "${OBSERVER_POSTGRES_PASSWORD:?" in compose
    assert "kingdee" not in _service_block(compose, "gate3-db-migrate").lower()
    assert "networks:\n      - gate3-internal" in _service_block(compose, "gate3-db-migrate")


def _service_block(compose: str, name: str) -> str:
    start = compose.index(f"  {name}:")
    next_service = re.search(r"(?m)^  [a-z0-9][a-z0-9-]*:\s*$", compose[start + 1 :])
    end = start + 1 + next_service.start() if next_service else len(compose)
    return compose[start:end]


def test_gate3_scripts_are_executable_shell_valid_and_non_destructive_by_default() -> None:
    for name in ("gate3-migrate", "test-gate3-integration", "run-gate3-local"):
        path = SCRIPTS / name
        script = _read(path)
        assert path.stat().st_mode & stat.S_IXUSR
        assert 'SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")"' in script
        assert 'REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/../.."' in script
        assert "GBOS_PRODUCTION_ENABLED" in script
        assert "kingdee" not in script.lower()
        subprocess.run(["bash", "-n", str(path)], check=True)

    integration = _read(SCRIPTS / "test-gate3-integration")
    assert "--pull never" in _read(SCRIPTS / "gate3-migrate")
    assert "GBOS_RUN_POSTGRES_INTEGRATION=1" in integration
    assert "--volumes" not in integration

    runner = _read(SCRIPTS / "run-gate3-local")
    assert "127.0.0.1" in runner
    assert "GBOS_REAL_CONNECTORS_ENABLED" in runner
    assert "GBOS_MODEL_NETWORK_ENABLED" in runner
    observer_block = runner[runner.index("  observer)") : runner.index("  context)")]
    for setting in (
        "GBOS_OBSERVER_DATABASE_ENABLED=true",
        "GBOS_OBSERVER_DATABASE_HOST=127.0.0.1",
        'GBOS_OBSERVER_DATABASE_PORT="${OBSERVER_POSTGRES_PORT}"',
        'GBOS_OBSERVER_DATABASE_NAME="${OBSERVER_POSTGRES_DB}"',
        "GBOS_OBSERVER_DATABASE_USER=gbos_observer_app",
        'GBOS_OBSERVER_DATABASE_PASSWORD="${OBSERVER_POSTGRES_PASSWORD}"',
        "GBOS_CONTEXT_WRITE_ENABLED=true",
        'GBOS_CONTEXT_LOCAL_URL="http://127.0.0.1:${GBOS_CONTEXT_PORT:-8092}"',
    ):
        assert setting in observer_block


def test_example_environment_keeps_every_external_gate3_capability_disabled() -> None:
    env = _read(ENV_EXAMPLE)

    for setting in (
        "GBOS_REAL_CONNECTORS_ENABLED=false",
        "GBOS_MODEL_NETWORK_ENABLED=false",
        "GBOS_EXTERNAL_SEND_ENABLED=false",
        "GBOS_FRAPPE_BRIDGE_ENABLED=false",
        "GBOS_OBSERVER_DATABASE_ENABLED=false",
        "GBOS_CONTEXT_WRITE_ENABLED=false",
    ):
        assert setting in env
    assert "GBOS_CONTEXT_LOCAL_URL=http://127.0.0.1:8092" in env
    assert "KINGDEE" not in env


def test_gate3_integration_reuses_an_existing_ci_job() -> None:
    workflow = _read(CI)

    assert workflow.count("runs-on: ubuntu-24.04") == 8
    assert "scripts/dev/test-gate3-integration" in workflow
    assert "services/observer/observer" in workflow
    assert "services/context/context_service" in workflow
    for script in (
        "scripts/dev/gate3-migrate",
        "scripts/dev/run-gate3-local",
        "scripts/dev/test-gate3-integration",
    ):
        assert script in workflow
    assert workflow.index("scripts/dev/test-gate3-integration") < workflow.index(
        "Vue unit, build, and Playwright checks"
    )
