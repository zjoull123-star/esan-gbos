from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.context.context_service.models import (
    RecordKind,
    TenantScope,
)
from services.context.context_service.publisher import ContextPublisher
from services.context.context_service.storage import (
    MigrationRunner,
    PostgresContextRepository,
    connect_postgres,
    connect_postgres_components,
)
from services.observer.observer.api import create_observer_app
from services.observer.observer.application import ManualImportPipeline, canonical_import_body
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.models import (
    ByteLocator,
    CanonicalObservation,
    EvidenceRecord,
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    Participant,
)
from services.observer.observer.models import (
    TenantScope as ObserverTenantScope,
)
from services.observer.observer.processing import (
    DeterministicProcessor,
    DisabledReviewCaseBridge,
)
from services.observer.observer.security import (
    HMACServiceIdentity,
    LocalRequestAuthenticator,
    NonceStore,
)
from services.observer.observer.storage import (
    IdempotencyConflict as ObserverIdempotencyConflict,
)
from services.observer.observer.storage import (
    PostgresObserverRepository,
)

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_DSN = os.getenv("GBOS_GATE3_POSTGRES_DSN")
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")
CONTEXT_HOST = os.getenv("GBOS_GATE3_CONTEXT_HOST")
CONTEXT_PORT = os.getenv("GBOS_GATE3_CONTEXT_PORT")
CONTEXT_DATABASE = os.getenv("GBOS_GATE3_CONTEXT_DATABASE")
CONTEXT_PASSWORD = os.getenv("GBOS_GATE3_CONTEXT_PASSWORD")

pytestmark = [pytest.mark.postgres_integration]
if not RUN_INTEGRATION:
    pytestmark.append(pytest.mark.skip(reason="set GBOS_RUN_POSTGRES_INTEGRATION=1 to run"))


def _container_sql(
    sql: str,
    *,
    database: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not POSTGRES_CONTAINER:
        pytest.fail(
            "GBOS_GATE3_POSTGRES_DSN or GBOS_GATE3_POSTGRES_CONTAINER is required "
            "when GBOS_RUN_POSTGRES_INTEGRATION=1"
        )
    command = [
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
        database or "",
    ]
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
    )


def test_gate3_migrations_run_twice_and_enable_forced_rls() -> None:
    if POSTGRES_DSN:
        root = Path(__file__).parents[2]
        connection = connect_postgres(POSTGRES_DSN)
        try:
            runner = MigrationRunner(
                connection,
                [
                    root / "services" / "observer" / "migrations",
                    root / "services" / "context" / "migrations",
                ],
            )
            first = runner.run()
            second = runner.run()
            assert second == ()
            assert first or _migration_ledger_count() == 3
        finally:
            connection.close()

    assert _migration_ledger_count() == 3
    result = _container_sql(
        """
        SELECT count(*)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('observer', 'context')
          AND c.relkind = 'r'
          AND c.relname <> 'schema_migrations'
          AND c.relrowsecurity
          AND c.relforcerowsecurity
        """
    )
    assert int(result.stdout.strip()) == 21


def _migration_ledger_count() -> int:
    result = _container_sql("SELECT count(*) FROM observer.schema_migrations")
    return int(result.stdout.strip())


def test_gate3_runtime_roles_are_non_privileged_and_schema_isolated() -> None:
    result = _container_sql(
        """
        SELECT rolname || ':' || rolsuper || ':' || rolbypassrls
        FROM pg_roles
        WHERE rolname IN ('gbos_observer_app', 'gbos_context_app')
        ORDER BY rolname
        """
    )
    assert result.stdout.splitlines() == [
        "gbos_context_app:false:false",
        "gbos_observer_app:false:false",
    ]
    privileges = _container_sql(
        """
        SELECT
          has_schema_privilege('gbos_observer_app', 'observer', 'USAGE')::int,
          has_schema_privilege('gbos_observer_app', 'context', 'USAGE')::int,
          has_schema_privilege('gbos_context_app', 'observer', 'USAGE')::int,
          has_schema_privilege('gbos_context_app', 'context', 'USAGE')::int
        """
    )
    assert privileges.stdout.strip() == "1|0|0|1"


def test_gate3_context_rls_and_proposal_constraint_fail_closed() -> None:
    suffix = uuid.uuid4().hex[:12]
    site_a = f"site-a-{suffix}"
    site_b = f"site-b-{suffix}"
    digest = "a" * 64

    for site in (site_a, site_b):
        _container_sql(
            f"""
            BEGIN;
            SET LOCAL ROLE gbos_context_app;
            SET LOCAL app.site_id = '{site}';
            INSERT INTO context.fact_proposals (
              site_id, fact_proposal_record_id, processing_purpose,
              idempotency_key, payload_digest, status, subject_ref,
              predicate, confidence, document
            ) VALUES (
              '{site}', 'fact-{site}', 'observation_processing',
              'idem-{site}', '{digest}', 'proposed', 'subject-{site}',
              'communication_summary', 1.0, '{{"status":"proposed"}}'::jsonb
            );
            COMMIT;
            """
        )

    visible = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_context_app;
        SET LOCAL app.site_id = '{site_a}';
        SELECT count(*) FROM context.fact_proposals
        WHERE fact_proposal_record_id IN ('fact-{site_a}', 'fact-{site_b}');
        COMMIT;
        """
    )
    assert visible.stdout.splitlines()[-1] == "1"

    cross_site = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_context_app;
        SET LOCAL app.site_id = '{site_a}';
        INSERT INTO context.fact_proposals (
          site_id, fact_proposal_record_id, processing_purpose,
          idempotency_key, payload_digest, status, subject_ref,
          predicate, confidence, document
        ) VALUES (
          '{site_b}', 'cross-{suffix}', 'observation_processing',
          'cross-{suffix}', '{digest}', 'proposed', 'subject',
          'communication_summary', 1.0, '{{"status":"proposed"}}'::jsonb
        );
        COMMIT;
        """,
        check=False,
    )
    assert cross_site.returncode != 0

    confirmed = _container_sql(
        f"""
        BEGIN;
        SET LOCAL ROLE gbos_context_app;
        SET LOCAL app.site_id = '{site_a}';
        INSERT INTO context.fact_proposals (
          site_id, fact_proposal_record_id, processing_purpose,
          idempotency_key, payload_digest, status, subject_ref,
          predicate, confidence, document
        ) VALUES (
          '{site_a}', 'confirmed-{suffix}', 'observation_processing',
          'confirmed-{suffix}', '{digest}', 'confirmed', 'subject',
          'communication_summary', 1.0, '{{"status":"confirmed"}}'::jsonb
        );
        COMMIT;
        """,
        check=False,
    )
    assert confirmed.returncode != 0


def test_gate3_postgres_context_repository_uses_the_rls_app_role(
    tmp_path: Path,
) -> None:
    if not all((CONTEXT_HOST, CONTEXT_PORT, CONTEXT_DATABASE, CONTEXT_PASSWORD)):
        pytest.fail("Gate 3 Context app-role connection components are required")
    context_connection = connect_postgres_components(
        host=str(CONTEXT_HOST),
        port=int(str(CONTEXT_PORT)),
        database=str(CONTEXT_DATABASE),
        user="gbos_context_app",
        password=str(CONTEXT_PASSWORD),
    )
    observer_connection = connect_postgres_components(
        host=str(CONTEXT_HOST),
        port=int(str(CONTEXT_PORT)),
        database=str(CONTEXT_DATABASE),
        user="gbos_observer_app",
        password=str(CONTEXT_PASSWORD),
    )
    try:
        with context_connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("gbos_context_app",)
        suffix = uuid.uuid4().hex[:12]
        site = f"repository-{suffix}"
        now = datetime.now(UTC)
        purpose = "observation_processing"
        observer_scope = ObserverTenantScope(site, purpose)
        secret = b"synthetic-context-publisher-secret"
        manifest = ManualImportManifest(
            connector="manual_import",
            fixture_id=f"fixture-{suffix}",
            occurred_at=now,
            consent_basis="consent",
            data_classification="Restricted",
            retention_class="R1-operational",
            participants=(Participant("external", f"party:synthetic-{suffix}"),),
            correlation_id=f"corr-{suffix}",
            provider_event_id=f"provider-{suffix}",
        )
        members = (
            ManualImportMember(
                "message.txt",
                "text/plain",
                "合成客户要求下周安排样品。".encode(),
            ),
        )
        pipeline = ManualImportPipeline(
            store=ContentAddressedEvidenceStore(tmp_path / "context-objects"),
            authenticator=LocalRequestAuthenticator(
                identity="context-publisher-fixture",
                secret=secret,
                nonce_store=NonceStore(),
                clock=lambda: now,
            ),
            processor=DeterministicProcessor(),
            review_bridge=DisabledReviewCaseBridge(),
            clock=lambda: now,
        )
        signed = HMACServiceIdentity("context-publisher-fixture", secret).sign(
            method="POST",
            path="/internal/v1/manual-imports",
            timestamp=now,
            nonce=f"nonce-{suffix}",
            scope=observer_scope,
            body=canonical_import_body(manifest, members),
        )
        result = pipeline.ingest(
            scope=observer_scope,
            signed_request=signed,
            idempotency_key=f"observer-{suffix}",
            manifest=manifest,
            members=members,
        )
        persisted = PostgresObserverRepository(observer_connection).persist(
            observer_scope,
            idempotency_key=f"observer-{suffix}",
            payload_digest=result.observation.raw_sha256,
            result=result,
            provider_event_id=manifest.provider_event_id,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        repository = PostgresContextRepository(context_connection)
        publisher = ContextPublisher(repository)
        created = publisher.publish(
            result,
            correlation_id=manifest.correlation_id,
            recorded_at=persisted.ingested_at,
        )
        replay = publisher.publish(
            result,
            correlation_id=manifest.correlation_id,
            recorded_at=persisted.ingested_at,
        )

        assert created == replay
        assert [item.kind for item in created] == [
            RecordKind.EVIDENCE,
            RecordKind.FACT_PROPOSAL,
            RecordKind.ENTITY_RESOLUTION_PROPOSAL,
        ]
        assert (
            repository.get(
                TenantScope(f"other-{suffix}", purpose),
                RecordKind.FACT_PROPOSAL,
                created[1].record_id,
            )
            is None
        )
        with context_connection.transaction(), context_connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (site,),
            )
            cursor.execute(
                """
                SELECT
                  (SELECT count(*) FROM context.fact_evidence WHERE site_id = %s),
                  (SELECT count(*) FROM context.candidates WHERE site_id = %s)
                """,
                (site, site),
            )
            assert cursor.fetchone() == (1, 1)
    finally:
        observer_connection.close()
        context_connection.close()


def _observer_result(
    *,
    site: str,
    suffix: str,
    occurred_at: datetime,
    raw_sha256: str,
) -> ImportResult:
    event_id = f"event-{suffix}"
    evidence_id = f"evidence-{suffix}"
    participant = Participant("external", f"party-{suffix}")
    observation = CanonicalObservation(
        event_id=event_id,
        site_id=site,
        processing_purpose="observation_processing",
        connector="manual_import",
        channel="manual_import",
        occurred_at=occurred_at,
        ingested_at=occurred_at,
        original_language="zh",
        participants=(participant,),
        evidence_refs=(evidence_id,),
        raw_sha256=raw_sha256,
        consent_basis="consent",
        data_classification="Restricted",
        retention_class="R1-operational",
        correlation_id=f"corr-{suffix}",
        source_lineage=(f"manual_import:{suffix}",),
        processor_version="manual-import-v1",
    )
    evidence = EvidenceRecord(
        evidence_id=evidence_id,
        observation_event_id=event_id,
        site_id=site,
        processing_purpose="observation_processing",
        data_classification="Restricted",
        source_lineage=(event_id,),
        processor_version="manual-import-v1",
        raw_sha256="f" * 64,
        object_ref=f"local-object://{evidence_id}",
        media_type="text/plain",
        locator=ByteLocator(0, 10),
        created_at=occurred_at,
        retention_class="R1-operational",
    )
    return ImportResult(observation, (evidence,), (), ())


def test_gate3_postgres_observer_repository_closes_rls_dedup_and_checkpoint_loop() -> None:
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
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("gbos_observer_app",)
        repository = PostgresObserverRepository(connection)
        suffix = uuid.uuid4().hex[:12]
        site = f"observer-{suffix}"
        scope = ObserverTenantScope(site, "observation_processing")
        occurred_at = datetime.now(UTC).replace(second=0, microsecond=0)
        original = _observer_result(
            site=site,
            suffix=f"{suffix}-original",
            occurred_at=occurred_at,
            raw_sha256="1" * 64,
        )

        stored = repository.persist(
            scope,
            idempotency_key=f"idem-{suffix}",
            payload_digest="1" * 64,
            result=original,
            provider_event_id=f"provider-{suffix}",
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        replay = repository.persist(
            scope,
            idempotency_key=f"idem-{suffix}",
            payload_digest="1" * 64,
            result=original,
            provider_event_id=f"provider-{suffix}",
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        provider_duplicate = repository.persist(
            scope,
            idempotency_key=f"idem-provider-duplicate-{suffix}",
            payload_digest="2" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-provider-duplicate",
                occurred_at=occurred_at + timedelta(seconds=5),
                raw_sha256="2" * 64,
            ),
            provider_event_id=f"provider-{suffix}",
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        advanced = repository.persist(
            scope,
            idempotency_key=f"idem-advance-{suffix}",
            payload_digest="3" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-advance",
                occurred_at=occurred_at + timedelta(minutes=2),
                raw_sha256="3" * 64,
            ),
            provider_event_id=None,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        fallback_stored = repository.persist(
            scope,
            idempotency_key=f"idem-fallback-{suffix}",
            payload_digest="6" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-fallback",
                occurred_at=occurred_at + timedelta(minutes=3),
                raw_sha256="6" * 64,
            ),
            provider_event_id=None,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        fallback_duplicate = repository.persist(
            scope,
            idempotency_key=f"idem-fallback-duplicate-{suffix}",
            payload_digest="6" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-fallback-duplicate",
                occurred_at=occurred_at + timedelta(minutes=3, seconds=30),
                raw_sha256="6" * 64,
            ),
            provider_event_id=None,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        late = repository.persist(
            scope,
            idempotency_key=f"idem-late-{suffix}",
            payload_digest="4" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-late",
                occurred_at=occurred_at + timedelta(minutes=2, seconds=30),
                raw_sha256="4" * 64,
            ),
            provider_event_id=None,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        outside = repository.persist(
            scope,
            idempotency_key=f"idem-outside-{suffix}",
            payload_digest="5" * 64,
            result=_observer_result(
                site=site,
                suffix=f"{suffix}-outside",
                occurred_at=occurred_at,
                raw_sha256="5" * 64,
            ),
            provider_event_id=None,
            checkpoint_id=f"checkpoint-{suffix}",
            replay_window_seconds=60,
        )
        with pytest.raises(ObserverIdempotencyConflict, match="idempotency_conflict"):
            repository.persist(
                scope,
                idempotency_key=f"idem-{suffix}",
                payload_digest="7" * 64,
                result=_observer_result(
                    site=site,
                    suffix=f"{suffix}-conflict",
                    occurred_at=occurred_at + timedelta(minutes=4),
                    raw_sha256="7" * 64,
                ),
                provider_event_id=f"provider-conflict-{suffix}",
                checkpoint_id=f"checkpoint-{suffix}",
                replay_window_seconds=60,
            )

        assert stored == replay
        assert provider_duplicate.event_id == stored.event_id
        assert provider_duplicate.status == "duplicate"
        assert fallback_duplicate.event_id == fallback_stored.event_id
        assert fallback_duplicate.status == "duplicate"
        assert advanced.status == "stored"
        assert late.status == "stored"
        assert late.checkpoint_disposition == "late_within_window"
        assert outside.status == "dead_letter"
        assert outside.dead_letter_reason == "outside_replay_window"
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL app.site_id = '{site}'")
            cursor.execute(
                """
                SELECT cursor_occurred_at
                FROM observer.checkpoints
                WHERE site_id = %s AND connector = 'manual_import'
                """,
                (site,),
            )
            assert cursor.fetchone() == (occurred_at + timedelta(minutes=3),)
            cursor.execute(
                """
                SELECT count(*)
                FROM observer.dead_letter
                WHERE site_id = %s AND reason_code = 'outside_replay_window'
                """,
                (site,),
            )
            assert cursor.fetchone() == (1,)
        assert (
            repository.get(
                ObserverTenantScope(f"other-{suffix}", "observation_processing"),
                stored.event_id or "",
            )
            is None
        )
    finally:
        connection.close()


def test_gate3_observer_http_runtime_persists_signed_import_with_app_role(
    tmp_path: Path,
) -> None:
    if not all((CONTEXT_HOST, CONTEXT_PORT, CONTEXT_DATABASE, CONTEXT_PASSWORD)):
        pytest.fail("Gate 3 Observer app-role connection components are required")
    observer_connection = connect_postgres_components(
        host=str(CONTEXT_HOST),
        port=int(str(CONTEXT_PORT)),
        database=str(CONTEXT_DATABASE),
        user="gbos_observer_app",
        password=str(CONTEXT_PASSWORD),
    )
    context_connection = connect_postgres_components(
        host=str(CONTEXT_HOST),
        port=int(str(CONTEXT_PORT)),
        database=str(CONTEXT_DATABASE),
        user="gbos_context_app",
        password=str(CONTEXT_PASSWORD),
    )
    try:
        suffix = uuid.uuid4().hex[:12]
        now = datetime.now(UTC)
        site = f"observer-api-{suffix}"
        purpose = "observation_processing"
        scope = ObserverTenantScope(site, purpose)
        secret = b"synthetic-postgres-api-secret"
        identity_name = "observer-postgres-fixture"
        content = "合成客户要求下周安排样品。".encode()
        manifest = ManualImportManifest(
            connector="manual_import",
            fixture_id=f"fixture-{suffix}",
            occurred_at=now,
            consent_basis="consent",
            data_classification="Restricted",
            retention_class="R1-operational",
            participants=(Participant("external", f"party:synthetic-{suffix}"),),
            correlation_id=f"corr-{suffix}",
            provider_event_id=f"provider-{suffix}",
        )
        members = (ManualImportMember("message.txt", "text/plain", content),)
        idempotency_key = f"import-{suffix}"
        pipeline = ManualImportPipeline(
            store=ContentAddressedEvidenceStore(tmp_path / "objects"),
            authenticator=LocalRequestAuthenticator(
                identity=identity_name,
                secret=secret,
                nonce_store=NonceStore(),
                clock=lambda: now,
            ),
            processor=DeterministicProcessor(),
            review_bridge=DisabledReviewCaseBridge(),
            clock=lambda: now,
        )
        signer = HMACServiceIdentity(identity_name, secret)
        signed = signer.sign(
            method="POST",
            path="/internal/v1/manual-imports",
            timestamp=now,
            nonce=f"nonce-{suffix}",
            scope=scope,
            body=canonical_import_body(manifest, members),
        )
        payload = {
            "manifest": {
                "schema_version": "1.0",
                "manifest_id": manifest.fixture_id,
                "synthetic": True,
                "site_id": site,
                "processing_purpose": purpose,
                "occurred_at": now.isoformat(),
                "original_language": "zh-CN",
                "consent_basis": manifest.consent_basis,
                "data_classification": manifest.data_classification,
                "retention_class": manifest.retention_class,
                "participants": [
                    {
                        "role": manifest.participants[0].role,
                        "identity_ref": manifest.participants[0].identity_ref,
                    }
                ],
                "source": {
                    "connector": "manual_import",
                    "package_type": "message_fixture",
                    "provider_event_id": manifest.provider_event_id,
                },
                "package": {
                    "filename": "message.txt",
                    "media_type": "text/plain",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
                "budgets": {
                    "body_bytes": 1_048_576,
                    "attachment_count": 0,
                    "attachment_bytes": 0,
                    "decompressed_bytes": 1_048_576,
                },
                "idempotency_key": idempotency_key,
                "correlation_id": manifest.correlation_id,
                "submitted_at": now.isoformat(),
            },
            "members": [
                {
                    "name": "message.txt",
                    "media_type": "text/plain",
                    "content_utf8": content.decode(),
                }
            ],
        }
        response = TestClient(
            create_observer_app(
                pipeline=pipeline,
                persistence=PostgresObserverRepository(observer_connection),
                context_publisher=ContextPublisher(PostgresContextRepository(context_connection)),
            )
        ).post(
            "/internal/v1/manual-imports",
            headers={
                "X-GBOS-Identity": signed.identity,
                "X-GBOS-Timestamp": signed.timestamp.isoformat(),
                "X-GBOS-Nonce": signed.nonce,
                "X-Site-ID": site,
                "X-Processing-Purpose": purpose,
                "X-GBOS-Body-SHA256": signed.body_sha256,
                "X-GBOS-Signature": signed.signature,
                "Idempotency-Key": idempotency_key,
                "X-Request-ID": f"REQ-{suffix}",
            },
            json=payload,
        )

        assert response.status_code == 200
        ingestion = response.json()["data"]["ingestion"]
        assert ingestion["status"] == "stored"
        persisted = PostgresObserverRepository(observer_connection).get(
            scope,
            ingestion["event_id"],
        )
        assert persisted is not None
        assert persisted.provider_event_id == manifest.provider_event_id
        assert persisted.raw_sha256 == signed.body_sha256
        assert response.json()["data"]["context_publication"] == {
            "status": "published",
            "record_count": 3,
        }
    finally:
        context_connection.close()
        observer_connection.close()


def test_gate3_backup_restore_smoke() -> None:
    if not POSTGRES_CONTAINER:
        pytest.skip("container mode is required for backup/restore smoke")
    restore_database = f"gbos_restore_smoke_{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "exec",
        POSTGRES_CONTAINER,
        "sh",
        "-eu",
        "-c",
        """
        PGPASSWORD="$POSTGRES_PASSWORD" createdb \
          -U "$POSTGRES_USER" "$1"
        cleanup() {
          PGPASSWORD="$POSTGRES_PASSWORD" dropdb \
            -U "$POSTGRES_USER" --if-exists "$1"
        }
        trap 'cleanup "$1"' EXIT
        PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
          -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
          --schema=observer --schema=context --no-owner --no-privileges |
          PGPASSWORD="$POSTGRES_PASSWORD" psql \
            -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 >/dev/null
        PGPASSWORD="$POSTGRES_PASSWORD" psql \
          -U "$POSTGRES_USER" -d "$1" -Atq -v ON_ERROR_STOP=1 \
          -c "SELECT count(*) FROM pg_tables WHERE schemaname IN ('observer','context')"
        """,
        "sh",
        restore_database,
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(result.stdout.strip().splitlines()[-1]) >= 22
