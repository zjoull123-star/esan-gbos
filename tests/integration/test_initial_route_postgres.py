from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.email_gateway.models import IdentityProjection, TenantScope, stable_ref
from services.email_gateway.repositories.identity import PostgresIdentityProjectionRepository
from services.email_gateway.repositories.identity_route_work import (
    IdentityRouteLeaseLost,
    IdentityRouteWorkClaim,
    PostgresIdentityRouteWorkRepository,
)


def _enabled_dsn() -> str:
    if os.environ.get("GBOS_RUN_EMAIL_GATEWAY_POSTGRES") != "1":
        pytest.skip("set GBOS_RUN_EMAIL_GATEWAY_POSTGRES=1 for disposable PostgreSQL")
    return os.environ["GBOS_EMAIL_GATEWAY_POSTGRES_DSN"]


def _apply_migrations_twice(connection: object) -> None:
    root = Path(__file__).resolve().parents[2]
    migrations = sorted((root / "services/email_gateway/migrations").glob("*.sql"))
    for _ in range(2):
        for migration in migrations:
            connection.execute(migration.read_text(encoding="utf-8"))  # type: ignore[attr-defined]


def _insert_pending_inbox(
    connection: object,
    *,
    scope: TenantScope,
    suffix: str,
    opaque_address_ref: str,
    team_ref: str,
) -> str:
    inbox_ref = f"INB-{suffix}"
    message_ref = f"MSG-{suffix}"
    received_at = datetime.now(UTC)
    with connection.transaction():  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            "SELECT set_config('gbos.site_id', %s, true)", (scope.site_id,)
        )
        connection.execute(  # type: ignore[attr-defined]
            "SELECT set_config('gbos.processing_purpose', %s, true)",
            (scope.processing_purpose,),
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO email_gateway.channel_messages (
                site_id, message_ref, direction, received_at, subject_digest,
                message_id_digest, evidence_refs, provider
            ) VALUES (%s, %s, 'inbound', %s, %s, %s, ARRAY[%s], 'fake')
            """,
            (
                scope.site_id,
                message_ref,
                received_at,
                "sha256:" + suffix[0].lower() * 64,
                "sha256:" + suffix[-1].lower() * 64,
                f"EVD-{suffix}",
            ),
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO email_gateway.message_participants (
                site_id, message_ref, role, identity_ref, ordinal
            ) VALUES (%s, %s, 'from', %s, 1)
            """,
            (scope.site_id, message_ref, opaque_address_ref),
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO email_gateway.inbox_items (
                site_id, inbox_item_ref, mailbox_ref, message_ref, team_ref,
                state, received_at, updated_at
            ) VALUES (%s, %s, 'MBX-ROUTE', %s, %s, 'identity_pending', %s, %s)
            """,
            (
                scope.site_id,
                inbox_ref,
                message_ref,
                team_ref,
                received_at,
                received_at,
            ),
        )
    return inbox_ref


def _apply_unassigned_open_transaction(
    connection: object,
    *,
    scope: TenantScope,
    claim: IdentityRouteWorkClaim,
    inbox_ref: str,
    request_suffix: str,
) -> None:
    applied_at = datetime.now(UTC)
    request_id = f"REQ-{request_suffix}"
    idempotency_key = f"identity-route:{request_suffix}"
    connection.execute("BEGIN")  # type: ignore[attr-defined]
    connection.execute("SET LOCAL ROLE gbos_email_gateway_worker")  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "SELECT set_config('gbos.site_id', %s, true)", (scope.site_id,)
    )
    connection.execute(  # type: ignore[attr-defined]
        "SELECT set_config('gbos.processing_purpose', %s, true)",
        (scope.processing_purpose,),
    )
    revised = connection.execute(  # type: ignore[attr-defined]
        """
        SELECT email_gateway.apply_identity_route_fenced(
            %s, %s, %s, %s, %s, %s, %s, %s, 1,
            'unassigned', NULL, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            scope.site_id,
            scope.processing_purpose,
            claim.work_ref,
            claim.worker_id,
            claim.attempt,
            claim.generation,
            claim.fence_token,
            inbox_ref,
            applied_at,
            stable_ref("OPR", scope.site_id, request_suffix),
            request_id,
            idempotency_key,
            "sha256:" + "d" * 64,
            stable_ref("AUD", scope.site_id, request_suffix),
            f"audit:{request_suffix}",
        ),
    ).fetchone()
    assert revised == (2,)


def _claim(
    repository: PostgresIdentityRouteWorkRepository,
    scope: TenantScope,
    worker_id: str,
) -> IdentityRouteWorkClaim:
    claim = repository.claim(
        scope,
        worker_id=worker_id,
        now=datetime.now(UTC),
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    return claim


@pytest.mark.postgres_integration
def test_late_inbox_rearms_completed_work_without_repeating_prior_effects_or_deadlock() -> None:
    dsn = _enabled_dsn()
    import psycopg

    scope = TenantScope(
        f"initial-route-{datetime.now(UTC).strftime('%H%M%S%f')}.example",
        "sales_follow_up",
    )
    opaque_ref = "extid:v1:email:" + "R" * 43
    mapping_ref = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    team_ref = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    projection = IdentityProjection(
        site_id=scope.site_id,
        processing_purpose=scope.processing_purpose,
        opaque_address_ref=opaque_ref,
        external_identity_ref=mapping_ref,
        external_identity_revision=1,
        identity_type="Party",
        team_ref=team_ref,
        status="confirmed",
        projection_receipt_ref="sha256:" + "a" * 64,
        observed_at=datetime.now(UTC),
        payload_digest="sha256:" + "b" * 64,
    )

    with (
        psycopg.connect(dsn, autocommit=True) as admin,
        psycopg.connect(dsn, autocommit=True) as worker,
    ):
        _apply_migrations_twice(admin)
        admin.execute("SET ROLE gbos_email_gateway_app")
        assert PostgresIdentityProjectionRepository(admin).apply(scope, projection) == projection
        assert PostgresIdentityProjectionRepository(admin).apply(scope, projection) == projection
        admin.execute("RESET ROLE")
        assert admin.execute(
            """
            SELECT count(*), min(status)
              FROM email_gateway.identity_route_work
             WHERE site_id = %s AND processing_purpose = %s
            """,
            (scope.site_id, scope.processing_purpose),
        ).fetchone() == (1, "queued")

        admin.execute(
            """
            INSERT INTO email_gateway.mailboxes (
                site_id, mailbox_ref, address_display_ciphertext, provider,
                provider_account_ref, observer_connector_instance_ref, entry_role,
                business_purpose, default_team_ref, account_owner_user_ref,
                credential_ref, created_by, updated_by
            ) VALUES (
                %s, 'MBX-ROUTE', %s, 'fake', 'provider-route', 'observer-route',
                'primary', %s, %s, 'owner-route', 'credential-route', 'test', 'test'
            )
            """,
            (scope.site_id, b"encrypted", scope.processing_purpose, team_ref),
        )
        admin.execute(
            """
            INSERT INTO email_gateway.mailbox_sla_policies (
                site_id, mailbox_ref, policy_ref, revision,
                first_response_duration_seconds, effective_at, request_id,
                idempotency_key, payload_digest
            ) VALUES (
                %s, 'MBX-ROUTE', 'SLA-01ARZ3NDEKTSV4RRFFQ69G5FAV', 1,
                3600, %s, 'REQ-SLA', 'sla-1', %s
            )
            """,
            (scope.site_id, datetime.now(UTC) - timedelta(minutes=1), "sha256:" + "c" * 64),
        )

        worker.execute("SET ROLE gbos_email_gateway_worker")
        work = PostgresIdentityRouteWorkRepository(worker)
        zero_candidate_claim = _claim(work, scope, "initial-route-zero")
        assert (
            work.list_candidate_refs(scope, zero_candidate_claim, now=datetime.now(UTC), limit=10)
            == ()
        )
        work.complete(scope, zero_candidate_claim, now=datetime.now(UTC))

        first_ref = _insert_pending_inbox(
            admin,
            scope=scope,
            suffix="AAA1",
            opaque_address_ref=opaque_ref,
            team_ref=team_ref,
        )
        rearmed = admin.execute(
            """
            SELECT status, attempt, lease_generation
              FROM email_gateway.identity_route_work
             WHERE site_id = %s AND processing_purpose = %s
            """,
            (scope.site_id, scope.processing_purpose),
        ).fetchone()
        assert rearmed == ("queued", 0, zero_candidate_claim.generation + 1)

        racing_claim = _claim(work, scope, "initial-route-racing")
        assert work.list_candidate_refs(scope, racing_claim, now=datetime.now(UTC), limit=10) == (
            first_ref,
        )

        apply_connection = psycopg.connect(dsn, autocommit=True)
        monitor = psycopg.connect(dsn, autocommit=True)
        insert_started = threading.Event()
        insert_finished = threading.Event()
        insert_errors: list[BaseException] = []
        late_ref: list[str] = []

        def insert_late() -> None:
            try:
                with psycopg.connect(
                    dsn, autocommit=True, application_name="initial-route-late-inbox"
                ) as late_connection:
                    insert_started.set()
                    late_ref.append(
                        _insert_pending_inbox(
                            late_connection,
                            scope=scope,
                            suffix="BBB2",
                            opaque_address_ref=opaque_ref,
                            team_ref=team_ref,
                        )
                    )
            except BaseException as error:
                insert_errors.append(error)
            finally:
                insert_finished.set()

        thread = threading.Thread(target=insert_late, daemon=True)
        try:
            _apply_unassigned_open_transaction(
                apply_connection,
                scope=scope,
                claim=racing_claim,
                inbox_ref=first_ref,
                request_suffix="race-first",
            )
            thread.start()
            assert insert_started.wait(timeout=2)
            observed_lock_wait = False
            for _ in range(100):
                wait = monitor.execute(
                    """
                    SELECT wait_event_type
                      FROM pg_stat_activity
                     WHERE application_name = 'initial-route-late-inbox'
                       AND state = 'active'
                    """
                ).fetchone()
                if wait == ("Lock",):
                    observed_lock_wait = True
                    break
                if insert_finished.wait(timeout=0.02):
                    break
            assert observed_lock_wait
            apply_connection.execute("COMMIT")
            assert insert_finished.wait(timeout=5)
            thread.join(timeout=1)
        finally:
            if not insert_finished.is_set():
                apply_connection.execute("ROLLBACK")
                insert_finished.wait(timeout=5)
                thread.join(timeout=1)
            apply_connection.close()
            monitor.close()
        assert insert_errors == []
        assert len(late_ref) == 1

        with pytest.raises(IdentityRouteLeaseLost):
            work.complete(scope, racing_claim, now=datetime.now(UTC))
        next_claim = _claim(work, scope, "initial-route-after-race")
        assert work.list_candidate_refs(scope, next_claim, now=datetime.now(UTC), limit=10) == (
            late_ref[0],
        )
        _apply_unassigned_open_transaction(
            admin,
            scope=scope,
            claim=next_claim,
            inbox_ref=late_ref[0],
            request_suffix="race-second",
        )
        admin.execute("COMMIT")
        work.complete(scope, next_claim, now=datetime.now(UTC))

        third_ref = _insert_pending_inbox(
            admin,
            scope=scope,
            suffix="CCC3",
            opaque_address_ref=opaque_ref,
            team_ref=team_ref,
        )
        final_claim = _claim(work, scope, "initial-route-after-complete")
        assert work.list_candidate_refs(scope, final_claim, now=datetime.now(UTC), limit=10) == (
            third_ref,
        )
        states = admin.execute(
            """
            SELECT inbox_item_ref, state, revision
              FROM email_gateway.inbox_items
             WHERE site_id = %s
             ORDER BY inbox_item_ref
            """,
            (scope.site_id,),
        ).fetchall()
        assert states == [
            (first_ref, "unassigned", 2),
            (late_ref[0], "unassigned", 2),
            (third_ref, "identity_pending", 1),
        ]


@pytest.mark.postgres_integration
def test_expired_fifth_attempt_is_safely_dead_lettered() -> None:
    dsn = _enabled_dsn()
    import psycopg

    scope = TenantScope(
        f"initial-route-exhausted-{datetime.now(UTC).strftime('%H%M%S%f')}.example",
        "sales_follow_up",
    )
    projection = IdentityProjection(
        site_id=scope.site_id,
        processing_purpose=scope.processing_purpose,
        opaque_address_ref="extid:v1:email:" + "X" * 43,
        external_identity_ref="EID-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        external_identity_revision=1,
        identity_type="Party",
        team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        status="confirmed",
        projection_receipt_ref="sha256:" + "e" * 64,
        observed_at=datetime.now(UTC),
        payload_digest="sha256:" + "f" * 64,
    )
    with (
        psycopg.connect(dsn, autocommit=True) as admin,
        psycopg.connect(dsn, autocommit=True) as worker,
    ):
        _apply_migrations_twice(admin)
        admin.execute("SET ROLE gbos_email_gateway_app")
        PostgresIdentityProjectionRepository(admin).apply(scope, projection)
        admin.execute("RESET ROLE")
        admin.execute(
            """
            UPDATE email_gateway.identity_route_work
               SET status = 'leased', attempt = 5,
                   lease_owner = 'expired-worker',
                   lease_expires_at = %s, lease_generation = 5,
                   fence_token = %s, updated_at = greatest(%s, created_at)
             WHERE site_id = %s AND processing_purpose = %s
            """,
            (
                datetime.now(UTC) - timedelta(minutes=1),
                "v1:" + "a" * 64,
                datetime.now(UTC),
                scope.site_id,
                scope.processing_purpose,
            ),
        )
        worker.execute("SET ROLE gbos_email_gateway_worker")
        repository = PostgresIdentityRouteWorkRepository(worker)
        assert (
            repository.claim(
                scope,
                worker_id="replacement-worker",
                now=datetime.now(UTC),
                lease_duration=timedelta(seconds=30),
            )
            is None
        )
        assert admin.execute(
            """
            SELECT status, safe_error_code, completed_at IS NOT NULL,
                   lease_generation
              FROM email_gateway.identity_route_work
             WHERE site_id = %s AND processing_purpose = %s
            """,
            (scope.site_id, scope.processing_purpose),
        ).fetchone() == ("dead_letter", "attempts_exhausted", True, 6)
