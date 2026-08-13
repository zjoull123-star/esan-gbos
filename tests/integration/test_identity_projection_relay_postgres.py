from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")
POSTGRES_DATABASE = os.getenv("GBOS_GATE3_CONTEXT_DATABASE")
ROOT = Path(__file__).parents[2]
OBSERVER_MIGRATION = ROOT / "services/observer/migrations/020_identity_projection_outbox.sql"
GATEWAY_MIGRATION = (
    ROOT / "services/email_gateway/migrations/012_identity_projection_purpose_scope.sql"
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
                '-U "${POSTGRES_USER:-postgres}" -d "$target_db" -Atq '
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


def test_identity_projection_migrations_run_twice_with_rls_and_minimal_grants() -> None:
    for migration in (OBSERVER_MIGRATION, GATEWAY_MIGRATION):
        sql = migration.read_text(encoding="utf-8")
        _sql(sql)
        _sql(sql)

    security = _sql(
        """
        SELECT
          (SELECT relrowsecurity::text || ':' || relforcerowsecurity::text
             FROM pg_class
            WHERE oid = 'observer.identity_projection_outbox'::regclass),
          (SELECT relrowsecurity::text || ':' || relforcerowsecurity::text
             FROM pg_class
            WHERE oid = 'email_gateway.identity_projection_receipts'::regclass),
          has_table_privilege(
              'gbos_observer_identity_projector',
              'observer.identity_projection_outbox', 'SELECT'
          ),
          has_table_privilege(
              'gbos_observer_identity_projector',
              'observer.identity_projection_outbox', 'INSERT'
          ),
          has_column_privilege(
              'gbos_observer_identity_projector',
              'observer.identity_projection_outbox', 'relay_status', 'UPDATE'
          ),
          has_column_privilege(
              'gbos_observer_identity_projector',
              'observer.identity_projection_outbox', 'payload', 'UPDATE'
          ),
          has_table_privilege(
              'gbos_email_gateway_app',
              'email_gateway.identity_projection_receipts', 'SELECT,INSERT'
          ),
          has_table_privilege(
              'gbos_email_gateway_app',
              'email_gateway.identity_projection_receipts', 'UPDATE,DELETE'
          )
        """
    )
    assert security.stdout.strip() == "true:true|true:true|t|f|t|f|t|f"


def test_gateway_revision_uniqueness_includes_exact_business_purpose() -> None:
    result = _sql(
        """
        BEGIN;
        INSERT INTO email_gateway.identity_projection_receipts (
            site_id, processing_purpose, opaque_address_ref,
            external_identity_ref, external_identity_revision, identity_type,
            team_ref, status, projection_receipt_ref, observed_at, payload_digest
        ) VALUES
        (
            'identity-purpose.example', 'sales_follow_up',
            'extid:v1:email:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE',
            'EID-01ARZ3NDEKTSV4RRFFQ69G5FAV', 7, 'Party',
            'TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV', 'confirmed',
            'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '2026-08-14T12:00:00Z',
            'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'
        ),
        (
            'identity-purpose.example', 'customer_service',
            'extid:v1:email:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE',
            'EID-01ARZ3NDEKTSV4RRFFQ69G5FAV', 7, 'Party',
            'TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV', 'confirmed',
            'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            '2026-08-14T12:00:00Z',
            'sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
        );
        SELECT count(*)
          FROM email_gateway.identity_projection_receipts
         WHERE site_id = 'identity-purpose.example'
           AND opaque_address_ref =
               'extid:v1:email:EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE'
           AND external_identity_revision = 7;
        ROLLBACK;
        """
    )
    assert result.stdout.strip() == "2"


def test_resolution_and_outbox_write_roll_back_together_on_closed_payload() -> None:
    failed = _sql(
        """
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SELECT set_config('app.site_id', 'identity-atomic.example', true);
        INSERT INTO observer.participant_identity_resolutions (
            site_id, identity_provider, external_subject_ref, mapping_ref,
            mapping_revision, team_ref, target_type, target_ref, status,
            resolved_at, recorded_at
        ) VALUES (
            'identity-atomic.example', 'email',
            'extid:v1:email:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF',
            'EID-01ARZ3NDEKTSV4RRFFQ69G5FAV', 1,
            'TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV', 'Party',
            'protected-atomic-target@example.invalid', 'confirmed',
            '2026-08-14T12:00:00Z', '2026-08-14T12:00:00Z'
        );
        INSERT INTO observer.identity_projection_outbox (
            site_id, processing_purpose, opaque_address_ref,
            external_identity_revision, projection_receipt, payload,
            payload_digest, next_attempt_at, created_at, updated_at
        ) VALUES (
            'identity-atomic.example', 'sales_follow_up',
            'extid:v1:email:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF', 1,
            'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            '{"unexpected":"closed"}'::jsonb,
            'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            '2026-08-14T12:00:00Z', '2026-08-14T12:00:00Z',
            '2026-08-14T12:00:00Z'
        );
        COMMIT;
        """,
        check=False,
    )
    assert failed.returncode != 0
    remaining = _sql(
        """
        SELECT count(*)
          FROM observer.participant_identity_resolutions
         WHERE site_id = 'identity-atomic.example'
           AND external_subject_ref =
               'extid:v1:email:FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF'
        """
    )
    assert remaining.stdout.strip() == "0"
