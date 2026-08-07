from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.observer.observer.control_service import (
    LocalPilotControlService,
    PostgresControlRepository,
)
from services.observer.observer.local_pilot_storage import PostgresLocalPilotStorage
from services.observer.observer.model_projection import (
    CommunicationIntelligenceResponse,
    LocalTokenizationResult,
    ObservationModelRequest,
    ObservationProjectionPublisher,
    PostgresObservationProjectionRepository,
)
from services.observer.observer.models import ConnectorKey, TenantScope
from services.observer.observer.read_service import (
    CommunicationAccess,
    LocalPilotReadService,
    PostgresCommunicationRepository,
)
from services.observer.observer.scheduler import PostgresPollingState
from services.observer.observer.storage import connect_postgres_components

RUN_INTEGRATION = os.getenv("GBOS_RUN_POSTGRES_INTEGRATION") == "1"
POSTGRES_CONTAINER = os.getenv("GBOS_GATE3_POSTGRES_CONTAINER")
HOST = os.getenv("GBOS_GATE3_CONTEXT_HOST")
PORT = os.getenv("GBOS_GATE3_CONTEXT_PORT")
DATABASE = os.getenv("GBOS_GATE3_CONTEXT_DATABASE")
PASSWORD = os.getenv("GBOS_GATE3_CONTEXT_PASSWORD")
USER = os.getenv("GBOS_GATE3_OBSERVER_USER", "gbos_observer_app")
ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services" / "observer" / "migrations" / "006_local_pilot_control.sql"
ROUTING_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "007_local_pilot_connector_routing.sql"
)

pytestmark = [pytest.mark.postgres_integration]
if not RUN_INTEGRATION:
    pytestmark.append(pytest.mark.skip(reason="set GBOS_RUN_POSTGRES_INTEGRATION=1 to run"))


def _container_sql(sql: str) -> None:
    if not POSTGRES_CONTAINER:
        pytest.fail("GBOS_GATE3_POSTGRES_CONTAINER is required")
    subprocess.run(
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
        check=True,
        capture_output=True,
        text=True,
    )


def _connect() -> object:
    if not all((HOST, PORT, DATABASE, PASSWORD)):
        pytest.fail("complete GBOS_GATE3_CONTEXT_* connection settings are required")
    return connect_postgres_components(
        host=HOST,
        port=int(PORT),
        database=DATABASE,
        user=USER,
        password=PASSWORD,
    )


def test_postgres_control_communication_and_polling_survive_repository_restart() -> None:
    _container_sql(MIGRATION.read_text(encoding="utf-8"))
    _container_sql(ROUTING_MIGRATION.read_text(encoding="utf-8"))
    suffix = uuid.uuid4().hex[:12]
    scope = TenantScope(f"runtime-{suffix}", "observation_processing")
    other_scope = TenantScope(f"other-{suffix}", "observation_processing")
    wrong_purpose_scope = TenantScope(scope.site_id, "audit_compliance")
    key = ConnectorKey("email", f"inbox-{suffix}")
    now = datetime.now(UTC)
    connection = _connect()
    try:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_user, role.rolbypassrls
                FROM pg_roles AS role
                WHERE role.rolname = current_user
                """
            )
            identity = cursor.fetchone()
        assert identity == ("gbos_observer_app", False)
        storage = PostgresLocalPilotStorage(connection)
        storage.register_connector_instance(
            scope,
            key,
            now=now,
            replay_window_seconds=30 * 24 * 60 * 60,
        )
        first_repository = PostgresControlRepository(
            connection=connection,
            replay_storage=storage,
        )
        first = LocalPilotControlService(
            repository=first_repository,
            clock=lambda: now,
        ).pause(
            scope,
            key,
            expected_revision=0,
            idempotency_key=f"pause-{suffix}",
        )
        restarted_connection = _connect()
        try:
            restarted_storage = PostgresLocalPilotStorage(restarted_connection)
            restarted_repository = PostgresControlRepository(
                connection=restarted_connection,
                replay_storage=restarted_storage,
            )
            replayed = LocalPilotControlService(
                repository=restarted_repository,
                clock=lambda: now,
            ).pause(
                scope,
                key,
                expected_revision=0,
                idempotency_key=f"pause-{suffix}",
            )
            assert (
                restarted_repository.resolve_connector(
                    scope,
                    instance_id=key.instance_id,
                )
                == key
            )
            assert restarted_repository.list_status(other_scope, channel=None) == ()
        finally:
            restarted_connection.close()
        assert first.status.status == "paused"
        assert first.status.revision == 1
        assert replayed.replayed is True
        assert replayed.status.revision == 1
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            for label, received_at in (
                ("eligible", now - timedelta(days=1)),
                ("expired", now - timedelta(days=31)),
            ):
                replay_digest = (label[0] * 52) + suffix
                cursor.execute(
                    """
                    INSERT INTO observer.inbound_deliveries (
                      site_id, connector, connector_instance_id, delivery_id,
                      exact_body_sha256, object_ref, byte_size, media_type,
                      received_at, processing_status, attempt_count,
                      correlation_id, created_at, updated_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, 8, 'message/rfc822',
                      %s, 'failed', 1, %s, %s, %s
                    )
                    """,
                    (
                        scope.site_id,
                        key.connector,
                        key.instance_id,
                        f"{label}-{suffix}",
                        replay_digest,
                        f"obs:v1:{suffix}:sha256:{replay_digest}",
                        received_at,
                        f"corr-{label}-{suffix}",
                        received_at,
                        received_at,
                    ),
                )
        replay_command = LocalPilotControlService(
            repository=first_repository,
            clock=lambda: now,
        )
        replay_result = replay_command.replay(
            scope,
            key,
            expected_revision=1,
            idempotency_key=f"replay-{suffix}",
        )
        replay_after_restart = LocalPilotControlService(
            repository=PostgresControlRepository(
                connection=connection,
                replay_storage=storage,
            ),
            clock=lambda: now,
        ).replay(
            scope,
            key,
            expected_revision=1,
            idempotency_key=f"replay-{suffix}",
        )
        assert replay_result.replayed_count == 1
        assert replay_result.status.revision == 2
        assert replay_after_restart.replayed is True
        assert replay_after_restart.replayed_count == 1

        polling = PostgresPollingState(
            connection=connection,
            storage=storage,
            durable_accept=lambda _scope, _key, _delivery: None,
        )
        assert polling.load_checkpoint(scope, key) == (None, 0, "paused")
        resumed = replay_command.resume(
            scope,
            key,
            expected_revision=2,
            idempotency_key=f"resume-{suffix}",
        )
        assert resumed.status.status == "enabled"
        assert resumed.status.revision == 3
        assert polling.load_checkpoint(scope, key) == (None, 0, "healthy")

        event_id = f"event-{suffix}"
        raw_id = f"raw-{suffix}"
        evidence_id = f"evidence-{suffix}"
        digest = suffix.ljust(64, "a")
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            cursor.execute(
                """
                INSERT INTO observer.raw_objects (
                  site_id, object_id, object_ref, sha256, media_type,
                  byte_size, retention_class, created_at, retention_until
                ) VALUES (%s, %s, %s, %s, 'message/rfc822', 8, 'R1', %s, %s)
                """,
                (
                    scope.site_id,
                    raw_id,
                    f"obs:v1:{suffix}:sha256:{digest}",
                    digest,
                    now,
                    now + timedelta(days=30),
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.observation_events (
                  site_id, event_id, connector, channel, processing_purpose,
                  consent_basis, data_classification, retention_class,
                  correlation_id, occurred_at, ingested_at, document,
                  raw_object_id, retention_until, team_ref, party_ref
                ) VALUES (
                  %s, %s, 'manual_import', 'email', %s, 'pilot',
                  'Restricted', 'R1', %s, %s, %s, '{}'::jsonb,
                  %s, %s, 'team-sales', 'party-1'
                )
                """,
                (
                    scope.site_id,
                    event_id,
                    scope.processing_purpose,
                    f"corr-{suffix}",
                    now,
                    now,
                    raw_id,
                    now + timedelta(days=30),
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.participants (
                  site_id, event_id, participant_id, role, identity_ref
                ) VALUES (%s, %s, %s, 'external', 'actor@example.com')
                """,
                (scope.site_id, event_id, f"participant-{suffix}"),
            )
            cursor.execute(
                """
                INSERT INTO observer.evidence_refs (
                  site_id, evidence_id, event_id, raw_object_id, raw_sha256,
                  media_type, locator, created_at, content_object_ref
                ) VALUES (
                  %s, %s, %s, %s, %s, 'message/rfc822',
                  '{"locator":"message"}'::jsonb, %s, %s
                )
                """,
                (
                    scope.site_id,
                    evidence_id,
                    event_id,
                    raw_id,
                    digest,
                    now,
                    f"obs:v1:{suffix}:sha256:{digest}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO observer.event_evidence (
                  site_id, event_id, evidence_id, evidence_ordinal
                ) VALUES (%s, %s, %s, 0)
                """,
                (scope.site_id, event_id, evidence_id),
            )

        provider_requests: list[ObservationModelRequest] = []

        class Provider:
            def project(
                self,
                request: ObservationModelRequest,
            ) -> CommunicationIntelligenceResponse:
                provider_requests.append(request)
                return CommunicationIntelligenceResponse(
                    output={
                        "schema_version": "1.0",
                        "site_id": scope.site_id,
                        "observation_id": event_id,
                        "evidence_refs": [evidence_id],
                        "summary_zh": "客户询问交期",
                        "original_language": "zh-CN",
                        "confidence": 0.92,
                        "review_status": "AI Draft",
                        "fact_proposals": [
                            {
                                "subject_ref": "party-1",
                                "predicate": "delivery_intent",
                                "value_display": "询问交期",
                                "type": "text",
                                "unit": None,
                                "confidence": 0.9,
                                "evidence_refs": [evidence_id],
                                "status": "proposed",
                            }
                        ],
                        "association_suggestions": [
                            {
                                "type": "party",
                                "target_ref": "party-1",
                                "confidence": 0.88,
                                "evidence_refs": [evidence_id],
                            }
                        ],
                    },
                    model_name="deepseek-v4-flash",
                    model_version="2026-08-08",
                    invocation_refs=(f"invocation-{suffix}",),
                )

        context_keys: set[str] = set()

        class ContextPublisher:
            def publish(
                self,
                published_scope: TenantScope,
                publication: object,
                *,
                idempotency_key: str,
            ) -> None:
                assert published_scope == scope
                assert publication.fact_proposals[0]["evidence_refs"] == [evidence_id]
                assert publication.model == {
                    "name": "deepseek-v4-flash",
                    "version": "2026-08-08",
                }
                assert publication.invocation_refs == (f"invocation-{suffix}",)
                context_keys.add(idempotency_key)

        def tokenize(
            token_scope: TenantScope,
            observation_id: str,
            raw_text: str,
        ) -> LocalTokenizationResult:
            assert token_scope == scope and observation_id == event_id
            assert raw_text == "raw customer message"
            return LocalTokenizationResult(
                text="tokenized customer message",
                receipt_ref=f"tokenization-{suffix}",
                tokenizer_version="stable-tokenizer-v1",
                mapping_digest="b" * 64,
            )

        def project(
            active_connection: object,
            *,
            projected_at: datetime,
        ) -> None:
            projection_store = PostgresCommunicationRepository(connection=active_connection)
            projection_repository = PostgresObservationProjectionRepository(
                connection=active_connection,
                projection_store=projection_store,
                raw_loader=lambda _scope, _ref: "raw customer message",
            )
            publisher = ObservationProjectionPublisher(
                repository=projection_repository,
                raw_loader=projection_repository.raw_loader,
                tokenizer=tokenize,
                provider=Provider(),
                context_publisher=ContextPublisher(),
                clock=lambda: projected_at,
                restricted_policy="local_tokenized",
            )
            publisher(
                scope,
                event_id,
                f"context-normalized:{event_id}",
            )

        project(connection, projected_at=now)
        projection_restart = _connect()
        try:
            project(
                projection_restart,
                projected_at=now + timedelta(hours=1),
            )
        finally:
            projection_restart.close()
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.site_id', %s, true)",
                (scope.site_id,),
            )
            cursor.execute(
                """
                SELECT count(*), min(projected_at), max(projected_at)
                FROM observer.communication_projections
                WHERE site_id = %s AND observation_event_id = %s
                """,
                (scope.site_id, event_id),
            )
            projection_count = cursor.fetchone()
        assert projection_count == (1, now, now)
        assert context_keys == {f"context-normalized:{event_id}"}
        assert len(provider_requests) == 2
        assert all(
            request.input_mode == "local_tokenized"
            and request.evidence_refs == (evidence_id,)
            and request.tokenization_refs == (f"tokenization-{suffix}",)
            and request.mapping_digest == "b" * 64
            for request in provider_requests
        )
        read_service = LocalPilotReadService(
            repository=PostgresCommunicationRepository(connection=connection),
            cursor_secret=b"x" * 32,
        )
        allowed = read_service.list_communications(
            scope,
            CommunicationAccess(team_refs=frozenset({"team-sales"})),
        )
        denied = read_service.list_communications(
            scope,
            CommunicationAccess(team_refs=frozenset({"team-finance"})),
        )
        cross_site = read_service.list_communications(
            other_scope,
            CommunicationAccess(
                team_refs=frozenset({"*"}),
                allow_all_teams=True,
            ),
        )
        cross_purpose = read_service.list_communications(
            wrong_purpose_scope,
            CommunicationAccess(
                team_refs=frozenset({"*"}),
                allow_all_teams=True,
            ),
        )
        detail = read_service.get_communication(
            scope,
            CommunicationAccess(team_refs=frozenset({"team-sales"})),
            observation_id=event_id,
        )
        assert [row.observation_id for row in allowed.communications] == [event_id]
        assert denied.communications == ()
        assert cross_site.communications == ()
        assert cross_purpose.communications == ()
        assert detail.evidence == (
            {
                "ref": evidence_id,
                "locator": f"obs:v1:{suffix}:sha256:{digest}",
            },
        )
        assert detail.summary.classification == "Restricted"
        assert detail.summary.review_status == "AI Draft"
        assert detail.fact_proposals == (
            {
                "status": "proposed",
                "confidence": 0.9,
                "type": "text",
                "value_display": "询问交期",
            },
        )
        assert detail.original_text is None
    finally:
        connection.close()
