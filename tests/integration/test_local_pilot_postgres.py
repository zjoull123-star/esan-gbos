from __future__ import annotations

import os
import subprocess
import uuid

import pytest

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")

pytestmark = [pytest.mark.postgres_integration]
if not RUN_INTEGRATION:
    pytestmark.append(pytest.mark.skip(reason="set GBOS_RUN_POSTGRES_INTEGRATION=1 to run"))


def _container_sql(sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not POSTGRES_CONTAINER:
        pytest.fail(
            "GBOS_GATE3_POSTGRES_CONTAINER is required when GBOS_RUN_POSTGRES_INTEGRATION=1"
        )
    return subprocess.run(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "sh",
            "-eu",
            "-c",
            (
                'PGPASSWORD="$POSTGRES_PASSWORD" exec psql '
                '-U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq '
                '-v ON_ERROR_STOP=1 -c "$1"'
            ),
            "sh",
            sql,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def test_local_pilot_migration_is_ledgered_and_all_tables_force_rls() -> None:
    ledger = _container_sql(
        """
        SELECT count(*)
        FROM observer.schema_migrations
        WHERE migration_name = 'observer/003_local_pilot_runtime.sql'
        """
    )
    assert ledger.stdout.strip() == "1"

    rls = _container_sql(
        """
        SELECT count(*)
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'observer'
          AND relation.relname IN (
            'connector_instances', 'inbound_deliveries',
            'inbound_delivery_events', 'connector_checkpoints',
            'persistent_nonces', 'processing_jobs',
            'context_publication_outbox', 'local_pilot_quarantine',
            'local_pilot_dead_letter'
          )
          AND relation.relrowsecurity
          AND relation.relforcerowsecurity
        """
    )
    assert rls.stdout.strip() == "9"


def test_local_pilot_connector_instances_are_rls_isolated() -> None:
    suffix = uuid.uuid4().hex[:12]
    site_a = f"local-a-{suffix}"
    site_b = f"local-b-{suffix}"
    for site in (site_a, site_b):
        _container_sql(
            f"""
            BEGIN;
            SET LOCAL ROLE gbos_observer_app;
            SET LOCAL app.site_id = '{site}';
            INSERT INTO observer.connector_instances (
              site_id, connector, connector_instance_id, status,
              registered_at, updated_at
            ) VALUES (
              '{site}', 'wecom', 'sales-primary', 'healthy', now(), now()
            );
            COMMIT;
            """
        )

    visible = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site_a}';
        SELECT count(*)
        FROM observer.connector_instances
        WHERE connector_instance_id = 'sales-primary'
          AND site_id IN ('{site_a}', '{site_b}');
        COMMIT;
        """
    )
    assert visible.stdout.splitlines()[-1] == "1"

    cross_site = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site_a}';
        INSERT INTO observer.connector_instances (
          site_id, connector, connector_instance_id, status,
          registered_at, updated_at
        ) VALUES (
          '{site_b}', 'email', 'cross-site', 'healthy', now(), now()
        );
        COMMIT;
        """,
        check=False,
    )
    assert cross_site.returncode != 0
