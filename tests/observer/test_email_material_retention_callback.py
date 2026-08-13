from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.email_material_retention import (
    EmailMaterialDeletionLease,
    EmailMaterialRetentionDeletionRunner,
    EmailMaterialRetentionRequest,
    EmailMaterialRetentionService,
    EmailMaterialTombstoneReceipt,
)
from observer.email_material_retention_callback import (
    EmailMaterialRetentionCallback,
    EmailMaterialRetentionCallbackLease,
    PostgresEmailMaterialRetentionCallbackRepository,
)
from observer.models import TenantScope

NOW = datetime(2026, 9, 13, 8, tzinfo=UTC)
MIGRATION = (
    Path(__file__).parents[2]
    / "services"
    / "observer"
    / "migrations"
    / "021_email_material_retention_callback.sql"
)


def test_migration_atomically_enqueues_callback_when_tombstone_is_minted() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in (
        "email_material_retention_callbacks",
        "email_material_retention_callback_work",
    ):
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql
        assert f"revoke all on observer.{table} from public" in sql
    completion_start = sql.index(
        "create or replace function observer.complete_email_material_retention"
    )
    completion = sql[completion_start:]
    assert "insert into observer.email_material_tombstone_receipts" in completion
    assert "insert into observer.email_material_retention_callbacks" in completion
    assert "insert into observer.email_material_retention_callback_work" in completion
    assert completion.index("email_material_tombstone_receipts") < completion.index(
        "email_material_retention_callbacks"
    )


def test_callback_outbox_is_fenced_bounded_and_contains_no_cas_locator() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    callback_schema = sql[
        : sql.index("create or replace function observer.complete_email_material_retention")
    ]

    assert "claim_email_material_retention_callback" in sql
    assert "heartbeat_email_material_retention_callback" in sql
    assert "ack_email_material_retention_callback" in sql
    assert "fail_email_material_retention_callback" in sql
    assert "attempt between 0 and 5" in sql
    assert "lease_generation" in sql
    assert "for update skip locked" in sql
    assert "object_ref" not in callback_schema
    assert "raw_content" not in callback_schema


def test_callback_digest_uses_postgresql_core_sha256_without_pgcrypto() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert sql.count("pg_catalog.sha256(pg_catalog.convert_to(") == 1
    assert "public.digest" not in sql
    assert "create extension" not in sql


def test_callback_wire_is_closed_and_redacted() -> None:
    callback = EmailMaterialRetentionCallback(
        callback_ref="EMC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id="alpha.example",
        purpose="email_draft_material",
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        observer_request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tombstone_receipt_ref="TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        deleted_at=NOW,
        evidence_digest="sha256:" + "1" * 64,
        callback_payload_digest="sha256:" + "2" * 64,
    )

    assert set(callback.to_wire()) == {
        "schema_version",
        "site_id",
        "purpose",
        "authority_receipt_ref",
        "evidence_ref",
        "observer_request_ref",
        "tombstone_receipt_ref",
        "deleted_at",
        "evidence_digest",
        "callback_payload_digest",
    }
    assert callback.evidence_ref not in repr(callback)


class _RetentionService:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.failures = 1

    def run_once(self, scope: object, *, batch_size: int) -> tuple[str, ...]:
        self.batch_sizes.append(batch_size)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("injected completion failure")
        return ("durable-tombstone",)


def test_bounded_deletion_runner_recovers_after_injected_completion_failure() -> None:
    service = _RetentionService()
    runner = EmailMaterialRetentionDeletionRunner(service=service, max_batch_size=5)

    with pytest.raises(RuntimeError, match="injected completion failure"):
        runner.run_once(object(), batch_size=5)
    assert runner.run_once(object(), batch_size=5) == ("durable-tombstone",)
    assert service.batch_sizes == [5, 5]
    with pytest.raises(ValueError, match="batch size"):
        runner.run_once(object(), batch_size=6)


class _RecoveringDeletionRepository:
    def __init__(self, request: EmailMaterialRetentionRequest) -> None:
        self.request = request
        self.claim_count = 0
        self.completion_failures = 1
        self.completed: list[EmailMaterialTombstoneReceipt] = []

    def claim_due(
        self,
        scope: TenantScope,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[EmailMaterialDeletionLease, ...]:
        assert scope.site_id == self.request.site_id
        assert worker_id == "observer-retention-1"
        assert lease_until == now + timedelta(minutes=5)
        assert limit == 1
        self.claim_count += 1
        return (
            EmailMaterialDeletionLease(
                **self.request.as_dict(),
                worker_id=worker_id,
                lease_generation=self.claim_count,
                lease_expires_at=lease_until,
            ),
        )

    def complete_deletion(
        self,
        scope: TenantScope,
        lease: EmailMaterialDeletionLease,
        *,
        receipt_ref: str,
        deleted_at: datetime,
    ) -> EmailMaterialTombstoneReceipt:
        assert scope.site_id == self.request.site_id
        if self.completion_failures:
            self.completion_failures -= 1
            raise RuntimeError("injected atomic callback failure")
        receipt = EmailMaterialTombstoneReceipt.from_lease(
            lease,
            receipt_ref=receipt_ref,
            deleted_at=deleted_at,
        )
        self.completed.append(receipt)
        return receipt


class _IdempotentFakeCas:
    def __init__(self) -> None:
        self.delete_calls: list[str] = []
        self.deleted: set[str] = set()

    def delete(self, scope: TenantScope, object_ref: str) -> None:
        assert scope.site_id == "alpha.example"
        self.delete_calls.append(object_ref)
        self.deleted.add(object_ref)


def test_cas_delete_then_atomic_callback_failure_recovers_on_new_lease() -> None:
    terminal_at = NOW - timedelta(days=31)
    digest = "sha256:" + "a" * 64
    object_ref = "obs:v1:" + "b" * 32 + ":" + digest
    request = EmailMaterialRetentionRequest(
        request_ref="EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        site_id="alpha.example",
        purpose="email_draft_material",
        evidence_ref="EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        material_kind="draft",
        draft_ref="DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        draft_revision=4,
        object_ref=object_ref,
        digest=digest,
        terminal_state="sent",
        terminal_at=terminal_at,
        not_before=terminal_at + timedelta(days=30),
        authority_receipt_ref="ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    repository = _RecoveringDeletionRepository(request)
    cas = _IdempotentFakeCas()
    clock_values = iter((NOW, NOW + timedelta(minutes=6)))
    service = EmailMaterialRetentionService(
        repository=repository,
        cas=cas,
        authoritative_registrar=None,
        worker_id="observer-retention-1",
        clock=lambda: next(clock_values),
    )
    runner = EmailMaterialRetentionDeletionRunner(service=service, max_batch_size=1)
    scope = TenantScope("alpha.example", "observation_processing")

    with pytest.raises(RuntimeError, match="atomic callback failure"):
        runner.run_once(scope, batch_size=1)
    receipts = runner.run_once(scope, batch_size=1)

    assert cas.delete_calls == [object_ref, object_ref]
    assert cas.deleted == {object_ref}
    assert receipts == tuple(repository.completed)
    assert len(repository.completed) == 1
    assert repository.claim_count == 2


class _RoleCursor:
    def __init__(self, current_user: str) -> None:
        self.current_user = current_user
        self.current: list[tuple[object, ...]] = []
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: object = ()) -> None:
        self.calls.append(query)
        self.current = [(self.current_user,)] if "SELECT current_user" in query else []

    def fetchone(self):
        return self.current.pop(0) if self.current else None


class _RoleConnection:
    def __init__(self, current_user: str) -> None:
        self.query_cursor = _RoleCursor(current_user)

    def transaction(self):
        return self

    def cursor(self):
        return self.query_cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_observer_callback_repository_rejects_spoofed_database_session_role() -> None:
    connection = _RoleConnection("postgres")
    repository = PostgresEmailMaterialRetentionCallbackRepository(connection)

    with pytest.raises(ValueError, match="database role"):
        repository.claim(
            TenantScope("alpha.example", "observation_processing"),
            worker_id="observer-callback-1",
            now=NOW,
            lease_until=NOW + timedelta(minutes=5),
        )

    assert not any(
        "claim_email_material_retention_callback" in query
        for query in connection.query_cursor.calls
    )


class _RepositoryCursor(_RoleCursor):
    def __init__(self, responses: dict[str, list[tuple[object, ...]]]) -> None:
        super().__init__("gbos_observer_app")
        self.responses = responses

    def execute(self, query: str, _params: object = ()) -> None:
        self.calls.append(query)
        self.current = []
        for marker, rows in self.responses.items():
            if marker in query:
                self.current = list(rows)
                break

    def fetchall(self):
        rows, self.current = self.current, []
        return rows


class _RepositoryConnection(_RoleConnection):
    def __init__(self, responses: dict[str, list[tuple[object, ...]]]) -> None:
        self.query_cursor = _RepositoryCursor(
            {"SELECT current_user": [("gbos_observer_app",)], **responses}
        )


def _callback_row() -> tuple[object, ...]:
    return (
        "EMC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "alpha.example",
        "email_draft_material",
        "ETA-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "EMR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "TMB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        NOW,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
    )


def test_callback_repository_claim_heartbeat_ack_and_fail_are_generation_fenced() -> None:
    lease_row = (
        *_callback_row(),
        "observer-callback-1",
        2,
        3,
        NOW + timedelta(minutes=5),
    )
    connection = _RepositoryConnection(
        {
            "claim_email_material_retention_callback": [lease_row],
            "heartbeat_email_material_retention_callback": [(NOW + timedelta(minutes=5),)],
            "ack_email_material_retention_callback": [(True,)],
            "fail_email_material_retention_callback": [(True,)],
        }
    )
    repository = PostgresEmailMaterialRetentionCallbackRepository(connection)
    scope = TenantScope("alpha.example", "observation_processing")

    lease = repository.claim(
        scope,
        worker_id="observer-callback-1",
        now=NOW,
        lease_until=NOW + timedelta(minutes=5),
    )
    assert lease == EmailMaterialRetentionCallbackLease.from_row(lease_row)
    assert lease is not None
    assert repository.heartbeat(
        scope,
        lease,
        now=NOW,
        lease_until=NOW + timedelta(minutes=5),
    ) == NOW + timedelta(minutes=5)
    repository.ack(
        scope,
        lease,
        callback_receipt_ref="GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        now=NOW,
    )
    repository.fail(
        scope,
        lease,
        safe_code="gateway_unavailable",
        next_attempt_at=NOW + timedelta(seconds=4),
        now=NOW,
    )
    for marker in ("heartbeat_", "ack_", "fail_"):
        assert any(marker in query for query in connection.query_cursor.calls)


def test_callback_repository_stale_generation_ack_fails_closed() -> None:
    connection = _RepositoryConnection({"ack_email_material_retention_callback": [(False,)]})
    repository = PostgresEmailMaterialRetentionCallbackRepository(connection)
    scope = TenantScope("alpha.example", "observation_processing")
    lease = EmailMaterialRetentionCallbackLease.from_row(
        (
            *_callback_row(),
            "observer-callback-1",
            2,
            2,
            NOW + timedelta(minutes=5),
        )
    )

    with pytest.raises(ValueError, match="lease fence"):
        repository.ack(
            scope,
            lease,
            callback_receipt_ref="GTC-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            now=NOW,
        )
