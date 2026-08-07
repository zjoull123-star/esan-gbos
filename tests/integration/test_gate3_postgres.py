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
    PostgresContextRepository,
    connect_postgres_components,
)
from services.observer.observer.api import create_observer_app
from services.observer.observer.application import ManualImportPipeline, canonical_import_body
from services.observer.observer.evidence_store import ContentAddressedEvidenceStore
from services.observer.observer.local_pilot_ingestion import DurableDeliveryInbox
from services.observer.observer.local_pilot_storage import (
    DeliveryConflict,
    IngressExpired,
    NonceReplay,
    NormalizedBatchConflict,
    PostgresLocalPilotStorage,
    ProcessingJobMetadata,
)
from services.observer.observer.models import (
    ByteLocator,
    CanonicalObservation,
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    EvidenceRecord,
    ImportResult,
    ManualImportManifest,
    ManualImportMember,
    NormalizedObservationInput,
    Participant,
    RawDelivery,
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
    assert _migration_ledger_count() == 6
    result = _container_sql(
        """
        SELECT count(*)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('observer', 'context')
          AND c.relkind = 'r'
          AND c.relname <> 'schema_migrations'
          AND c.relname IN (
              'manual_import_jobs', 'raw_objects', 'observation_events',
              'participants', 'evidence_refs', 'event_evidence',
              'checkpoints', 'quarantine', 'dead_letter', 'processor_runs',
              'derivation_edges', 'consent', 'legal_holds',
              'deletion_receipts', 'evidence_records', 'fact_proposals',
              'fact_evidence', 'entity_resolution_proposals', 'candidates',
              'restrictions', 'inbox_messages', 'connector_instances',
              'inbound_deliveries',
              'inbound_delivery_events', 'connector_checkpoints',
              'persistent_nonces', 'processing_jobs',
              'context_publication_outbox', 'local_pilot_quarantine',
              'local_pilot_dead_letter'
          )
          AND c.relrowsecurity
          AND c.relforcerowsecurity
        """
    )
    assert int(result.stdout.strip()) == 30


def _migration_ledger_count() -> int:
    result = _container_sql(
        """
        SELECT count(*)
        FROM observer.schema_migrations
        WHERE migration_name IN (
            'observer/001_gate3_observer.sql',
            'observer/002_gate3_observer_runtime.sql',
            'observer/003_local_pilot_runtime.sql',
            'observer/004_local_pilot_ingestion.sql',
            'observer/005_local_pilot_normalized_sink.sql',
            'context/001_gate3_context.sql'
        )
        """
    )
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
              'communication_summary', 1.0,
              '{{"status":"proposed","output_version":"gate3-test-v1"}}'::jsonb
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
          'communication_summary', 1.0,
          '{{"status":"proposed","output_version":"gate3-test-v1"}}'::jsonb
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
          'communication_summary', 1.0,
          '{{"status":"confirmed","output_version":"gate3-test-v1"}}'::jsonb
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


def test_authenticated_ingress_is_atomic_idempotent_and_instance_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        scope = ObserverTenantScope(f"ingress-{suffix}", "observation_processing")
        other_scope = ObserverTenantScope(
            f"ingress-other-{suffix}",
            "observation_processing",
        )
        key = ConnectorKey("wecom", f"primary-{suffix}")
        other_key = ConnectorKey("wecom", f"secondary-{suffix}")
        repository = PostgresLocalPilotStorage(connection)
        repository.register_connector_instance(
            scope,
            key,
            now=now,
            replay_window_seconds=60,
        )
        evidence_store = ContentAddressedEvidenceStore(tmp_path / "objects")
        inbox = DurableDeliveryInbox(
            storage=repository,
            evidence_store=evidence_store,
        )
        exact = b"\xff\x00authenticated-ingress"
        raw = RawDelivery(
            f"delivery-{suffix}",
            exact,
            "application/octet-stream",
            now,
        )
        nonce = f"nonce-{suffix}"
        nonce_expires_at = now + timedelta(seconds=30)

        original_enqueue = repository._enqueue_processing_job

        def fail_enqueue(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic job insert failure")

        monkeypatch.setattr(repository, "_enqueue_processing_job", fail_enqueue)
        with pytest.raises(RuntimeError, match="synthetic job insert failure"):
            inbox.accept_authenticated(
                scope,
                key,
                raw,
                correlation_id=f"corr-{suffix}",
                nonce=nonce,
                nonce_expires_at=nonce_expires_at,
                now=now,
            )
        assert _authenticated_ingress_counts(connection, scope.site_id) == (0, 0, 0)
        orphan = evidence_store.put(
            scope,
            exact,
            media_type="application/octet-stream",
        )
        assert evidence_store.exists(scope, orphan.object_ref)

        monkeypatch.setattr(repository, "_enqueue_processing_job", original_enqueue)
        accepted = inbox.accept_authenticated(
            scope,
            key,
            raw,
            correlation_id=f"corr-{suffix}",
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            now=now,
        )
        duplicate = inbox.accept_authenticated(
            scope,
            key,
            raw,
            correlation_id=f"corr-{suffix}",
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            now=now + timedelta(seconds=1),
        )
        assert accepted.disposition == "accepted"
        assert duplicate.disposition == "duplicate"
        assert _authenticated_ingress_counts(connection, scope.site_id) == (1, 1, 1)

        with pytest.raises(DeliveryConflict, match="content metadata"):
            inbox.accept_authenticated(
                scope,
                key,
                RawDelivery(
                    raw.delivery_id,
                    b"different-body",
                    raw.media_type,
                    raw.received_at,
                ),
                correlation_id=f"corr-{suffix}",
                nonce=nonce,
                nonce_expires_at=nonce_expires_at,
                now=now + timedelta(seconds=1),
            )
        with pytest.raises(NonceReplay, match="different delivery"):
            inbox.accept_authenticated(
                scope,
                key,
                RawDelivery(
                    f"different-{raw.delivery_id}",
                    raw.exact_bytes,
                    raw.media_type,
                    raw.received_at,
                ),
                correlation_id=f"corr-{suffix}",
                nonce=nonce,
                nonce_expires_at=nonce_expires_at,
                now=now + timedelta(seconds=1),
            )
        with pytest.raises(IngressExpired, match="nonce"):
            inbox.accept_authenticated(
                scope,
                key,
                RawDelivery(
                    f"expired-{raw.delivery_id}",
                    raw.exact_bytes,
                    raw.media_type,
                    now,
                ),
                correlation_id=f"corr-{suffix}",
                nonce=f"expired-{nonce}",
                nonce_expires_at=now,
                now=now,
            )
        with pytest.raises(IngressExpired, match="replay window"):
            inbox.accept_authenticated(
                scope,
                key,
                RawDelivery(
                    f"old-{raw.delivery_id}",
                    raw.exact_bytes,
                    raw.media_type,
                    now - timedelta(seconds=61),
                ),
                correlation_id=f"corr-{suffix}",
                nonce=f"old-{nonce}",
                nonce_expires_at=nonce_expires_at,
                now=now,
            )
        assert _authenticated_ingress_counts(connection, scope.site_id) == (1, 1, 1)

        repository.register_connector_instance(
            scope,
            other_key,
            now=now,
            replay_window_seconds=60,
        )
        instance_isolated = inbox.accept_authenticated(
            scope,
            other_key,
            raw,
            correlation_id=f"corr-instance-{suffix}",
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            now=now,
        )
        assert instance_isolated.disposition == "accepted"
        assert _authenticated_ingress_counts(connection, scope.site_id) == (2, 2, 2)

        repository.register_connector_instance(
            other_scope,
            key,
            now=now,
            replay_window_seconds=60,
        )
        site_isolated = inbox.accept_authenticated(
            other_scope,
            key,
            raw,
            correlation_id=f"corr-site-{suffix}",
            nonce=nonce,
            nonce_expires_at=nonce_expires_at,
            now=now,
        )
        assert site_isolated.disposition == "accepted"
        assert _authenticated_ingress_counts(connection, other_scope.site_id) == (1, 1, 1)
        assert _authenticated_ingress_counts(connection, scope.site_id) == (2, 2, 2)
    finally:
        connection.close()


def _authenticated_ingress_counts(
    connection: object,
    site_id: str,
) -> tuple[int, int, int]:
    with connection.transaction(), connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (site_id,),
        )
        cursor.execute(
            """
            SELECT
              (SELECT count(*) FROM observer.persistent_nonces
               WHERE site_id = %s),
              (SELECT count(*) FROM observer.inbound_deliveries
               WHERE site_id = %s),
              (SELECT count(*) FROM observer.processing_jobs
               WHERE site_id = %s)
            """,
            (site_id, site_id, site_id),
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2])


def _normalized_input(
    *,
    provider_event_id: str,
    source_ref: str,
    correlation_suffix: str = "",
) -> tuple[ConnectorItem, NormalizedObservationInput]:
    item = ConnectorItem(
        provider_event_id=provider_event_id,
        occurred_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
        source_cursor=f"cursor:{provider_event_id}",
        payload={"opaque": True},
    )
    normalized = NormalizedObservationInput(
        channel="chat",
        participants=(
            Participant(
                role="unknown",
                identity_ref=(
                    "unresolved:delivery:" + hashlib.sha256(provider_event_id.encode()).hexdigest()
                ),
            ),
        ),
        evidence=(
            EvidenceArtifact(
                media_type="application/json",
                locator="delivery",
                role="source",
                reference=source_ref,
            ),
        ),
        consent_basis="pilot_deferred_review",
        data_classification="Restricted",
        retention_class="R1-operational",
        original_language="und",
        correlation_id=f"corr-{provider_event_id}{correlation_suffix}",
    )
    return item, normalized


def _claim_normalized_job(
    *,
    repository: PostgresLocalPilotStorage,
    evidence_store: ContentAddressedEvidenceStore,
    scope: ObserverTenantScope,
    key: ConnectorKey,
    suffix: str,
    now: datetime,
) -> tuple[ProcessingJobMetadata, str]:
    repository.register_connector_instance(
        scope,
        key,
        now=now,
        replay_window_seconds=60,
    )
    accepted = DurableDeliveryInbox(
        storage=repository,
        evidence_store=evidence_store,
    ).accept(
        scope,
        key,
        RawDelivery(
            delivery_id=f"delivery-{suffix}",
            exact_bytes=f'{{"fixture":"{suffix}"}}'.encode(),
            media_type="application/json",
            received_at=now,
        ),
        correlation_id=f"corr-{suffix}",
    )
    claimed = repository.claim_processing_job(
        scope,
        worker_id=f"worker-{suffix}",
        now=now + timedelta(seconds=1),
        lease_seconds=60,
    )
    assert claimed is not None
    assert claimed.job_id == accepted.job.job_id
    return claimed, accepted.delivery.object_ref


def test_normalized_batch_is_atomic_replay_safe_and_site_instance_isolated(
    tmp_path: Path,
) -> None:
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
        scope = ObserverTenantScope(
            f"normalized-{suffix}",
            "observation_processing",
        )
        other_scope = ObserverTenantScope(
            f"normalized-other-{suffix}",
            "observation_processing",
        )
        key = ConnectorKey("wecom", f"primary-{suffix}")
        other_key = ConnectorKey("wecom", f"secondary-{suffix}")
        repository = PostgresLocalPilotStorage(connection)
        evidence_store = ContentAddressedEvidenceStore(tmp_path / "objects")
        job, source_ref = _claim_normalized_job(
            repository=repository,
            evidence_store=evidence_store,
            scope=scope,
            key=key,
            suffix=f"base-{suffix}",
            now=now,
        )
        first_item, first_normalized = _normalized_input(
            provider_event_id=f"provider-{suffix}",
            source_ref=source_ref,
        )
        second_item, second_normalized = _normalized_input(
            provider_event_id=f"provider-second-{suffix}",
            source_ref=source_ref,
        )

        first = repository.persist_normalized_batch(
            scope,
            key,
            job,
            (first_item,),
            (first_normalized,),
        )
        cross_batch = repository.persist_normalized_batch(
            scope,
            key,
            job,
            (first_item, second_item),
            (first_normalized, second_normalized),
        )
        assert first.observations[0].replayed is False
        assert cross_batch.observations[0].replayed is True
        assert cross_batch.observations[1].replayed is False

        rollback_item, rollback_normalized = _normalized_input(
            provider_event_id=f"provider-rollback-{suffix}",
            source_ref=source_ref,
        )
        _, conflicting_normalized = _normalized_input(
            provider_event_id=first_item.provider_event_id,
            source_ref=source_ref,
            correlation_suffix="-different",
        )
        with pytest.raises(NormalizedBatchConflict, match="payload conflict"):
            repository.persist_normalized_batch(
                scope,
                key,
                job,
                (rollback_item, first_item),
                (rollback_normalized, conflicting_normalized),
            )
        assert _normalized_counts(
            connection,
            scope,
            key,
            (rollback_item.provider_event_id,),
        ) == (0, 0, 0)

        retried = repository.retry_processing_job(
            scope,
            job_id=job.job_id,
            worker_id=str(job.lease_owner),
            expected_attempt=job.attempt_count,
            expected_lease_generation=job.lease_generation,
            now=now + timedelta(seconds=2),
            next_retry_at=now + timedelta(seconds=3),
            error_code="synthetic_crash",
        )
        assert retried.status == "retry_wait"
        reclaimed = repository.claim_processing_job(
            scope,
            worker_id=f"worker-retry-{suffix}",
            now=now + timedelta(seconds=4),
            lease_seconds=60,
        )
        assert reclaimed is not None
        replay_after_crash = repository.persist_normalized_batch(
            scope,
            key,
            reclaimed,
            (first_item, second_item),
            (first_normalized, second_normalized),
        )
        assert all(value.replayed for value in replay_after_crash.observations)
        repository.complete_processing_job(
            scope,
            job_id=reclaimed.job_id,
            worker_id=str(reclaimed.lease_owner),
            expected_attempt=reclaimed.attempt_count,
            expected_lease_generation=reclaimed.lease_generation,
            now=now + timedelta(seconds=5),
            provider_event_ids=(
                first_item.provider_event_id,
                second_item.provider_event_id,
            ),
        )
        assert _normalized_counts(
            connection,
            scope,
            key,
            (
                first_item.provider_event_id,
                second_item.provider_event_id,
            ),
        ) == (2, 2, 2)
        assert _normalized_retention_is_30_days(connection, scope, key)

        for isolated_scope, isolated_key, label in (
            (scope, other_key, "instance"),
            (other_scope, key, "site"),
        ):
            isolated_job, isolated_ref = _claim_normalized_job(
                repository=repository,
                evidence_store=evidence_store,
                scope=isolated_scope,
                key=isolated_key,
                suffix=f"{label}-{suffix}",
                now=now + timedelta(seconds=10),
            )
            isolated_item, isolated_normalized = _normalized_input(
                provider_event_id=first_item.provider_event_id,
                source_ref=isolated_ref,
            )
            isolated_result = repository.persist_normalized_batch(
                isolated_scope,
                isolated_key,
                isolated_job,
                (isolated_item,),
                (isolated_normalized,),
            )
            assert isolated_result.observations[0].replayed is False
            assert _normalized_counts(
                connection,
                isolated_scope,
                isolated_key,
                (isolated_item.provider_event_id,),
            ) == (1, 1, 1)
    finally:
        connection.close()


def _normalized_counts(
    connection: object,
    scope: ObserverTenantScope,
    key: ConnectorKey,
    provider_event_ids: tuple[str, ...],
) -> tuple[int, int, int]:
    with connection.transaction(), connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (scope.site_id,),
        )
        cursor.execute(
            """
            SELECT
                count(*),
                count(outbox.outbox_id),
                count(*) FILTER (
                    WHERE event.team_ref IS NULL AND event.party_ref IS NULL
                )
            FROM observer.observation_events AS event
            LEFT JOIN observer.context_publication_outbox AS outbox
              ON outbox.site_id = event.site_id
             AND outbox.observation_event_id = event.event_id
            WHERE event.site_id = %s
              AND event.connector = %s
              AND event.connector_instance_id = %s
              AND event.provider_event_id = ANY(%s::text[])
            """,
            (
                scope.site_id,
                key.connector,
                key.instance_id,
                list(provider_event_ids),
            ),
        )
        row = cursor.fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2])


def _normalized_retention_is_30_days(
    connection: object,
    scope: ObserverTenantScope,
    key: ConnectorKey,
) -> bool:
    with connection.transaction(), connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(
            "SELECT set_config('app.site_id', %s, true)",
            (scope.site_id,),
        )
        cursor.execute(
            """
            SELECT bool_and(
                event.retention_class = 'R1-operational'
                AND event.retention_until = event.ingested_at + interval '30 days'
                AND raw.retention_class = 'R1-operational'
                AND raw.retention_until = raw.created_at + interval '30 days'
            )
            FROM observer.observation_events AS event
            JOIN observer.raw_objects AS raw
              ON raw.site_id = event.site_id
             AND raw.object_id = event.raw_object_id
            WHERE event.site_id = %s
              AND event.connector = %s
              AND event.connector_instance_id = %s
            """,
            (scope.site_id, key.connector, key.instance_id),
        )
        row = cursor.fetchone()
        assert row is not None
        return row[0] is True


def test_failed_delivery_replay_is_bounded_cas_and_preserves_evidence(
    tmp_path: Path,
) -> None:
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
        scope = ObserverTenantScope(
            f"replay-cas-{suffix}",
            "observation_processing",
        )
        key = ConnectorKey("wecom", f"primary-{suffix}")
        wrong_key = ConnectorKey("wecom", f"other-{suffix}")
        repository = PostgresLocalPilotStorage(connection)
        evidence_store = ContentAddressedEvidenceStore(tmp_path / "objects")
        repository.register_connector_instance(scope, key, now=now)
        repository.register_connector_instance(scope, wrong_key, now=now)
        accepted = DurableDeliveryInbox(
            storage=repository,
            evidence_store=evidence_store,
        ).accept(
            scope,
            key,
            RawDelivery(
                f"delivery-{suffix}",
                b'{"failed":"fixture"}',
                "application/json",
                now,
            ),
            correlation_id=f"corr-{suffix}",
            max_attempts=1,
        )
        claimed = repository.claim_processing_job(
            scope,
            worker_id=f"worker-{suffix}",
            now=now + timedelta(seconds=1),
            lease_seconds=60,
        )
        assert claimed is not None
        repository.retry_processing_job(
            scope,
            job_id=claimed.job_id,
            worker_id=str(claimed.lease_owner),
            expected_attempt=claimed.attempt_count,
            expected_lease_generation=claimed.lease_generation,
            now=now + timedelta(seconds=2),
            next_retry_at=now + timedelta(seconds=3),
            error_code="synthetic_terminal_failure",
        )
        failed = repository.get_inbound_delivery(
            scope,
            key,
            delivery_id=accepted.delivery.delivery_id,
        )
        assert failed.processing_status == "failed"
        evidence_metadata = (
            failed.exact_body_sha256,
            failed.object_ref,
            failed.byte_size,
            failed.media_type,
            failed.received_at,
        )

        replayed = repository.replay_delivery(
            scope,
            key,
            delivery_id=failed.delivery_id,
            job_id=f"replay-{suffix}",
            idempotency_key=f"replay:ticket-{suffix}",
            now=now + timedelta(seconds=3),
            max_attempts=2,
        )
        same_replay = repository.replay_delivery(
            scope,
            key,
            delivery_id=failed.delivery_id,
            job_id=f"ignored-{suffix}",
            idempotency_key=f"replay:ticket-{suffix}",
            now=now + timedelta(seconds=4),
            max_attempts=2,
        )
        assert same_replay == replayed
        queued = repository.get_inbound_delivery(
            scope,
            key,
            delivery_id=failed.delivery_id,
        )
        assert queued.processing_status == "queued"
        assert (
            queued.exact_body_sha256,
            queued.object_ref,
            queued.byte_size,
            queued.media_type,
            queued.received_at,
        ) == evidence_metadata
        with pytest.raises(DeliveryConflict, match="eligible failed"):
            repository.replay_delivery(
                scope,
                wrong_key,
                delivery_id=failed.delivery_id,
                job_id=f"wrong-instance-{suffix}",
                idempotency_key=f"replay:wrong-instance-{suffix}",
                now=now + timedelta(seconds=4),
                max_attempts=2,
            )

        reclaimed = repository.claim_processing_job(
            scope,
            worker_id=f"replay-worker-{suffix}",
            now=now + timedelta(seconds=5),
            lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.job_id == replayed.job_id
        repository.complete_processing_job(
            scope,
            job_id=reclaimed.job_id,
            worker_id=str(reclaimed.lease_owner),
            expected_attempt=reclaimed.attempt_count,
            expected_lease_generation=reclaimed.lease_generation,
            now=now + timedelta(seconds=6),
            provider_event_ids=(),
        )
        succeeded = repository.get_inbound_delivery(
            scope,
            key,
            delivery_id=failed.delivery_id,
        )
        assert succeeded.processing_status == "succeeded"
        assert (
            succeeded.exact_body_sha256,
            succeeded.object_ref,
            succeeded.byte_size,
            succeeded.media_type,
            succeeded.received_at,
        ) == evidence_metadata

        old_scope = ObserverTenantScope(
            f"replay-old-{suffix}",
            "observation_processing",
        )
        old_key = ConnectorKey("wecom", f"old-{suffix}")
        old_now = now - timedelta(days=31)
        repository.register_connector_instance(old_scope, old_key, now=old_now)
        old_accepted = DurableDeliveryInbox(
            storage=repository,
            evidence_store=evidence_store,
        ).accept(
            old_scope,
            old_key,
            RawDelivery(
                f"old-delivery-{suffix}",
                b'{"old":"fixture"}',
                "application/json",
                old_now,
            ),
            correlation_id=f"old-corr-{suffix}",
            max_attempts=1,
        )
        old_claimed = repository.claim_processing_job(
            old_scope,
            worker_id=f"old-worker-{suffix}",
            now=old_now + timedelta(seconds=1),
            lease_seconds=60,
        )
        assert old_claimed is not None
        repository.retry_processing_job(
            old_scope,
            job_id=old_claimed.job_id,
            worker_id=str(old_claimed.lease_owner),
            expected_attempt=old_claimed.attempt_count,
            expected_lease_generation=old_claimed.lease_generation,
            now=old_now + timedelta(seconds=2),
            next_retry_at=old_now + timedelta(seconds=3),
            error_code="synthetic_terminal_failure",
        )
        with pytest.raises(DeliveryConflict, match="eligible failed"):
            repository.replay_delivery(
                old_scope,
                old_key,
                delivery_id=old_accepted.delivery.delivery_id,
                job_id=f"old-replay-{suffix}",
                idempotency_key=f"replay:old-{suffix}",
                now=now,
                max_attempts=2,
            )
    finally:
        connection.close()


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
