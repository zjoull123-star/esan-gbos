from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.observer.observer.models import TenantScope
from services.observer.observer.retention import (
    CasDeletionLease,
    RetentionError,
    RetentionFence,
    RetentionPreview,
    RetentionService,
)

NOW = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
SCOPE = TenantScope("alpha.example", "audit_compliance")
OBJECT_REF = "obs:v1:0123456789abcdef0123456789abcdef:sha256:" + "a" * 64


class _Storage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.preview = RetentionPreview(
            scanned_count=4,
            eligible_count=2,
            legal_hold_count=1,
            historical_reference_count=1,
        )
        self.fence = RetentionFence(
            run_id="retention-run-1",
            worker_id="retention-worker-1",
            generation=1,
        )
        self.leases = (
            CasDeletionLease(
                object_ref=OBJECT_REF,
                sha256="a" * 64,
                lease_generation=1,
            ),
        )
        self.metadata_deleted_count = 2

    def preview_batch(
        self,
        scope: TenantScope,
        *,
        now: datetime,
        batch_size: int,
    ) -> RetentionPreview:
        self.calls.append(("preview", (scope, now, batch_size)))
        return self.preview

    def claim_run(
        self,
        scope: TenantScope,
        *,
        run_id: str,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> RetentionFence:
        self.calls.append(("claim_run", (scope, run_id, worker_id, now, lease_until)))
        return self.fence

    def expire_metadata(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        batch_size: int,
    ) -> tuple[RetentionPreview, int]:
        self.calls.append(("expire_metadata", (scope, fence, now, batch_size)))
        return self.preview, self.metadata_deleted_count

    def claim_cas_deletions(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[CasDeletionLease, ...]:
        self.calls.append(("claim_cas", (scope, fence, now, lease_until, batch_size)))
        return self.leases

    def complete_cas_deletion(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        lease: CasDeletionLease,
        *,
        now: datetime,
    ) -> None:
        self.calls.append(("complete_cas", (scope, fence, lease, now)))

    def complete_run(
        self,
        scope: TenantScope,
        fence: RetentionFence,
        *,
        now: datetime,
        metadata_deleted_count: int,
        cas_deleted_count: int,
        vault_deleted_count: int,
    ) -> None:
        self.calls.append(
            (
                "complete_run",
                (
                    scope,
                    fence,
                    now,
                    metadata_deleted_count,
                    cas_deleted_count,
                    vault_deleted_count,
                ),
            )
        )


class _Cas:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.deleted: list[tuple[TenantScope, str]] = []
        self.failure = failure

    def delete(self, scope: TenantScope, object_ref: str) -> None:
        self.deleted.append((scope, object_ref))
        if self.failure is not None:
            raise self.failure


class _Vault:
    def __init__(self, removed: int = 3) -> None:
        self.removed = removed
        self.calls: list[datetime] = []

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        assert now is not None
        self.calls.append(now)
        return self.removed


def _service(
    storage: _Storage,
    cas: _Cas,
    vault: _Vault,
) -> RetentionService:
    return RetentionService(
        storage=storage,
        cas=cas,
        vault=vault,
        worker_id="retention-worker-1",
        clock=lambda: NOW,
        lease_duration=timedelta(minutes=2),
        run_id_factory=lambda: "retention-run-1",
    )


def test_dry_run_reports_eligibility_without_any_mutation() -> None:
    storage = _Storage()
    cas = _Cas()
    vault = _Vault()

    result = _service(storage, cas, vault).run(SCOPE, batch_size=25, dry_run=True)

    assert result.dry_run is True
    assert result.preview == storage.preview
    assert result.metadata_deleted_count == 0
    assert result.cas_deleted_count == 0
    assert result.vault_deleted_count == 0
    assert [name for name, _ in storage.calls] == ["preview"]
    assert cas.deleted == []
    assert vault.calls == []


def test_execute_uses_fenced_db_batch_then_cas_then_vault_and_content_free_metrics() -> None:
    storage = _Storage()
    cas = _Cas()
    vault = _Vault()

    result = _service(storage, cas, vault).run(SCOPE, batch_size=25, dry_run=False)

    assert result.dry_run is False
    assert result.metadata_deleted_count == 2
    assert result.cas_deleted_count == 1
    assert result.vault_deleted_count == 3
    assert [name for name, _ in storage.calls] == [
        "claim_run",
        "expire_metadata",
        "claim_cas",
        "complete_cas",
        "complete_run",
    ]
    assert cas.deleted == [(SCOPE, OBJECT_REF)]
    assert vault.calls == [NOW]
    receipt = result.as_receipt()
    assert set(receipt) == {
        "schema_version",
        "dry_run",
        "scanned_count",
        "eligible_count",
        "legal_hold_count",
        "historical_reference_count",
        "metadata_deleted_count",
        "cas_deleted_count",
        "vault_deleted_count",
        "completed_at",
    }
    assert OBJECT_REF not in repr(receipt)
    assert "a" * 64 not in repr(receipt)


def test_cas_failure_is_redacted_and_leaves_tombstone_uncompleted_for_replay() -> None:
    storage = _Storage()
    cas = _Cas(RuntimeError(f"failed deleting {OBJECT_REF}"))
    vault = _Vault()

    with pytest.raises(RetentionError, match="retention.cas_delete_failed") as captured:
        _service(storage, cas, vault).run(SCOPE, batch_size=25, dry_run=False)

    assert OBJECT_REF not in repr(captured.value)
    assert [name for name, _ in storage.calls] == [
        "claim_run",
        "expire_metadata",
        "claim_cas",
    ]
    assert vault.calls == []


@pytest.mark.parametrize("batch_size", [True, 0, 1001])
def test_run_fails_closed_for_invalid_batch_size(batch_size: object) -> None:
    storage = _Storage()

    with pytest.raises(ValueError, match="batch_size"):
        _service(storage, _Cas(), _Vault()).run(
            SCOPE,
            batch_size=batch_size,  # type: ignore[arg-type]
            dry_run=True,
        )

    assert storage.calls == []


def test_retention_sensitive_types_have_redacted_repr() -> None:
    lease = CasDeletionLease(
        object_ref=OBJECT_REF,
        sha256="a" * 64,
        lease_generation=9,
    )
    fence = RetentionFence(
        run_id="run-secret-sentinel",
        worker_id="worker-secret-sentinel",
        generation=9,
    )

    rendered = repr((lease, fence))
    for sensitive in (OBJECT_REF, "a" * 64, "run-secret-sentinel", "worker-secret-sentinel"):
        assert sensitive not in rendered


def test_retention_migration_declares_durable_fences_and_reinsertion_guards() -> None:
    migration = (
        Path(__file__).parents[2]
        / "services"
        / "observer"
        / "migrations"
        / "011_local_pilot_retention.sql"
    ).read_text(encoding="utf-8")

    for required in (
        "CREATE TABLE IF NOT EXISTS observer.retention_runs",
        "CREATE TABLE IF NOT EXISTS observer.retention_cas_tombstones",
        "preview_retention_batch",
        "claim_retention_run",
        "expire_retention_metadata",
        "claim_retention_cas_deletions",
        "complete_retention_cas_deletion",
        "complete_retention_run",
        "FOR UPDATE SKIP LOCKED",
        "reject_tombstoned_cas_reference",
        "retention_until <= p_now",
        "hold.released_at IS NULL",
        "context.evidence_records",
    ):
        assert required in migration
