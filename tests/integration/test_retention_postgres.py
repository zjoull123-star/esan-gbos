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


def test_retention_migration_is_ledgered_and_forces_site_rls() -> None:
    result = _container_sql(
        """
        SELECT
          (SELECT count(*) FROM observer.schema_migrations
           WHERE migration_name = 'observer/011_local_pilot_retention.sql'),
          (SELECT count(*)
           FROM pg_class AS relation
           JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'observer'
             AND relation.relname IN ('retention_runs', 'retention_cas_tombstones')
             AND relation.relrowsecurity
             AND relation.relforcerowsecurity),
          (SELECT count(*)
           FROM pg_trigger AS trigger
           JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
           JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'observer'
             AND trigger.tgname IN (
               'raw_objects_retention_tombstone_guard',
               'evidence_refs_retention_tombstone_guard',
               'inbound_deliveries_retention_tombstone_guard'
             )
             AND NOT trigger.tgisinternal)
        """
    )
    assert result.stdout.strip() == "1|2|3"


def test_retention_preserves_holds_history_and_shared_cas_then_replays_lease() -> None:
    suffix = uuid.uuid4().hex[:12]
    site = f"retention-{suffix}"
    shared_ref = f"obs:v1:{suffix}:sha256:{'a' * 64}"
    orphan_ref = f"obs:v1:{suffix}:sha256:{'b' * 64}"
    historical_ref = f"obs:v1:{suffix}:sha256:{'c' * 64}"
    held_ref = f"obs:v1:{suffix}:sha256:{'d' * 64}"

    result = _container_sql(
        f"""
        INSERT INTO observer.raw_objects (
          site_id, object_id, object_ref, sha256, media_type,
          byte_size, retention_class, created_at
        ) VALUES
          ('{site}', 'raw-shared', '{shared_ref}', '{"a" * 64}',
           'text/plain', 1, 'R1-operational', '2026-01-01T00:00:00Z'),
          ('{site}', 'raw-orphan', '{orphan_ref}', '{"b" * 64}',
           'text/plain', 1, 'R1-operational', '2026-01-01T00:00:00Z'),
          ('{site}', 'raw-history', '{historical_ref}', '{"c" * 64}',
           'text/plain', 1, 'R1-operational', '2026-01-01T00:00:00Z'),
          ('{site}', 'raw-held', '{held_ref}', '{"d" * 64}',
           'text/plain', 1, 'R1-operational', '2026-01-01T00:00:00Z');

        INSERT INTO observer.observation_events (
          site_id, event_id, raw_object_id, connector, channel,
          processing_purpose, consent_basis, data_classification,
          retention_class, correlation_id, occurred_at, ingested_at, document
        ) VALUES
          ('{site}', 'event-shared-expired', 'raw-shared', 'manual_import', 'manual_import',
           'audit_compliance', 'legal_obligation', 'Restricted', 'R1-operational',
           'corr-1', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{{}}'),
          ('{site}', 'event-shared-retained', 'raw-shared', 'manual_import', 'manual_import',
           'audit_compliance', 'legal_obligation', 'Restricted', 'R1-operational',
           'corr-2', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z', '{{}}'),
          ('{site}', 'event-orphan', 'raw-orphan', 'manual_import', 'manual_import',
           'audit_compliance', 'legal_obligation', 'Restricted', 'R1-operational',
           'corr-3', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{{}}'),
          ('{site}', 'event-history', 'raw-history', 'manual_import', 'manual_import',
           'audit_compliance', 'legal_obligation', 'Restricted', 'R1-operational',
           'corr-4', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{{}}'),
          ('{site}', 'event-held', 'raw-held', 'manual_import', 'manual_import',
           'audit_compliance', 'legal_obligation', 'Restricted', 'R1-operational',
           'corr-5', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '{{}}');

        INSERT INTO observer.evidence_refs (
          site_id, evidence_id, event_id, raw_object_id, raw_sha256,
          media_type, locator, created_at, content_object_ref
        ) VALUES
          ('{site}', 'evidence-shared-expired', 'event-shared-expired', 'raw-shared',
           '{"a" * 64}', 'text/plain', '{{}}', '2026-01-01T00:00:00Z', '{shared_ref}'),
          ('{site}', 'evidence-shared-retained', 'event-shared-retained', 'raw-shared',
           '{"a" * 64}', 'text/plain', '{{}}', '2026-08-01T00:00:00Z', '{shared_ref}'),
          ('{site}', 'evidence-orphan', 'event-orphan', 'raw-orphan',
           '{"b" * 64}', 'text/plain', '{{}}', '2026-01-01T00:00:00Z', '{orphan_ref}'),
          ('{site}', 'evidence-history', 'event-history', 'raw-history',
           '{"c" * 64}', 'text/plain', '{{}}', '2026-01-01T00:00:00Z', '{historical_ref}'),
          ('{site}', 'evidence-held', 'event-held', 'raw-held',
           '{"d" * 64}', 'text/plain', '{{}}', '2026-01-01T00:00:00Z', '{held_ref}');

        INSERT INTO observer.legal_holds (
          site_id, hold_id, evidence_id, owner_ref, reason, started_at
        ) VALUES (
          '{site}', 'hold-1', 'evidence-held', 'legal-team',
          'regulated preservation', '2026-02-01T00:00:00Z'
        );
        INSERT INTO context.evidence_records (
          site_id, evidence_record_id, observer_evidence_id,
          processing_purpose, idempotency_key, payload_digest,
          review_status, data_classification, document, recorded_at
        ) VALUES (
          '{site}', 'history-1', 'evidence-history', 'audit_compliance',
          'history-idem-1', '{"e" * 64}', 'accepted', 'Restricted', '{{}}',
          '2026-02-01T00:00:00Z'
        );

        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        SELECT * FROM observer.preview_retention_batch(
          '{site}', '2026-08-11T04:00:00Z', 100
        );
        SELECT * FROM observer.claim_retention_run(
          '{site}', 'run-1', 'worker-1',
          '2026-08-11T04:00:00Z', '2026-08-11T04:10:00Z'
        );
        SELECT * FROM observer.expire_retention_metadata(
          '{site}', 'run-1', 'worker-1', 1,
          '2026-08-11T04:00:00Z', 100
        );
        COMMIT;

        SELECT
          (SELECT count(*) FROM observer.observation_events
           WHERE site_id = '{site}'
             AND event_id IN ('event-shared-expired', 'event-orphan')),
          (SELECT count(*) FROM observer.observation_events
           WHERE site_id = '{site}'
             AND event_id IN ('event-shared-retained', 'event-held', 'event-history')),
          (SELECT count(*) FROM context.evidence_records
           WHERE site_id = '{site}' AND evidence_record_id = 'history-1'),
          (SELECT count(*) FROM observer.retention_cas_tombstones
           WHERE site_id = '{site}' AND object_ref = '{orphan_ref}'),
          (SELECT count(*) FROM observer.retention_cas_tombstones
           WHERE site_id = '{site}' AND object_ref = '{shared_ref}');

        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        SELECT lease_generation FROM observer.claim_retention_cas_deletions(
          '{site}', 'run-1', 'worker-1', 1,
          '2026-08-11T04:00:00Z', '2026-08-11T04:01:00Z', 100
        );
        SELECT lease_generation FROM observer.claim_retention_cas_deletions(
          '{site}', 'run-1', 'worker-1', 1,
          '2026-08-11T04:02:00Z', '2026-08-11T04:03:00Z', 100
        );
        COMMIT;
        """
    )
    lines = result.stdout.strip().splitlines()
    assert "4|2|1|1" in lines
    assert "4|2|1|1|2" in lines
    assert "0|3|1|1|0" in lines
    assert lines[-2:] == ["1", "2"]

    blocked = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SET LOCAL app.site_id = '{site}';
        INSERT INTO observer.raw_objects (
          site_id, object_id, object_ref, sha256, media_type,
          byte_size, retention_class, created_at
        ) VALUES (
          '{site}', 'raw-reinserted', '{orphan_ref}', '{"b" * 64}',
          'text/plain', 1, 'R1-operational', '2026-08-11T04:00:00Z'
        );
        COMMIT;
        """,
        check=False,
    )
    assert blocked.returncode != 0
    assert "retention tombstoned" in blocked.stderr
