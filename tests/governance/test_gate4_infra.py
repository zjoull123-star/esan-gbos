from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_gate4_migration_is_local_only_checksum_guarded_and_repeatable() -> None:
    script = (ROOT / "scripts" / "dev" / "gate4-migrate").read_text(encoding="utf-8")

    assert "GBOS_PRODUCTION_ENABLED" in script
    assert "refuses production" in script
    assert "observer.schema_migrations" in script
    assert "existing_checksum" in script
    assert "Applied migration checksum changed" in script
    assert "/migrations/agent" in script
    assert "/migrations/context/00[2-4]_gate4_*.sql" in script
    assert "gbos_agent_app" in script
    assert "NOBYPASSRLS" in script


def test_compose_adds_only_a_local_gate4_migration_job() -> None:
    compose = (ROOT / "infra" / "dev" / "compose.yml").read_text(encoding="utf-8")

    assert "gate4-db-migrate:" in compose
    assert 'command: ["/scripts/gate4-migrate", "--inside-container"]' in compose
    assert "../../services/agent_runtime/migrations:/migrations/agent:ro" in compose
    assert "127.0.0.1:${OBSERVER_POSTGRES_PORT:-5432}:5432" in compose
    assert "kingdee" not in "\n".join(
        line.lower()
        for line in compose.splitlines()
        if "gate4-db-migrate" in line or "/migrations/agent" in line
    )


def test_gate4_integration_runner_enables_only_local_postgres_tests() -> None:
    script = (ROOT / "scripts" / "dev" / "test-gate4-integration").read_text(encoding="utf-8")

    assert "GBOS_RUN_GATE4_POSTGRES_INTEGRATION=1" in script
    assert "GBOS_GATE4_AGENT_USER=gbos_agent_app" in script
    assert "GBOS_GATE4_CONTEXT_USER=gbos_context_app" in script
    assert "tests/integration/test_gate4_postgres.py" in script
    assert "GBOS_PRODUCTION_ENABLED=false" in script
    assert "GBOS_GATE4_OWNER_USER" in script


def test_ci_runs_gate4_types_and_live_postgres_integration() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "services/action_guard" in workflow
    assert "services/agent_runtime" in workflow
    assert "scripts/dev/test-gate4-integration" in workflow
