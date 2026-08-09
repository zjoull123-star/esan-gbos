from __future__ import annotations

import hashlib
import importlib
import re
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from observer.local_pilot_storage import (
    AuthenticatedIngressMetadata,
    CheckpointConflict,
    DeliveryConflict,
    IngressExpired,
    JobConflict,
    LeaseConflict,
    NonceReplay,
    NormalizedBatchConflict,
    OutboxConflict,
    PostgresLocalPilotStorage,
    ProcessingJobMetadata,
)
from observer.models import (
    ConnectorItem,
    ConnectorKey,
    EvidenceArtifact,
    NormalizedObservationInput,
    Participant,
    TenantScope,
)

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services" / "observer" / "migrations" / "003_local_pilot_runtime.sql"
INGESTION_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "004_local_pilot_ingestion.sql"
)
NORMALIZED_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "005_local_pilot_normalized_sink.sql"
)
ROUTING_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "007_local_pilot_connector_routing.sql"
)
IDENTITY_MIGRATION = (
    ROOT / "services" / "observer" / "migrations" / "009_local_pilot_identity_resolution.sql"
)
MIGRATION_SCRIPT = ROOT / "scripts" / "dev" / "gate3-migrate"
NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("wecom", "sales-primary")


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._many: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        upper = normalized.upper()
        if ("RETURNING " in upper or upper.startswith("SELECT")) and "SET_CONFIG(" not in upper:
            response = self.connection.responses.pop(0)
            if isinstance(response, list):
                self._many = response
                self._one = response[0] if response else None
            else:
                self._one = response
                self._many = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._many


class FakeConnection:
    def __init__(
        self,
        responses: list[tuple[Any, ...] | list[tuple[Any, ...]] | None],
    ) -> None:
        self.responses = responses
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


def _connector_row(
    *,
    connector: str = KEY.connector,
    instance_id: str = KEY.instance_id,
    status: str = "healthy",
    control_revision: int = 0,
    team_ref: str | None = None,
    agent_task_type: str | None = None,
    account_user_ref: str | None = None,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        connector,
        instance_id,
        status,
        NOW,
        NOW,
        control_revision,
        team_ref,
        agent_task_type,
        account_user_ref,
    )


def _delivery_row(
    *,
    digest: str = "a" * 64,
    instance_id: str = KEY.instance_id,
    object_ref: str = "obs:v1:site-partition:sha256:" + "a" * 64,
    byte_size: int = 17,
    media_type: str = "application/json",
    status: str = "queued",
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        KEY.connector,
        instance_id,
        "delivery-001",
        digest,
        object_ref,
        byte_size,
        media_type,
        NOW,
        status,
        0,
        "corr-001",
        None,
        None,
        NOW,
        NOW,
    )


def _routing_row(
    *,
    control_revision: int = 0,
    team_ref: str | None = None,
    agent_task_type: str | None = None,
) -> tuple[Any, ...]:
    return control_revision, team_ref, agent_task_type


def _job_row(
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    generation: int = 0,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    lease_generation: int = 0,
    next_retry_at: datetime | None = None,
    error_code: str | None = None,
    delivery_id: str = "delivery-001",
    idempotency_key: str = "delivery:wecom:sales-primary:delivery-001:g0",
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        "job-001",
        KEY.connector,
        KEY.instance_id,
        delivery_id,
        "normalize",
        status,
        attempt_count,
        max_attempts,
        idempotency_key,
        generation,
        lease_owner,
        lease_expires_at,
        lease_generation,
        next_retry_at,
        error_code,
        NOW,
        NOW,
    )


def _checkpoint_row(
    *,
    version: int = 0,
    cursor_value: str | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        KEY.connector,
        KEY.instance_id,
        f"{KEY.connector}:{KEY.instance_id}",
        cursor_value,
        version,
        60,
        lease_owner,
        lease_expires_at,
        None,
        None,
        "healthy",
        NOW,
    )


def _outbox_row(
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    error_code: str | None = None,
) -> tuple[Any, ...]:
    return (
        SCOPE.site_id,
        "outbox-001",
        "01K20B8BV5C6P4YFAT8YQ3D4S5",
        "context:event-001",
        "b" * 64,
        status,
        attempt_count,
        max_attempts,
        NOW,
        lease_owner,
        lease_expires_at,
        error_code,
        NOW,
        NOW,
    )


def test_local_pilot_migration_is_metadata_only_instance_scoped_and_rls_forced() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in (
        "connector_instances",
        "inbound_deliveries",
        "inbound_delivery_events",
        "connector_checkpoints",
        "persistent_nonces",
        "processing_jobs",
        "context_publication_outbox",
        "local_pilot_quarantine",
        "local_pilot_dead_letter",
    ):
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql

    assert "add column if not exists connector_instance_id" in sql
    assert "default 'legacy-manual-import'" in sql
    assert "observation_events_site_id_connector_provider_event_id_key" in sql
    assert "site_id, connector, connector_instance_id, provider_event_id" in sql
    assert "site_id, connector, connector_instance_id, raw_sha256, occurred_minute" in sql
    assert "drop index if exists observer.observation_events_fallback_dedup_uq" in sql
    assert "gbos_observer_app" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "occurred_at" not in _table_columns(sql, "connector_checkpoints")

    forbidden_columns = re.compile(
        r"(?m)^\s*(raw_body|exact_bytes|secret|prompt|response|phone|email)\s+"
    )
    for table in (
        "connector_instances",
        "inbound_deliveries",
        "inbound_delivery_events",
        "connector_checkpoints",
        "persistent_nonces",
        "processing_jobs",
        "context_publication_outbox",
        "local_pilot_quarantine",
        "local_pilot_dead_letter",
    ):
        assert forbidden_columns.search(_table_columns(sql, table)) is None


def test_gate3_migration_runner_discovers_the_additive_local_pilot_migration() -> None:
    script = MIGRATION_SCRIPT.read_text(encoding="utf-8")

    assert "/migrations/observer/[0-9][0-9][0-9]_*.sql" in script


def test_ingestion_migration_is_additive_repeatable_and_preserves_forced_rls() -> None:
    sql = INGESTION_MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter table observer.inbound_deliveries" in sql
    assert "add column if not exists object_ref" in sql
    assert "add column if not exists byte_size" in sql
    for column in (
        "idempotency_key",
        "generation",
        "lease_owner",
        "lease_expires_at",
        "lease_generation",
    ):
        assert f"add column if not exists {column}" in sql
    assert "create unique index if not exists processing_jobs_idempotency_uq" in sql
    assert "create index if not exists processing_jobs_claim_idx" in sql
    assert "force row level security" in sql
    assert "raw_body" not in sql
    assert "exact_bytes" not in sql


def test_normalized_sink_migration_separates_connector_jobs_without_touching_manual_import() -> (
    None
):
    sql = NORMALIZED_MIGRATION.read_text(encoding="utf-8").lower()

    for column in (
        "processing_job_id",
        "delivery_id",
        "team_ref",
        "party_ref",
        "normalized_payload_sha256",
        "retention_until",
    ):
        assert f"add column if not exists {column}" in sql
    assert "connector <> 'manual_import'" in sql
    assert "job_id is null" in sql
    assert "processing_job_id is not null" in sql
    assert "references observer.processing_jobs (site_id, job_id)" in sql
    assert "references observer.inbound_deliveries" in sql
    assert "site_id, connector, connector_instance_id, provider_event_id" in sql
    assert "add column if not exists content_object_ref" in sql
    assert "old.processing_status = 'failed'" in sql
    assert "new.processing_status = 'queued'" in sql
    for table in (
        "observation_events",
        "evidence_refs",
        "context_publication_outbox",
    ):
        assert f"alter table observer.{table} force row level security" in sql
    assert "grant select, insert on" in sql
    assert "drop table" not in sql
    assert "delete from observer.manual_import" not in sql


def test_connector_routing_migration_is_repeatable_bounded_and_site_scoped() -> None:
    sql = ROUTING_MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists team_ref" in sql
    assert "add column if not exists agent_task_type" in sql
    assert "char_length(team_ref) between 1 and 256" in sql
    assert "team_ref !~ e'[\\r\\n]'" in sql
    assert "agent_task_type in (" in sql
    for task_type in ("sales", "purchase", "product_sample", "ceo"):
        assert f"'{task_type}'" in sql
    assert "agent_task_type is null or team_ref is not null" in sql
    assert "create index if not exists connector_instances_routing_idx" in sql
    assert "alter table observer.connector_instances enable row level security" in sql
    assert "alter table observer.connector_instances force row level security" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "grant select, insert, update on observer.connector_instances" in sql
    assert "drop table" not in sql
    assert "delete from observer.connector_instances" not in sql


def _normalized_item(
    provider_event_id: str,
    *,
    source_ref: str = "obs:v1:site-partition:sha256:" + "a" * 64,
    role: str = "unknown",
) -> tuple[ConnectorItem, NormalizedObservationInput]:
    item = ConnectorItem(
        provider_event_id=provider_event_id,
        occurred_at=NOW,
        source_cursor=f"cursor:{provider_event_id}",
        payload={"opaque": True},
    )
    normalized = NormalizedObservationInput(
        channel="chat",
        participants=(
            Participant(
                role=role,
                identity_ref=f"unresolved:delivery:{hashlib.sha256(provider_event_id.encode()).hexdigest()}",
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
        correlation_id=f"corr-{provider_event_id}",
    )
    return item, normalized


def _processing_job() -> ProcessingJobMetadata:
    return ProcessingJobMetadata(
        site_id=SCOPE.site_id,
        job_id="job-001",
        connector=KEY.connector,
        connector_instance_id=KEY.instance_id,
        delivery_id="delivery-001",
        stage="normalize",
        status="processing",
        attempt_count=1,
        max_attempts=3,
        idempotency_key="delivery:wecom:sales-primary:delivery-001:g0",
        generation=0,
        lease_owner="worker-a",
        lease_expires_at=NOW + timedelta(seconds=30),
        lease_generation=1,
        next_retry_at=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_persist_normalized_batch_uses_one_transaction_and_one_outbox_per_event() -> None:
    item1, normalized1 = _normalized_item("event-001")
    item2, normalized2 = _normalized_item("event-002")
    connection = FakeConnection(
        [
            _routing_row(team_ref="team:db-sales", agent_task_type="sales"),
            _delivery_row(status="processing"),
            [(None,), (None,)],
            [],
            ("raw-object-001", "obs:v1:site-partition:sha256:" + "a" * 64),
        ]
    )

    result = PostgresLocalPilotStorage(connection).persist_normalized_batch(
        SCOPE,
        KEY,
        _processing_job(),
        (item1, item2),
        (normalized1, normalized2),
    )

    assert connection.transactions == 1
    assert tuple(value.provider_event_id for value in result.observations) == (
        "event-001",
        "event-002",
    )
    inserts = [sql for sql, _ in connection.executed]
    assert any("pg_advisory_xact_lock" in sql for sql in inserts)
    routing_select = next(
        sql for sql in inserts if "SELECT control_revision, team_ref, agent_task_type" in sql
    )
    assert "FOR UPDATE" in routing_select
    raw_insert = next(sql for sql in inserts if "INSERT INTO observer.raw_objects" in sql)
    assert "DO NOTHING" in raw_insert
    assert "DO UPDATE" not in raw_insert
    assert "retention_until" in raw_insert
    event_insert = next(sql for sql in inserts if "INSERT INTO observer.observation_events" in sql)
    assert "retention_until" in event_insert
    assert "date_trunc('minute', %s::timestamptz), %s, NULL, %s" in event_insert
    event_params = [
        params
        for sql, params in connection.executed
        if "INSERT INTO observer.observation_events" in sql
    ]
    assert all(params is not None and params[-2] == "team:db-sales" for params in event_params)
    assert sum("INSERT INTO observer.observation_events" in sql for sql in inserts) == 2
    assert sum("INSERT INTO observer.context_publication_outbox" in sql for sql in inserts) == 2
    assert all(value.replayed is False for value in result.observations)


def test_persist_normalized_batch_uses_each_materialized_evidence_own_digest() -> None:
    item, original = _normalized_item("email-event-001")
    email_key = ConnectorKey("email", KEY.instance_id)
    email_job = replace(_processing_job(), connector="email")
    partition = hashlib.sha256(f"site:{SCOPE.site_id}".encode()).hexdigest()[:32]
    body_digest = "b" * 64
    attachment_digest = "c" * 64
    normalized = NormalizedObservationInput(
        channel="email",
        participants=original.participants,
        evidence=(
            original.evidence[0],
            EvidenceArtifact(
                media_type="text/plain; charset=utf-8",
                locator="message-body",
                role="derived-text",
                reference=f"obs:v1:{partition}:sha256:{body_digest}",
            ),
            EvidenceArtifact(
                media_type="application/octet-stream",
                locator="attachment:1",
                role="attachment",
                reference=f"obs:v1:{partition}:sha256:{attachment_digest}",
            ),
        ),
        consent_basis=original.consent_basis,
        data_classification=original.data_classification,
        retention_class=original.retention_class,
        original_language=original.original_language,
        correlation_id=original.correlation_id,
    )
    connection = FakeConnection(
        [
            _routing_row(),
            _delivery_row(status="processing"),
            [(None,)],
            [],
            ("raw-object-001", "obs:v1:site-partition:sha256:" + "a" * 64),
        ]
    )

    PostgresLocalPilotStorage(connection).persist_normalized_batch(
        SCOPE,
        email_key,
        email_job,
        (item,),
        (normalized,),
    )

    evidence_params = [
        params for sql, params in connection.executed if "INSERT INTO observer.evidence_refs" in sql
    ]
    assert [params[4] for params in evidence_params if params is not None] == [
        "a" * 64,
        body_digest,
        attachment_digest,
    ]
    assert [params[8] for params in evidence_params if params is not None] == [
        original.evidence[0].reference,
        f"obs:v1:{partition}:sha256:{body_digest}",
        f"obs:v1:{partition}:sha256:{attachment_digest}",
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/private-body",
        "obs:v1:wrong-partition:sha256:" + "b" * 64,
        "obs:v1:" + "f" * 32 + ":sha256:" + "b" * 63,
    ],
)
def test_persist_normalized_batch_rejects_untrusted_derived_evidence_reference(
    reference: str,
) -> None:
    item, original = _normalized_item("email-event-unsafe")
    email_key = ConnectorKey("email", KEY.instance_id)
    email_job = replace(_processing_job(), connector="email")
    normalized = NormalizedObservationInput(
        channel="email",
        participants=original.participants,
        evidence=(
            original.evidence[0],
            EvidenceArtifact(
                media_type="text/plain; charset=utf-8",
                locator="message-body",
                role="derived-text",
                reference=reference,
            ),
        ),
        consent_basis=original.consent_basis,
        data_classification=original.data_classification,
        retention_class=original.retention_class,
        original_language=original.original_language,
        correlation_id=original.correlation_id,
    )
    connection = FakeConnection([])

    with pytest.raises(ValueError, match="evidence reference"):
        PostgresLocalPilotStorage(connection).persist_normalized_batch(
            SCOPE,
            email_key,
            email_job,
            (item,),
            (normalized,),
        )

    assert connection.transactions == 0


def test_persist_normalized_batch_rejects_source_cas_ref_digest_mismatch() -> None:
    partition = hashlib.sha256(f"site:{SCOPE.site_id}".encode()).hexdigest()[:32]
    mismatched_ref = f"obs:v1:{partition}:sha256:" + "b" * 64
    item, normalized = _normalized_item(
        "event-source-mismatch",
        source_ref=mismatched_ref,
    )
    connection = FakeConnection(
        [
            _routing_row(),
            _delivery_row(
                status="processing",
                digest="a" * 64,
                object_ref=mismatched_ref,
            ),
        ]
    )

    with pytest.raises(ValueError, match="source evidence"):
        PostgresLocalPilotStorage(connection).persist_normalized_batch(
            SCOPE,
            KEY,
            _processing_job(),
            (item,),
            (normalized,),
        )

    assert not any(
        "INSERT INTO observer.observation_events" in sql for sql, _ in connection.executed
    )


def test_persist_normalized_batch_replay_returns_original_and_conflict_is_fail_closed() -> None:
    item, normalized = _normalized_item("event-001")
    probe = FakeConnection(
        [
            _routing_row(),
            _delivery_row(status="processing"),
            [(None,)],
            [],
            ("raw-object-001", "obs:v1:site-partition:sha256:" + "a" * 64),
        ]
    )
    original = (
        PostgresLocalPilotStorage(probe)
        .persist_normalized_batch(
            SCOPE,
            KEY,
            _processing_job(),
            (item,),
            (normalized,),
        )
        .observations[0]
    )

    replay = FakeConnection(
        [
            _routing_row(),
            _delivery_row(status="processing"),
            [(None,)],
            [
                (
                    item.provider_event_id,
                    original.event_id,
                    original.payload_sha256,
                    original.outbox_id,
                )
            ],
        ]
    )
    replayed = PostgresLocalPilotStorage(replay).persist_normalized_batch(
        SCOPE,
        KEY,
        _processing_job(),
        (item,),
        (normalized,),
    )
    assert replayed.observations[0].event_id == original.event_id
    assert replayed.observations[0].replayed is True
    assert not any(
        "INSERT INTO observer.observation_events" in sql for sql, _params in replay.executed
    )
    assert not any(
        "INSERT INTO observer.context_publication_outbox" in sql for sql, _params in replay.executed
    )

    conflict = FakeConnection(
        [
            _routing_row(),
            _delivery_row(status="processing"),
            [(None,)],
            [
                (
                    item.provider_event_id,
                    original.event_id,
                    "f" * 64,
                    original.outbox_id,
                )
            ],
        ]
    )
    with pytest.raises(NormalizedBatchConflict, match="payload conflict"):
        PostgresLocalPilotStorage(conflict).persist_normalized_batch(
            SCOPE,
            KEY,
            _processing_job(),
            (item,),
            (normalized,),
        )
    assert not any(
        "INSERT INTO observer.observation_events" in sql for sql, _params in conflict.executed
    )


def test_persist_normalized_batch_preserves_null_db_team_routing() -> None:
    item, normalized = _normalized_item("event-null-route")
    connection = FakeConnection(
        [
            _routing_row(),
            _delivery_row(status="processing"),
            [(None,)],
            [],
            ("raw-object-001", "obs:v1:site-partition:sha256:" + "a" * 64),
        ]
    )

    PostgresLocalPilotStorage(connection).persist_normalized_batch(
        SCOPE,
        KEY,
        _processing_job(),
        (item,),
        (normalized,),
    )

    event_params = next(
        params
        for sql, params in connection.executed
        if "INSERT INTO observer.observation_events" in sql
    )
    assert event_params is not None
    assert event_params[-2] is None


@pytest.mark.parametrize(
    "routing_row",
    [
        None,
        _routing_row(team_ref=None, agent_task_type="sales"),
        _routing_row(team_ref="team\ninvalid", agent_task_type=None),
    ],
)
def test_persist_normalized_batch_fails_closed_for_missing_or_invalid_db_route(
    routing_row: tuple[Any, ...] | None,
) -> None:
    item, normalized = _normalized_item("event-invalid-route")
    connection = FakeConnection([routing_row])

    with pytest.raises(ValueError, match="connector routing"):
        PostgresLocalPilotStorage(connection).persist_normalized_batch(
            SCOPE,
            KEY,
            _processing_job(),
            (item,),
            (normalized,),
        )

    assert not any(
        "INSERT INTO observer.observation_events" in sql for sql, _params in connection.executed
    )


def test_persist_normalized_batch_rejects_inline_content_and_wrong_scope_before_writes() -> None:
    item, normalized = _normalized_item("event-001")
    inline = NormalizedObservationInput(
        channel=normalized.channel,
        participants=normalized.participants,
        evidence=(
            EvidenceArtifact(
                media_type="text/plain",
                locator="delivery",
                role="source",
                content=b"private body",
            ),
        ),
        consent_basis=normalized.consent_basis,
        data_classification=normalized.data_classification,
        retention_class=normalized.retention_class,
        original_language=normalized.original_language,
        correlation_id=normalized.correlation_id,
    )
    connection = FakeConnection([])
    repository = PostgresLocalPilotStorage(connection)

    with pytest.raises(ValueError, match="evidence references"):
        repository.persist_normalized_batch(
            SCOPE,
            KEY,
            _processing_job(),
            (item,),
            (inline,),
        )
    assert connection.transactions == 0
    assert b"private body".decode() not in repr((item, inline.evidence[0].reference))

    wrong_job = _processing_job()
    object.__setattr__(wrong_job, "site_id", "other.example")
    with pytest.raises(ValueError, match="job scope"):
        repository.persist_normalized_batch(
            SCOPE,
            KEY,
            wrong_job,
            (item,),
            (normalized,),
        )
    assert connection.transactions == 0


def test_local_pilot_storage_exposes_provider_neutral_repository_contract() -> None:
    module = importlib.import_module("observer.local_pilot_storage")

    assert module.LocalPilotStorage
    assert module.PostgresLocalPilotStorage


def test_register_get_and_list_connector_instances_use_per_call_site_transactions() -> None:
    connection = FakeConnection(
        [
            _connector_row(
                team_ref="team:sales",
                agent_task_type="product_sample",
                account_user_ref="owner@example.invalid",
            ),
            _connector_row(
                team_ref="team:sales",
                agent_task_type="product_sample",
                account_user_ref="owner@example.invalid",
            ),
            [
                _connector_row(
                    team_ref="team:sales",
                    agent_task_type="product_sample",
                    account_user_ref="owner@example.invalid",
                ),
                _connector_row(connector="email", instance_id="support"),
            ],
        ]
    )
    repository = PostgresLocalPilotStorage(connection)

    registered = repository.register_connector_instance(
        SCOPE,
        KEY,
        now=NOW,
        replay_window_seconds=60,
        team_ref="team:sales",
        agent_task_type="product_sample",
        account_user_ref="owner@example.invalid",
    )
    loaded = repository.get_connector_instance(SCOPE, KEY)
    listed = repository.list_connector_instances(SCOPE)

    assert connection.transactions == 3
    assert [statement for statement in connection.executed if "set_config" in statement[0]] == [
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
        ("SELECT set_config('app.site_id', %s, true)", (SCOPE.site_id,)),
    ]
    assert registered == loaded
    assert [item.connector for item in listed] == ["wecom", "email"]
    assert registered.team_ref == "team:sales"
    assert registered.agent_task_type == "product_sample"
    assert registered.control_revision == 0
    assert registered.account_user_ref == "owner@example.invalid"
    assert "team:sales" not in repr(registered)
    assert "product_sample" not in repr(registered)
    assert "owner@example.invalid" not in repr(registered)
    assert not hasattr(registered, "config")
    assert not hasattr(registered, "secret")


def test_register_connector_routing_replay_is_idempotent_and_different_metadata_conflicts() -> None:
    team_ref = "team:trusted-sales"
    owner = "owner@example.invalid"
    same = _connector_row(
        team_ref=team_ref,
        agent_task_type="sales",
        account_user_ref=owner,
    )
    different = _connector_row(team_ref="team:other", agent_task_type="sales")
    connection = FakeConnection([same, None, same, None, different])
    repository = PostgresLocalPilotStorage(connection)

    first = repository.register_connector_instance(
        SCOPE,
        KEY,
        now=NOW,
        team_ref=team_ref,
        agent_task_type="sales",
        account_user_ref=owner,
    )
    replay = repository.register_connector_instance(
        SCOPE,
        KEY,
        now=NOW + timedelta(seconds=1),
        team_ref=team_ref,
        agent_task_type="sales",
        account_user_ref=owner,
    )

    assert first == replay
    assert replay.updated_at == NOW
    register_statements = [
        sql
        for sql, _params in connection.executed
        if "INSERT INTO observer.connector_instances" in sql
    ]
    assert len(register_statements) == 2
    assert all("DO NOTHING" in sql for sql in register_statements)
    with pytest.raises(ValueError, match="routing metadata conflict"):
        repository.register_connector_instance(
            SCOPE,
            KEY,
            now=NOW + timedelta(seconds=2),
            team_ref=team_ref,
            agent_task_type="purchase",
            account_user_ref=owner,
        )
    assert team_ref not in repr(first)


@pytest.mark.parametrize(
    ("team_ref", "agent_task_type", "account_user_ref"),
    [
        ("", None, None),
        ("team\nsales", None, None),
        ("team\rsales", None, None),
        ("team\x00sales", None, None),
        ("t" * 257, None, None),
        ("team:sales", "unknown", None),
        (None, "sales", None),
        ("team:sales", None, ""),
        ("team:sales", None, " owner@example.invalid"),
        ("team:sales", None, "owner\n@example.invalid"),
        ("team:sales", None, "owner\x7f@example.invalid"),
        ("team:sales", None, "u" * 257),
    ],
)
def test_connector_routing_rejects_invalid_metadata_before_opening_a_transaction(
    team_ref: str | None,
    agent_task_type: str | None,
    account_user_ref: str | None,
) -> None:
    connection = FakeConnection([])

    with pytest.raises(ValueError, match="routing"):
        PostgresLocalPilotStorage(connection).register_connector_instance(
            SCOPE,
            KEY,
            now=NOW,
            team_ref=team_ref,
            agent_task_type=agent_task_type,
            account_user_ref=account_user_ref,
        )

    assert connection.transactions == 0


def test_update_connector_routing_uses_site_scoped_control_revision_cas() -> None:
    connection = FakeConnection(
        [
            _connector_row(
                control_revision=4,
                team_ref="team:ceo-visible",
                agent_task_type=None,
                account_user_ref="ceo@example.invalid",
            ),
            None,
        ]
    )
    repository = PostgresLocalPilotStorage(connection)

    updated = repository.update_connector_routing(
        SCOPE,
        KEY,
        expected_control_revision=3,
        team_ref="team:ceo-visible",
        agent_task_type=None,
        account_user_ref="ceo@example.invalid",
        now=NOW,
    )
    assert updated.control_revision == 4
    assert updated.team_ref == "team:ceo-visible"
    assert updated.account_user_ref == "ceo@example.invalid"
    update_sql, update_params = connection.executed[1]
    assert "control_revision = control_revision + 1" in update_sql
    assert "control_revision = %s" in update_sql
    assert update_params is not None
    assert SCOPE.site_id in update_params
    assert KEY.connector in update_params
    assert KEY.instance_id in update_params
    assert 3 in update_params

    with pytest.raises(ValueError, match="routing compare-and-swap"):
        repository.update_connector_routing(
            SCOPE,
            KEY,
            expected_control_revision=3,
            team_ref=None,
            agent_task_type=None,
            account_user_ref=None,
            now=NOW + timedelta(seconds=1),
        )


def test_identity_migration_protects_connector_owner_with_repeatable_constraint() -> None:
    sql = IDENTITY_MIGRATION.read_text()

    assert "connector_instances_account_user_ref_safe_ck" in sql
    assert "account_user_ref !~ '[[:cntrl:]]'" in sql
    assert "VALIDATE CONSTRAINT connector_instances_account_user_ref_safe_ck" in sql


def test_accept_delivery_is_idempotent_for_same_digest_and_rejects_changed_body() -> None:
    connection = FakeConnection(
        [
            None,
            _delivery_row(),
            None,
            _delivery_row(digest="c" * 64),
        ]
    )
    repository = PostgresLocalPilotStorage(connection)

    replay = repository.accept_inbound_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        exact_body_sha256="a" * 64,
        object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
        byte_size=17,
        media_type="application/json",
        received_at=NOW,
        correlation_id="corr-001",
    )
    assert replay.delivery_id == "delivery-001"
    assert not hasattr(replay, "body")
    with pytest.raises(DeliveryConflict, match="different body"):
        repository.accept_inbound_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
        )

    with pytest.raises(TypeError, match="exact_bytes"):
        repository.accept_inbound_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-002",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=9,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-002",
            exact_bytes=b"forbidden",
        )
    flattened_params = repr([params for _sql, params in connection.executed])
    assert "forbidden" not in flattened_params


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("object_ref", "obs:v1:other:sha256:" + "a" * 64),
        ("byte_size", 18),
        ("media_type", "application/octet-stream"),
    ],
)
def test_accept_delivery_rejects_changed_content_metadata(
    changed: str,
    value: str | int,
) -> None:
    existing: dict[str, str | int] = {
        "object_ref": "obs:v1:site-partition:sha256:" + "a" * 64,
        "byte_size": 17,
        "media_type": "application/json",
    }
    connection = FakeConnection([None, _delivery_row()])
    arguments: dict[str, str | int] = dict(existing)
    arguments[changed] = value

    with pytest.raises(DeliveryConflict, match="content metadata"):
        PostgresLocalPilotStorage(connection).accept_inbound_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref=str(arguments["object_ref"]),
            byte_size=int(arguments["byte_size"]),
            media_type=str(arguments["media_type"]),
            received_at=NOW,
            correlation_id="corr-001",
        )


def test_accept_and_enqueue_delivery_is_one_transaction_and_idempotent() -> None:
    connection = FakeConnection([_delivery_row(), _job_row()])

    delivery, job = PostgresLocalPilotStorage(connection).accept_and_enqueue_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        exact_body_sha256="a" * 64,
        object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
        byte_size=17,
        media_type="application/json",
        received_at=NOW,
        correlation_id="corr-001",
        job_id="job-001",
        idempotency_key="delivery:wecom:sales-primary:delivery-001:g0",
        max_attempts=3,
    )

    assert connection.transactions == 1
    assert delivery.object_ref.endswith("a" * 64)
    assert delivery.byte_size == 17
    assert job.status == "queued"
    assert all(
        params is not None for sql, params in connection.executed if "INSERT INTO observer." in sql
    )


def test_authenticated_ingress_consumes_nonce_and_enqueues_in_one_transaction() -> None:
    nonce = "nonce-secret-sentinel"
    expires_at = NOW + timedelta(minutes=1)
    idempotency_key = (
        "authenticated:"
        + hashlib.sha256(
            f"{SCOPE.site_id}\x1f{KEY.connector}\x1f{KEY.instance_id}\x1f{nonce}".encode()
        ).hexdigest()
    )
    connection = FakeConnection(
        [
            (60,),
            (NOW, expires_at),
            _delivery_row(),
            _job_row(idempotency_key=idempotency_key),
        ]
    )

    result = PostgresLocalPilotStorage(connection).accept_authenticated_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        exact_body_sha256="a" * 64,
        object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
        byte_size=17,
        media_type="application/json",
        received_at=NOW,
        correlation_id="corr-001",
        nonce=nonce,
        nonce_expires_at=expires_at,
        now=NOW,
        job_id="job-001",
        max_attempts=3,
    )

    assert result == AuthenticatedIngressMetadata(
        disposition="accepted",
    )
    assert connection.transactions == 1
    assert nonce not in repr(connection.executed)
    assert any("persistent_nonces" in sql for sql, _params in connection.executed)
    assert all(
        params is not None for sql, params in connection.executed if "INSERT INTO observer." in sql
    )


def test_authenticated_ingress_duplicate_ack_does_not_enqueue_another_job() -> None:
    nonce = "nonce-001"
    expires_at = NOW + timedelta(minutes=1)
    idempotency_key = (
        "authenticated:"
        + hashlib.sha256(
            f"{SCOPE.site_id}\x1f{KEY.connector}\x1f{KEY.instance_id}\x1f{nonce}".encode()
        ).hexdigest()
    )
    connection = FakeConnection(
        [
            (60,),
            None,
            (NOW, expires_at),
            _job_row(idempotency_key=idempotency_key),
            None,
            _delivery_row(),
        ]
    )

    duplicate = PostgresLocalPilotStorage(connection).accept_authenticated_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        exact_body_sha256="a" * 64,
        object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
        byte_size=17,
        media_type="application/json",
        received_at=NOW,
        correlation_id="corr-001",
        nonce=nonce,
        nonce_expires_at=expires_at,
        now=NOW,
        job_id="job-001",
        max_attempts=3,
    )

    assert duplicate.disposition == "duplicate"
    job_inserts = [
        sql for sql, _params in connection.executed if "INSERT INTO observer.processing_jobs" in sql
    ]
    assert job_inserts == []


def test_authenticated_ingress_rejects_body_conflict_and_nonce_replay() -> None:
    expires_at = NOW + timedelta(minutes=1)
    nonce = "nonce-001"
    idempotency_key = (
        "authenticated:"
        + hashlib.sha256(
            f"{SCOPE.site_id}\x1f{KEY.connector}\x1f{KEY.instance_id}\x1f{nonce}".encode()
        ).hexdigest()
    )
    body_conflict = FakeConnection(
        [
            (60,),
            None,
            (NOW, expires_at),
            _job_row(idempotency_key=idempotency_key),
            None,
            _delivery_row(digest="c" * 64),
        ]
    )
    with pytest.raises(DeliveryConflict, match="content metadata"):
        PostgresLocalPilotStorage(body_conflict).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
            nonce=nonce,
            nonce_expires_at=expires_at,
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert not any(
        "INSERT INTO observer.processing_jobs" in sql for sql, _params in body_conflict.executed
    )

    replay = FakeConnection(
        [
            (60,),
            None,
            (NOW, expires_at),
            _job_row(
                delivery_id="delivery-original",
                idempotency_key=idempotency_key,
            ),
        ]
    )
    with pytest.raises(NonceReplay, match="different delivery"):
        PostgresLocalPilotStorage(replay).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-002",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-002",
            nonce=nonce,
            nonce_expires_at=expires_at,
            now=NOW,
            job_id="job-002",
            max_attempts=3,
        )


def test_authenticated_ingress_rejects_expired_nonce_and_old_delivery_before_writes() -> None:
    repository = PostgresLocalPilotStorage(FakeConnection([]))
    with pytest.raises(IngressExpired, match="nonce"):
        repository.accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
            nonce="nonce-001",
            nonce_expires_at=NOW,
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert repository._connection.executed == []

    old_connection = FakeConnection([(30,)])
    with pytest.raises(IngressExpired, match="replay window"):
        PostgresLocalPilotStorage(old_connection).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW - timedelta(seconds=31),
            correlation_id="corr-001",
            nonce="nonce-001",
            nonce_expires_at=NOW + timedelta(minutes=1),
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert not any("INSERT INTO" in sql for sql, _params in old_connection.executed)

    expired_replay = FakeConnection(
        [
            (60,),
            None,
            (NOW - timedelta(minutes=2), NOW - timedelta(seconds=1)),
        ]
    )
    with pytest.raises(IngressExpired, match="persisted nonce"):
        PostgresLocalPilotStorage(expired_replay).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
            nonce="nonce-001",
            nonce_expires_at=NOW + timedelta(minutes=1),
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert not any("inbound_deliveries" in sql for sql, _params in expired_replay.executed)


def test_authenticated_ingress_job_failure_is_in_same_transaction() -> None:
    expires_at = NOW + timedelta(minutes=1)
    connection = FakeConnection(
        [
            (60,),
            (NOW, expires_at),
            _delivery_row(),
            None,
            None,
        ]
    )

    with pytest.raises(JobConflict):
        PostgresLocalPilotStorage(connection).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
            nonce="nonce-001",
            nonce_expires_at=expires_at,
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert connection.transactions == 1


def test_authenticated_ingress_rejects_nonce_without_bound_job() -> None:
    expires_at = NOW + timedelta(minutes=1)
    connection = FakeConnection(
        [
            (60,),
            None,
            (NOW, expires_at),
            None,
        ]
    )

    with pytest.raises(NonceReplay, match="matching delivery job"):
        PostgresLocalPilotStorage(connection).accept_authenticated_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            exact_body_sha256="a" * 64,
            object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
            byte_size=17,
            media_type="application/json",
            received_at=NOW,
            correlation_id="corr-001",
            nonce="nonce-consumed-by-legacy-path",
            nonce_expires_at=expires_at,
            now=NOW,
            job_id="job-001",
            max_attempts=3,
        )
    assert not any(
        "inbound_deliveries" in sql or "INSERT INTO observer.processing_jobs" in sql
        for sql, _params in connection.executed
    )


def test_claim_job_uses_skip_locked_recovers_expired_lease_and_fences_old_attempt() -> None:
    claimed_connection = FakeConnection(
        [
            _job_row(
                status="processing",
                attempt_count=2,
                lease_owner="worker-b",
                lease_expires_at=NOW + timedelta(seconds=30),
                lease_generation=2,
            )
        ]
    )
    claimed = PostgresLocalPilotStorage(claimed_connection).claim_processing_job(
        SCOPE,
        worker_id="worker-b",
        now=NOW,
        lease_seconds=30,
    )

    assert claimed is not None
    assert claimed.attempt_count == 2
    assert claimed.lease_generation == 2
    claim_sql = claimed_connection.executed[-1][0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "lease_expires_at <= %s" in claim_sql
    assert "instance.status <> 'paused'" in claim_sql

    stale = FakeConnection([None])
    with pytest.raises(JobConflict, match="lease"):
        PostgresLocalPilotStorage(stale).complete_processing_job(
            SCOPE,
            job_id="job-001",
            worker_id="worker-a",
            expected_attempt=1,
            expected_lease_generation=1,
            now=NOW,
            provider_event_ids=("provider-001",),
        )
    complete_sql = stale.executed[-1][0]
    assert "attempt_count = %s" in complete_sql
    assert "lease_generation = %s" in complete_sql
    assert "lease_expires_at > %s" in complete_sql


def test_job_retry_reaches_dead_letter_and_quarantine_is_terminal() -> None:
    retry = FakeConnection(
        [_job_row(status="retry_wait", attempt_count=1, error_code="temporary_failure")]
    )
    retried = PostgresLocalPilotStorage(retry).retry_processing_job(
        SCOPE,
        job_id="job-001",
        worker_id="worker-a",
        expected_attempt=1,
        expected_lease_generation=1,
        now=NOW,
        next_retry_at=NOW + timedelta(minutes=1),
        error_code="temporary_failure",
    )
    assert retried.status == "retry_wait"

    dead = FakeConnection(
        [_job_row(status="dead_letter", attempt_count=3, error_code="retry_exhausted")]
    )
    dead_lettered = PostgresLocalPilotStorage(dead).retry_processing_job(
        SCOPE,
        job_id="job-001",
        worker_id="worker-a",
        expected_attempt=3,
        expected_lease_generation=3,
        now=NOW,
        next_retry_at=NOW + timedelta(minutes=1),
        error_code="retry_exhausted",
    )
    assert dead_lettered.status == "dead_letter"
    assert any("local_pilot_dead_letter" in sql for sql, _params in dead.executed)

    quarantined = FakeConnection(
        [_job_row(status="quarantined", attempt_count=1, error_code="invalid_envelope")]
    )
    result = PostgresLocalPilotStorage(quarantined).quarantine_processing_job(
        SCOPE,
        job_id="job-001",
        worker_id="worker-a",
        expected_attempt=1,
        expected_lease_generation=1,
        now=NOW,
        reason_code="invalid_envelope",
    )
    assert result.status == "quarantined"
    assert any("local_pilot_quarantine" in sql for sql, _params in quarantined.executed)


def test_connector_pause_resume_checkpoint_health_and_replay_are_safe() -> None:
    paused_connection = FakeConnection([_connector_row(status="paused")])
    paused = PostgresLocalPilotStorage(paused_connection).set_connector_status(
        SCOPE,
        KEY,
        status="paused",
        now=NOW,
    )
    assert paused.status == "paused"

    health_connection = FakeConnection([_checkpoint_row()])
    checkpoint = PostgresLocalPilotStorage(health_connection).update_checkpoint_health(
        SCOPE,
        KEY,
        status="healthy",
        now=NOW,
        last_success_at=NOW,
    )
    assert checkpoint.status == "healthy"
    flattened = repr(health_connection.executed)
    assert "payload" not in flattened
    assert "provider_event_id" not in flattened

    replay_connection = FakeConnection(
        [
            (None,),
            None,
            ("delivery-001", "a" * 64, "evidence-ref-001", 17),
            (1,),
            _job_row(generation=1),
        ]
    )
    replayed = PostgresLocalPilotStorage(replay_connection).replay_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        job_id="job-replay-001",
        idempotency_key="replay:ticket-001",
        now=NOW,
        max_attempts=3,
    )
    assert replayed.generation == 1
    replay_sql = " ".join(sql for sql, _params in replay_connection.executed)
    assert "FOR UPDATE" in replay_sql
    assert "processing_status = 'queued'" in replay_sql
    assert "processing_status = 'failed'" in replay_sql
    assert "received_at >= %s" in replay_sql
    assert "received_at <= %s" in replay_sql
    assert "object_ref IS NOT NULL" in replay_sql
    assert "byte_size IS NOT NULL" in replay_sql
    assert "ON CONFLICT (site_id, idempotency_key)" in replay_sql
    assert "DO NOTHING" in replay_sql
    assert "exact_body_sha256 =" not in replay_sql
    assert "object_ref =" not in replay_sql
    assert "byte_size =" not in replay_sql

    ineligible = FakeConnection([(None,), None, None])
    with pytest.raises(DeliveryConflict, match="eligible failed"):
        PostgresLocalPilotStorage(ineligible).replay_delivery(
            SCOPE,
            KEY,
            delivery_id="delivery-001",
            job_id="job-replay-old",
            idempotency_key="replay:ticket-old",
            now=NOW,
            max_attempts=3,
        )

    existing = FakeConnection([(None,), _job_row(generation=1)])
    same = PostgresLocalPilotStorage(existing).replay_delivery(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        job_id="ignored",
        idempotency_key="replay:ticket-001",
        now=NOW,
        max_attempts=3,
    )
    assert same.generation == 1
    assert not any(
        "UPDATE observer.inbound_deliveries" in sql for sql, _params in existing.executed
    )


def test_link_delivery_events_is_instance_scoped_and_supports_one_batch_many_events() -> None:
    connection = FakeConnection([])
    repository = PostgresLocalPilotStorage(connection)

    repository.link_delivery_events(
        SCOPE,
        KEY,
        delivery_id="delivery-001",
        provider_event_ids=("provider-001", "provider-002"),
        linked_at=NOW,
    )

    insert = next(item for item in connection.executed if "inbound_delivery_events" in item[0])
    assert "UNNEST(%s::text[])" in insert[0]
    assert insert[1] == (
        SCOPE.site_id,
        KEY.connector,
        KEY.instance_id,
        "delivery-001",
        NOW,
        ("provider-001", "provider-002"),
    )


def test_checkpoint_cas_rejects_stale_versions_and_never_uses_occurred_at_as_cursor() -> None:
    success = FakeConnection([_checkpoint_row(version=1, cursor_value="opaque:next")])
    checkpoint = PostgresLocalPilotStorage(success).compare_and_swap_checkpoint(
        SCOPE,
        KEY,
        expected_version=0,
        cursor="opaque:next",
        next_version=1,
        now=NOW,
    )
    assert checkpoint.checkpoint_version == 1
    assert checkpoint.cursor == "opaque:next"
    update_sql = success.executed[-1][0].lower()
    assert "checkpoint_version = %s" in update_sql
    assert "checkpoint_version = %s" in update_sql
    assert "occurred_at" not in update_sql

    stale = FakeConnection([None])
    with pytest.raises(CheckpointConflict, match="stale"):
        PostgresLocalPilotStorage(stale).compare_and_swap_checkpoint(
            SCOPE,
            KEY,
            expected_version=0,
            cursor="opaque:next",
            next_version=1,
            now=NOW,
        )


def test_connector_lease_enforces_owner_expiry_renewal_and_release() -> None:
    lease_expires = NOW + timedelta(seconds=30)
    acquire = FakeConnection(
        [_checkpoint_row(lease_owner="worker-a", lease_expires_at=lease_expires)]
    )
    repository = PostgresLocalPilotStorage(acquire)
    checkpoint = repository.acquire_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
        lease_seconds=30,
    )
    assert checkpoint.lease_owner == "worker-a"
    assert "lease_expires_at <= %s" in acquire.executed[-1][0]

    renew = FakeConnection(
        [
            _checkpoint_row(
                lease_owner="worker-a",
                lease_expires_at=lease_expires + timedelta(seconds=30),
            )
        ]
    )
    PostgresLocalPilotStorage(renew).renew_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
        lease_seconds=60,
    )
    assert "lease_owner = %s" in renew.executed[-1][0]
    assert "lease_expires_at > %s" in renew.executed[-1][0]

    release = FakeConnection([_checkpoint_row()])
    PostgresLocalPilotStorage(release).release_connector_lease(
        SCOPE,
        KEY,
        owner="worker-a",
        now=NOW,
    )
    assert "lease_owner = NULL" in release.executed[-1][0]

    conflict = FakeConnection([None])
    with pytest.raises(LeaseConflict, match="lease"):
        PostgresLocalPilotStorage(conflict).acquire_connector_lease(
            SCOPE,
            KEY,
            owner="worker-b",
            now=NOW,
            lease_seconds=30,
        )


def test_nonce_is_hashed_consumed_once_and_requires_future_expiry() -> None:
    accepted = FakeConnection([(NOW, NOW + timedelta(minutes=5))])
    receipt = PostgresLocalPilotStorage(accepted).consume_nonce(
        SCOPE,
        identity_ref="connector:wecom:sales-primary",
        nonce="nonce-001",
        now=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert receipt.nonce_sha256 != "nonce-001"
    assert "nonce-001" not in repr(accepted.executed)

    replay = FakeConnection([None])
    with pytest.raises(NonceReplay, match="replay"):
        PostgresLocalPilotStorage(replay).consume_nonce(
            SCOPE,
            identity_ref="connector:wecom:sales-primary",
            nonce="nonce-001",
            now=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    with pytest.raises(ValueError, match="future"):
        PostgresLocalPilotStorage(FakeConnection([])).consume_nonce(
            SCOPE,
            identity_ref="connector:wecom:sales-primary",
            nonce="nonce-002",
            now=NOW,
            expires_at=NOW,
        )


def test_context_outbox_idempotency_claim_retry_dead_letter_and_replay_rejection() -> None:
    enqueue = FakeConnection([None, _outbox_row()])
    repository = PostgresLocalPilotStorage(enqueue)
    queued = repository.enqueue_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        observation_event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
        idempotency_key="context:event-001",
        payload_digest="b" * 64,
        now=NOW,
        max_attempts=3,
    )
    assert queued.status == "queued"

    conflict = FakeConnection([None, _outbox_row()])
    with pytest.raises(OutboxConflict, match="different payload"):
        PostgresLocalPilotStorage(conflict).enqueue_context_outbox(
            SCOPE,
            outbox_id="outbox-001",
            observation_event_id="01K20B8BV5C6P4YFAT8YQ3D4S5",
            idempotency_key="context:event-001",
            payload_digest="c" * 64,
            now=NOW,
            max_attempts=3,
        )

    claimed_connection = FakeConnection(
        [
            _outbox_row(
                status="leased",
                attempt_count=1,
                lease_owner="publisher-a",
                lease_expires_at=NOW + timedelta(seconds=30),
            )
        ]
    )
    claimed = PostgresLocalPilotStorage(claimed_connection).claim_context_outbox(
        SCOPE,
        worker_id="publisher-a",
        now=NOW,
        lease_seconds=30,
    )
    assert claimed is not None
    assert claimed.attempt_count == 1
    claim_sql = claimed_connection.executed[-1][0]
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "attempt_count < max_attempts" in claim_sql

    retry_connection = FakeConnection(
        [_outbox_row(status="retry_wait", attempt_count=1, error_code="context_unavailable")]
    )
    retry = PostgresLocalPilotStorage(retry_connection).mark_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        worker_id="publisher-a",
        now=NOW,
        published=False,
        error_code="context_unavailable",
        next_retry_at=NOW + timedelta(minutes=1),
    )
    assert retry.status == "retry_wait"
    assert "attempt_count >= max_attempts" in retry_connection.executed[-1][0]

    dead_connection = FakeConnection(
        [_outbox_row(status="dead_letter", attempt_count=3, error_code="context_rejected")]
    )
    dead = PostgresLocalPilotStorage(dead_connection).mark_context_outbox(
        SCOPE,
        outbox_id="outbox-001",
        worker_id="publisher-a",
        now=NOW,
        published=False,
        error_code="context_rejected",
        next_retry_at=NOW + timedelta(minutes=1),
    )
    assert dead.status == "dead_letter"

    replay = FakeConnection([None])
    with pytest.raises(OutboxConflict, match="lease"):
        PostgresLocalPilotStorage(replay).mark_context_outbox(
            SCOPE,
            outbox_id="outbox-001",
            worker_id="publisher-a",
            now=NOW,
            published=True,
        )


def test_health_query_returns_sanitized_site_scoped_status_only() -> None:
    connection = FakeConnection(
        [
            (
                SCOPE.site_id,
                KEY.connector,
                KEY.instance_id,
                "degraded",
                3,
                "worker-a",
                NOW + timedelta(seconds=30),
                NOW,
                "provider_timeout",
                2,
                1,
            )
        ]
    )
    health = PostgresLocalPilotStorage(connection).get_connector_health(SCOPE, KEY)

    assert health is not None
    assert health.last_error_code == "provider_timeout"
    assert health.pending_jobs == 2
    assert health.pending_outbox == 1
    assert not hasattr(health, "payload")
    assert not hasattr(health, "body")
    assert connection.executed[0] == (
        "SELECT set_config('app.site_id', %s, true)",
        (SCOPE.site_id,),
    )
    health_sql = connection.executed[1][0]
    assert (
        "JOIN observer.observation_events AS event "
        "ON event.site_id = outbox.site_id "
        "AND event.event_id = outbox.observation_event_id"
    ) in health_sql
    assert "event.connector = instance.connector" in health_sql
    assert "event.connector_instance_id = instance.connector_instance_id" in health_sql


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda repository: repository.acquire_connector_lease(
                SCOPE,
                KEY,
                owner="worker-a",
                now=NOW,
                lease_seconds=0,
            ),
            "positive",
        ),
        (
            lambda repository: repository.compare_and_swap_checkpoint(
                SCOPE,
                KEY,
                expected_version=2,
                cursor="opaque",
                next_version=2,
                now=NOW,
            ),
            "next_version",
        ),
        (
            lambda repository: repository.accept_inbound_delivery(
                SCOPE,
                KEY,
                delivery_id="delivery-001",
                exact_body_sha256="A" * 64,
                object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
                byte_size=17,
                media_type="application/json",
                received_at=NOW,
                correlation_id="corr-001",
            ),
            "sha256",
        ),
        (
            lambda repository: repository.accept_inbound_delivery(
                SCOPE,
                KEY,
                delivery_id="delivery-001",
                exact_body_sha256="a" * 64,
                object_ref="obs:v1:site-partition:sha256:" + "a" * 64,
                byte_size=17,
                media_type="application/json",
                received_at=NOW.replace(tzinfo=None),
                correlation_id="corr-001",
            ),
            "timezone-aware",
        ),
    ],
)
def test_repository_validates_bounds_and_metadata(
    call: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        call(PostgresLocalPilotStorage(FakeConnection([])))


def _table_columns(sql: str, table: str) -> str:
    start = sql.index(f"create table if not exists observer.{table}")
    end = sql.index(");", start)
    return sql[start:end]
