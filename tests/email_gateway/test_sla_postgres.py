from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.mark.postgres_integration
def test_sla_authority_migration_runs_twice_on_disposable_postgres() -> None:
    if os.environ.get("GBOS_RUN_EMAIL_GATEWAY_POSTGRES") != "1":
        pytest.skip("set GBOS_RUN_EMAIL_GATEWAY_POSTGRES=1 for disposable PostgreSQL")
    import psycopg

    root = Path(__file__).resolve().parents[2]
    migrations = sorted(
        path
        for path in (root / "services/email_gateway/migrations").glob("*.sql")
        if path.name[:3].isdigit() and int(path.name[:3]) <= 10
    )
    assert migrations[-1].name == "010_email_gateway_sla_authority.sql"
    with psycopg.connect(os.environ["GBOS_EMAIL_GATEWAY_POSTGRES_DSN"], autocommit=True) as db:
        for _ in range(2):
            for migration in migrations:
                db.execute(migration.read_text(encoding="utf-8"))
        columns = {
            str(row[0])
            for row in db.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'email_gateway'
                   AND table_name = 'inbox_authority_receipts'
                """
            ).fetchall()
        }
        assert {"actor_ref_digest", "target_user_ref_digest"} <= columns
        assert not {"actor_ref", "target_user_ref"} & columns
        functions = {
            str(row[0])
            for row in db.execute(
                """
                SELECT proname
                  FROM pg_proc AS p
                  JOIN pg_namespace AS n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'email_gateway'
                """
            ).fetchall()
        }
        assert {
            "start_inbox_sla_clock",
            "complete_sla_from_provider_receipt",
            "apply_inbox_sla_operation",
        } <= functions

        received_at = datetime(2026, 8, 13, tzinfo=UTC)
        db.execute(
            """
            INSERT INTO email_gateway.mailboxes (
                site_id, mailbox_ref, address_display_ciphertext, provider,
                provider_account_ref, observer_connector_instance_ref, entry_role,
                business_purpose, default_team_ref, account_owner_user_ref,
                credential_ref, created_by, updated_by
            ) VALUES (
                'sla.example', 'MBX-SLA', %s, 'fake', 'provider-sla', 'observer-sla',
                'primary', 'sales_follow_up', 'TEM-SLA', 'owner-sla',
                'credential-sla', 'test', 'test'
            )
            """,
            (b"encrypted",),
        )
        db.execute(
            """
            INSERT INTO email_gateway.channel_messages (
                site_id, message_ref, direction, received_at, subject_digest,
                message_id_digest, evidence_refs, provider
            ) VALUES (
                'sla.example', 'MSG-SLA', 'inbound', %s, %s, %s,
                ARRAY['EVD-SLA'], 'fake'
            )
            """,
            (received_at, "sha256:" + "1" * 64, "sha256:" + "2" * 64),
        )
        db.execute(
            """
            INSERT INTO email_gateway.mailbox_sla_policies (
                site_id, mailbox_ref, policy_ref, revision,
                first_response_duration_seconds, effective_at, request_id,
                idempotency_key, payload_digest
            ) VALUES (
                'sla.example', 'MBX-SLA', 'SLA-01ARZ3NDEKTSV4RRFFQ69G5FAV', 1,
                3600, %s, 'REQ-POLICY', 'policy-1', %s
            )
            """,
            (received_at - timedelta(minutes=1), "sha256:" + "3" * 64),
        )
        db.execute(
            """
            INSERT INTO email_gateway.inbox_items (
                site_id, inbox_item_ref, mailbox_ref, message_ref, team_ref,
                state, received_at, updated_at
            ) VALUES (
                'sla.example', 'INB-SLA', 'MBX-SLA', 'MSG-SLA', 'TEM-SLA',
                'unassigned', %s, %s
            )
            """,
            (received_at, received_at),
        )
        started = db.execute(
            """
            SELECT started_at, due_at, status
              FROM email_gateway.inbox_sla_clocks
             WHERE site_id = 'sla.example' AND inbox_item_ref = 'INB-SLA'
            """
        ).fetchone()
        assert started == (received_at, received_at + timedelta(hours=1), "running")

        db.execute("SET ROLE gbos_email_gateway_app")
        db.execute("SELECT set_config('gbos.site_id', 'sla.example', false)")
        db.execute("SELECT set_config('gbos.processing_purpose', 'sales_follow_up', false)")
        revised = db.execute(
            """
            SELECT email_gateway.apply_inbox_sla_operation(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                "sla.example",
                "sales_follow_up",
                "INB-SLA",
                1,
                "assigned",
                "sales@example.invalid",
                [],
                received_at + timedelta(minutes=1),
                "SLA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
                1,
                received_at,
                received_at + timedelta(hours=1),
                "running",
                None,
                None,
                None,
                None,
                1,
                "claim",
                "inbox_claimed",
                "AUD-SLA",
                "sales@example.invalid",
                "sha256:" + "4" * 64,
                "REQ-CLAIM",
                "claim-1",
                "sha256:" + "5" * 64,
                "AUR-SLA",
                "sha256:" + "6" * 64,
                None,
                None,
            ),
        ).fetchone()
        assert revised == (2,)
        db.execute("RESET ROLE")
        durable = db.execute(
            """
            SELECT inbox.revision, sla.started_at, sla.due_at, sla.status,
                   authority.actor_ref_digest, authority.target_user_ref_digest
              FROM email_gateway.inbox_items AS inbox
              JOIN email_gateway.inbox_sla_clocks AS sla USING (site_id, inbox_item_ref)
              JOIN email_gateway.inbox_authority_receipts AS authority
                USING (site_id, inbox_item_ref)
             WHERE inbox.site_id = 'sla.example' AND inbox.inbox_item_ref = 'INB-SLA'
            """
        ).fetchone()
        assert durable == (
            2,
            received_at,
            received_at + timedelta(hours=1),
            "running",
            "sha256:" + "4" * 64,
            None,
        )
