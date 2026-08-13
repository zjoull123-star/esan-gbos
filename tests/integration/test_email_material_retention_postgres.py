from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")
POSTGRES_DATABASE = os.getenv("GBOS_GATE3_CONTEXT_DATABASE")
ROOT = Path(__file__).parents[2]
MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "019_email_material_retention_tombstones.sql"
)

pytestmark = [pytest.mark.postgres_integration]
if not RUN_INTEGRATION:
    pytestmark.append(pytest.mark.skip(reason="set GBOS_RUN_POSTGRES_INTEGRATION=1 to run"))


def _sql(sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    if not POSTGRES_CONTAINER:
        pytest.fail("GBOS_GATE3_POSTGRES_CONTAINER is required for this integration test")
    return subprocess.run(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "sh",
            "-eu",
            "-c",
            (
                'target_db="${2:-$POSTGRES_DB}"; '
                'PGPASSWORD="$POSTGRES_PASSWORD" exec psql '
                '-U "$POSTGRES_USER" -d "$target_db" -Atq '
                '-v ON_ERROR_STOP=1 -c "$1"'
            ),
            "sh",
            sql,
            POSTGRES_DATABASE or "",
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def test_migration_runs_twice_with_forced_rls_functions_and_no_direct_mutation() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    _sql(migration)
    _sql(migration)

    security = _sql(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               has_table_privilege('gbos_observer_app', c.oid, 'SELECT'),
               has_table_privilege('gbos_observer_app', c.oid, 'INSERT'),
               has_table_privilege('gbos_observer_app', c.oid, 'UPDATE'),
               has_table_privilege('gbos_observer_app', c.oid, 'DELETE')
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname = 'observer'
           AND c.relname IN (
               'email_material_retention_requests',
               'email_material_retention_work',
               'email_material_tombstone_receipts',
               'email_material_legal_hold_events'
           )
         ORDER BY c.relname
        """
    )
    assert security.stdout.strip().splitlines() == [
        "email_material_legal_hold_events|t|t|t|f|f|f",
        "email_material_retention_requests|t|t|t|f|f|f",
        "email_material_retention_work|t|t|t|f|f|f",
        "email_material_tombstone_receipts|t|t|t|f|f|f",
    ]


def test_registration_claim_completion_and_receipt_are_revision_pinned_and_immutable() -> None:
    result = _sql(
        """
        INSERT INTO observer.email_draft_evidence_bindings
            (site_id, purpose, inbox_item_ref, draft_ref, draft_revision,
             evidence_ref, object_ref, digest, media_type, byte_size, created_at)
        VALUES (
            'email-retention.example', 'email_draft_material',
            'INB-01ARZ3NDEKTSV4RRFFQ69G5FAV',
            'DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV', 3,
            'EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV',
            'obs:v1:' || repeat('a', 32) || ':sha256:' || repeat('b', 64),
            'sha256:' || repeat('b', 64), 'text/plain; charset=utf-8', 10,
            current_timestamp - interval '31 days'
        ) ON CONFLICT DO NOTHING;

        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SELECT set_config('app.site_id', 'email-retention.example', true);
        SELECT material_kind, draft_revision, digest,
               not_before = terminal_at + interval '30 days'
          FROM observer.register_email_material_retention(
              'email-retention.example', 'email_draft_material',
              'EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV', 'discarded',
              current_timestamp - interval '31 days',
              current_timestamp - interval '1 day',
              'ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV',
              'DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV', 3
          );
        SELECT material_kind, lease_generation, digest
          FROM observer.claim_email_material_retention(
              'email-retention.example', 'observer-retention-test',
              current_timestamp, current_timestamp + interval '5 minutes', 10
          );
        SELECT material_kind, draft_revision, digest, deleted_at >= not_before
          FROM observer.complete_email_material_retention(
              'email-retention.example',
              (SELECT request_ref FROM observer.email_material_retention_requests
                WHERE evidence_ref = 'EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV'),
              'observer-retention-test', 1,
              'TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV', current_timestamp
          );
        SELECT material_kind, draft_revision, digest
          FROM observer.resolve_email_material_tombstone(
              'email-retention.example',
              'EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV',
              'TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV'
          );
        COMMIT;
        """
    )
    lines = result.stdout.strip().splitlines()
    assert lines[-4:] == [
        "draft|3|sha256:" + "b" * 64 + "|t",
        "draft|1|sha256:" + "b" * 64,
        "draft|3|sha256:" + "b" * 64 + "|t",
        "draft|3|sha256:" + "b" * 64,
    ]

    immutable = _sql(
        """
        UPDATE observer.email_material_tombstone_receipts
           SET deleted_at = deleted_at + interval '1 second'
         WHERE site_id = 'email-retention.example';
        """,
        check=False,
    )
    assert immutable.returncode != 0
    assert "email_material_retention_immutable" in immutable.stderr


def test_legal_hold_and_unregistered_shared_object_block_claim() -> None:
    result = _sql(
        """
        INSERT INTO observer.email_draft_evidence_bindings
            (site_id, purpose, inbox_item_ref, draft_ref, draft_revision,
             evidence_ref, object_ref, digest, media_type, byte_size, created_at)
        VALUES
            ('email-retention-hold.example', 'email_draft_material',
             'INB-01BX5ZZKBKACTAV9WEVGEMMVRZ',
             'DRF-01BX5ZZKBKACTAV9WEVGEMMVRZ', 1,
             'EVR-01BX5ZZKBKACTAV9WEVGEMMVRZ',
             'obs:v1:' || repeat('c', 32) || ':sha256:' || repeat('d', 64),
             'sha256:' || repeat('d', 64), 'text/plain; charset=utf-8', 10,
             current_timestamp - interval '31 days'),
            ('email-retention-hold.example', 'email_draft_material',
             'INB-01D78XYFJ1PRM1WPBCBT3VHMNV',
             'DRF-01D78XYFJ1PRM1WPBCBT3VHMNV', 1,
             'EVR-01D78XYFJ1PRM1WPBCBT3VHMNV',
             'obs:v1:' || repeat('c', 32) || ':sha256:' || repeat('d', 64),
             'sha256:' || repeat('d', 64), 'text/plain; charset=utf-8', 10,
             current_timestamp - interval '31 days')
        ON CONFLICT DO NOTHING;

        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SELECT set_config('app.site_id', 'email-retention-hold.example', true);
        SELECT request_ref IS NOT NULL
          FROM observer.register_email_material_retention(
              'email-retention-hold.example', 'email_draft_material',
              'EVR-01BX5ZZKBKACTAV9WEVGEMMVRZ', 'sent',
              current_timestamp - interval '31 days',
              current_timestamp - interval '1 day',
              'ETA-01BX5ZZKBKACTAV9WEVGEMMVRZ',
              'DRF-01BX5ZZKBKACTAV9WEVGEMMVRZ', 1
          );
        SELECT count(*) FROM observer.claim_email_material_retention(
            'email-retention-hold.example', 'observer-retention-test',
            current_timestamp, current_timestamp + interval '5 minutes', 10
        );
        SELECT observer.record_email_material_legal_hold_event(
            'email-retention-hold.example', 'EVR-01BX5ZZKBKACTAV9WEVGEMMVRZ',
            'HLD-01ARZ3NDEKTSV4RRFFQ69G5FAV', 1, 'placed',
            current_timestamp, 'legal_review'
        );
        SELECT observer.email_material_has_legal_hold(
            'email-retention-hold.example', 'EVR-01BX5ZZKBKACTAV9WEVGEMMVRZ',
            current_timestamp
        );
        SELECT count(*) FROM observer.claim_email_material_retention(
            'email-retention-hold.example', 'observer-retention-test',
            current_timestamp, current_timestamp + interval '5 minutes', 10
        );
        ROLLBACK;
        """
    )
    assert result.stdout.strip().splitlines()[-5:] == ["t", "0", "t", "t", "0"]
