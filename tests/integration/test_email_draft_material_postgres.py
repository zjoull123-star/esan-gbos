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
    ROOT / "services" / "observer" / "migrations" / "018_email_draft_material_evidence_binding.sql"
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


def test_migration_runs_twice_with_immutable_forced_rls_and_insert_select_only() -> None:
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
               'email_draft_material_receipts',
               'email_draft_evidence_bindings',
               'email_final_mime_evidence_bindings'
           )
         ORDER BY c.relname
        """
    )
    assert security.stdout.strip().splitlines() == [
        "email_draft_evidence_bindings|t|t|t|t|f|f",
        "email_draft_material_receipts|t|t|t|t|f|f",
        "email_final_mime_evidence_bindings|t|t|t|t|f|f",
    ]

    mutation = _sql(
        """
        BEGIN;
        SET LOCAL ROLE gbos_observer_app;
        SELECT set_config('app.site_id', 'draft-material-rls.example', true);
        INSERT INTO observer.email_draft_material_receipts
            (site_id, purpose, operation, idempotency_key,
             request_digest, response, created_at)
        VALUES ('draft-material-rls.example', 'email_draft_material', 'save',
                'draft-save-rls-01', 'sha256:' || repeat('a', 64),
                '{"evidence_ref":"EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV","digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","revision":1}'::jsonb,
                current_timestamp);
        SELECT set_config('app.site_id', 'other-draft-material-rls.example', true);
        DO $block$
        BEGIN
            IF (SELECT count(*) FROM observer.email_draft_material_receipts) <> 0 THEN
                RAISE EXCEPTION 'email_draft_material_rls_failed';
            END IF;
        END
        $block$;
        RESET ROLE;
        UPDATE observer.email_draft_material_receipts SET created_at = current_timestamp;
        ROLLBACK;
        """,
        check=False,
    )
    assert mutation.returncode != 0
    assert "email_draft_material_immutable" in mutation.stderr
