from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_storage import (
    DeliveryConflict,
    JobConflict,
    PostgresLocalPilotStorage,
)
from services.observer.observer.models import (
    ConnectorKey,
    RawDelivery,
    TenantScope,
)
from services.observer.observer.storage import connect_postgres_components

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")
CONTEXT_HOST = os.getenv("GBOS_GATE3_CONTEXT_HOST")
CONTEXT_PORT = os.getenv("GBOS_GATE3_CONTEXT_PORT")
CONTEXT_DATABASE = os.getenv("GBOS_GATE3_CONTEXT_DATABASE")
CONTEXT_PASSWORD = os.getenv("GBOS_GATE3_CONTEXT_PASSWORD")
ROOT = Path(__file__).parents[2]
INGESTION_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "004_local_pilot_ingestion.sql"
)

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
    ingestion_ledger = _container_sql(
        """
        SELECT count(*)
        FROM observer.schema_migrations
        WHERE migration_name = 'observer/004_local_pilot_ingestion.sql'
        """
    )
    assert ingestion_ledger.stdout.strip() == "1"

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


def test_local_pilot_ingestion_migration_can_run_twice_and_keeps_rls_forced() -> None:
    sql = INGESTION_MIGRATION.read_text(encoding="utf-8")

    _container_sql(sql)
    _container_sql(sql)

    result = _container_sql(
        """
        SELECT
          count(*) FILTER (
            WHERE attribute.attname IN (
              'object_ref', 'byte_size', 'idempotency_key', 'generation',
              'lease_owner', 'lease_expires_at', 'lease_generation'
            )
          ),
          bool_and(relation.relrowsecurity AND relation.relforcerowsecurity)
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_attribute AS attribute ON attribute.attrelid = relation.oid
        WHERE namespace.nspname = 'observer'
          AND relation.relname IN ('inbound_deliveries', 'processing_jobs')
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        """
    )
    columns, rls_forced = result.stdout.strip().split("|")
    assert columns == "7"
    assert rls_forced == "t"


def test_processing_job_lease_fencing_pause_and_terminal_delivery_state() -> None:
    suffix = uuid.uuid4().hex[:12]
    site = f"ingestion-{suffix}"
    instance = f"instance-{suffix}"
    delivery = f"delivery-{suffix}"
    job = f"job-{suffix}"
    digest = "a" * 64
    object_ref = f"obs:v1:{suffix}:sha256:{digest}"
    idempotency_key = f"delivery:{suffix}"

    _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        INSERT INTO observer.connector_instances (
          site_id, connector, connector_instance_id, status,
          registered_at, updated_at
        ) VALUES (
          '{site}', 'wecom', '{instance}', 'paused', now(), now()
        );
        INSERT INTO observer.inbound_deliveries (
          site_id, connector, connector_instance_id, delivery_id,
          exact_body_sha256, object_ref, byte_size, media_type, received_at,
          processing_status, attempt_count, correlation_id, created_at, updated_at
        ) VALUES (
          '{site}', 'wecom', '{instance}', '{delivery}', '{digest}',
          '{object_ref}', 7, 'application/octet-stream', now(), 'queued', 0,
          'corr-{suffix}', now(), now()
        );
        INSERT INTO observer.processing_jobs (
          site_id, job_id, connector, connector_instance_id, delivery_id,
          stage, status, attempt_count, max_attempts, idempotency_key,
          generation, lease_generation, created_at, updated_at
        ) VALUES (
          '{site}', '{job}', 'wecom', '{instance}', '{delivery}',
          'normalize', 'queued', 0, 3, '{idempotency_key}', 0, 0, now(), now()
        );
        COMMIT;
        """
    )

    paused = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        SELECT count(*)
        FROM observer.processing_jobs AS candidate
        JOIN observer.connector_instances AS instance
          ON instance.site_id = candidate.site_id
         AND instance.connector = candidate.connector
         AND instance.connector_instance_id = candidate.connector_instance_id
        WHERE candidate.site_id = '{site}'
          AND candidate.status = 'queued'
          AND instance.status <> 'paused';
        COMMIT;
        """
    )
    assert paused.stdout.splitlines()[-1] == "0"

    fenced = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        UPDATE observer.connector_instances
        SET status = 'healthy', updated_at = now()
        WHERE site_id = '{site}' AND connector_instance_id = '{instance}';
        UPDATE observer.processing_jobs
        SET status = 'processing',
            attempt_count = attempt_count + 1,
            lease_generation = lease_generation + 1,
            lease_owner = 'worker-new',
            lease_expires_at = now() + interval '1 minute',
            updated_at = now()
        WHERE site_id = '{site}' AND job_id = '{job}';
        WITH stale AS (
          UPDATE observer.processing_jobs
          SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL
          WHERE site_id = '{site}'
            AND job_id = '{job}'
            AND status = 'processing'
            AND lease_owner = 'worker-old'
            AND attempt_count = 0
            AND lease_generation = 0
          RETURNING job_id
        )
        SELECT count(*) FROM stale;
        COMMIT;
        """
    )
    assert fenced.stdout.splitlines()[-1] == "0"

    _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        UPDATE observer.inbound_deliveries
        SET processing_status = 'processing', updated_at = now()
        WHERE site_id = '{site}' AND delivery_id = '{delivery}';
        UPDATE observer.inbound_deliveries
        SET processing_status = 'succeeded', updated_at = now()
        WHERE site_id = '{site}' AND delivery_id = '{delivery}';
        COMMIT;
        """
    )
    terminal_regression = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        UPDATE observer.inbound_deliveries
        SET processing_status = 'processing', updated_at = now()
        WHERE site_id = '{site}' AND delivery_id = '{delivery}';
        COMMIT;
        """,
        check=False,
    )
    assert terminal_regression.returncode != 0


def test_postgres_storage_runs_durable_delivery_job_lifecycle(tmp_path: Path) -> None:
    if not all((CONTEXT_HOST, CONTEXT_PORT, CONTEXT_DATABASE, CONTEXT_PASSWORD)):
        pytest.fail("Gate 3 Observer app-role connection components are required")
    connection = connect_postgres_components(
        host=str(CONTEXT_HOST),
        port=int(str(CONTEXT_PORT)),
        database=str(CONTEXT_DATABASE),
        user="gbos_observer_app",
        password=str(CONTEXT_PASSWORD),
    )
    try:
        suffix = uuid.uuid4().hex[:12]
        now = datetime.now(UTC)
        scope = TenantScope(f"lifecycle-{suffix}", "observation_processing")
        other_scope = TenantScope(f"other-{suffix}", "observation_processing")
        key = ConnectorKey("wecom", f"instance-{suffix}")
        repository = PostgresLocalPilotStorage(connection)
        repository.register_connector_instance(scope, key, now=now)
        inbox = DurableDeliveryInbox(
            storage=repository,
            evidence_store=ContentAddressedEvidenceStore(tmp_path / "objects"),
        )
        raw = RawDelivery(
            f"delivery-{suffix}",
            b"\xff\x00durable",
            "application/octet-stream",
            now,
        )

        accepted = inbox.accept(
            scope,
            key,
            raw,
            correlation_id=f"corr-{suffix}",
            max_attempts=2,
        )
        replayed_accept = inbox.accept(
            scope,
            key,
            raw,
            correlation_id=f"corr-{suffix}",
            max_attempts=2,
        )
        assert replayed_accept == accepted
        with pytest.raises(DeliveryConflict, match="content metadata"):
            inbox.accept(
                scope,
                key,
                RawDelivery(
                    raw.delivery_id,
                    b"different",
                    raw.media_type,
                    now,
                ),
                correlation_id=f"corr-{suffix}",
                max_attempts=2,
            )

        repository.set_connector_status(scope, key, status="paused", now=now)
        assert (
            repository.claim_processing_job(
                scope,
                worker_id="worker-a",
                now=now,
                lease_seconds=30,
            )
            is None
        )
        repository.set_connector_status(
            scope,
            key,
            status="healthy",
            now=now + timedelta(seconds=1),
        )
        first = repository.claim_processing_job(
            scope,
            worker_id="worker-a",
            now=now + timedelta(seconds=1),
            lease_seconds=30,
        )
        assert first is not None
        with pytest.raises(JobConflict, match="lease"):
            repository.heartbeat_processing_job(
                scope,
                job_id=first.job_id,
                worker_id="stale-worker",
                expected_attempt=first.attempt_count + 1,
                expected_lease_generation=first.lease_generation + 1,
                now=now + timedelta(seconds=2),
                lease_seconds=30,
            )
        retried = repository.retry_processing_job(
            scope,
            job_id=first.job_id,
            worker_id="worker-a",
            expected_attempt=first.attempt_count,
            expected_lease_generation=first.lease_generation,
            now=now + timedelta(seconds=2),
            next_retry_at=now + timedelta(seconds=3),
            error_code="temporary_failure",
        )
        assert retried.status == "retry_wait"

        second = repository.claim_processing_job(
            scope,
            worker_id="worker-b",
            now=now + timedelta(seconds=3),
            lease_seconds=30,
        )
        assert second is not None
        dead = repository.retry_processing_job(
            scope,
            job_id=second.job_id,
            worker_id="worker-b",
            expected_attempt=second.attempt_count,
            expected_lease_generation=second.lease_generation,
            now=now + timedelta(seconds=4),
            next_retry_at=now + timedelta(seconds=5),
            error_code="retry_exhausted",
        )
        assert dead.status == "dead_letter"

        replay = repository.replay_delivery(
            scope,
            key,
            delivery_id=raw.delivery_id,
            job_id=f"replay-{suffix}",
            idempotency_key=f"replay:ticket-{suffix}",
            now=now + timedelta(seconds=5),
            max_attempts=2,
        )
        same_replay = repository.replay_delivery(
            scope,
            key,
            delivery_id=raw.delivery_id,
            job_id=f"ignored-{suffix}",
            idempotency_key=f"replay:ticket-{suffix}",
            now=now + timedelta(seconds=6),
            max_attempts=2,
        )
        assert replay == same_replay
        assert replay.generation == 1
        with pytest.raises(DeliveryConflict, match="not found"):
            repository.get_inbound_delivery(
                other_scope,
                key,
                delivery_id=raw.delivery_id,
            )
    finally:
        connection.close()


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
