from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.control_service import (
    ConnectorControlResult,
    ConnectorStatus,
    IdempotencyConflict,
    LocalPilotControlService,
    PostgresControlRepository,
    RevisionConflict,
)
from observer.models import ConnectorKey, TenantScope

NOW = datetime(2026, 8, 8, 9, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "observation_processing")
KEY = ConnectorKey("email", "sales-inbox")
ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "services" / "observer" / "migrations" / "006_local_pilot_control.sql"


class FakeControlRepository:
    def __init__(self) -> None:
        self.status = ConnectorStatus(
            instance_id=KEY.instance_id,
            channel="email",
            status="enabled",
            checkpoint_version=4,
            backlog=2,
            last_success_at=NOW - timedelta(minutes=1),
            safe_error_code=None,
            freshness="fresh",
            revision=7,
        )
        self.commands: dict[str, tuple[str, ConnectorControlResult]] = {}
        self.replay_call: tuple[datetime, int] | None = None

    def list_status(
        self,
        scope: TenantScope,
        *,
        channel: str | None,
    ) -> tuple[ConnectorStatus, ...]:
        assert scope == SCOPE
        return (self.status,) if channel in {None, self.status.channel} else ()

    def resolve_connector(
        self,
        scope: TenantScope,
        *,
        instance_id: str,
    ) -> ConnectorKey:
        assert scope == SCOPE and instance_id == KEY.instance_id
        return KEY

    def mutate_status(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        target_status: str,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        now: datetime,
    ) -> ConnectorControlResult:
        assert scope == SCOPE and key == KEY and now == NOW
        if existing := self.commands.get(idempotency_key):
            digest, result = existing
            if digest != request_digest:
                raise IdempotencyConflict("conflict")
            return ConnectorControlResult(status=result.status, replayed_count=0, replayed=True)
        if expected_revision != self.status.revision:
            raise RevisionConflict("stale")
        self.status = ConnectorStatus(
            **{
                **self.status.as_dict(),
                "status": target_status,
                "revision": expected_revision + 1,
            }
        )
        result = ConnectorControlResult(status=self.status, replayed_count=0, replayed=False)
        self.commands[idempotency_key] = (request_digest, result)
        return result

    def replay_failed(
        self,
        scope: TenantScope,
        key: ConnectorKey,
        *,
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
        cutoff: datetime,
        limit: int,
        now: datetime,
    ) -> ConnectorControlResult:
        assert scope == SCOPE and key == KEY and expected_revision == 7
        assert idempotency_key == "replay-0001" and len(request_digest) == 64 and now == NOW
        self.replay_call = (cutoff, limit)
        return ConnectorControlResult(status=self.status, replayed_count=2, replayed=False)


def test_pause_is_cas_guarded_and_idempotently_replayed() -> None:
    repository = FakeControlRepository()
    service = LocalPilotControlService(repository=repository, clock=lambda: NOW)

    first = service.pause(
        SCOPE,
        KEY,
        expected_revision=7,
        idempotency_key="pause-0001",
    )
    second = service.pause(
        SCOPE,
        KEY,
        expected_revision=7,
        idempotency_key="pause-0001",
    )

    assert first.status.status == "paused"
    assert first.status.revision == 8
    assert first.replayed is False
    assert second.replayed is True


def test_instance_id_is_resolved_inside_site_scope_before_bff_command() -> None:
    service = LocalPilotControlService(
        repository=FakeControlRepository(),
        clock=lambda: NOW,
    )

    assert service.resolve_instance(SCOPE, instance_id=KEY.instance_id) == KEY


def test_pause_rejects_stale_revision() -> None:
    service = LocalPilotControlService(repository=FakeControlRepository(), clock=lambda: NOW)

    with pytest.raises(RevisionConflict):
        service.pause(
            SCOPE,
            KEY,
            expected_revision=6,
            idempotency_key="pause-0002",
        )


def test_replay_is_bounded_to_same_instance_30_day_candidates() -> None:
    repository = FakeControlRepository()
    service = LocalPilotControlService(repository=repository, clock=lambda: NOW)

    result = service.replay(
        SCOPE,
        KEY,
        expected_revision=7,
        idempotency_key="replay-0001",
        limit=100,
    )

    assert result.replayed_count == 2
    assert repository.replay_call == (NOW - timedelta(days=30), 100)
    with pytest.raises(ValueError, match="limit"):
        service.replay(
            SCOPE,
            KEY,
            expected_revision=7,
            idempotency_key="replay-0002",
            limit=101,
        )


def test_control_migration_persists_revision_and_idempotency_under_rls() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "add column if not exists control_revision" in sql
    assert "create table if not exists observer.connector_control_commands" in sql
    assert "request_digest char(64)" in sql
    assert "unique (site_id, idempotency_key)" in sql
    assert "enable row level security" in sql
    assert "force row level security" in sql
    assert "current_setting('app.site_id', true)" in sql
    assert "processing_status = 'failed'" in sql
    assert "received_at" in sql


def test_postgres_control_repository_is_a_real_storage_adapter() -> None:
    repository = PostgresControlRepository(connection=object(), replay_storage=object())

    assert "connection=<redacted>" in repr(repository)
    assert "replay_storage=<redacted>" in repr(repository)
