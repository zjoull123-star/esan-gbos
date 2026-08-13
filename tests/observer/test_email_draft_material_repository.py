from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.observer.observer.email_draft_material_repository import (
    EmailDraftMaterialReplayConflict,
    PostgresEmailDraftMaterialRepository,
)
from services.observer.observer.models import TenantScope

SCOPE = TenantScope("alpha.example", "observation_processing")


class _Cursor:
    def __init__(self, rows: list[object]) -> None:
        self.rows = iter(rows)
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, params: object = None) -> None:
        self.calls.append((statement, params))

    def fetchone(self) -> object:
        return next(self.rows, None)


class _Connection:
    def __init__(self, rows: list[object]) -> None:
        self.query_cursor = _Cursor(rows)

    def transaction(self) -> _Connection:
        return self

    def cursor(self) -> _Cursor:
        return self.query_cursor

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_postgres_repository_replays_only_exact_closed_receipt() -> None:
    digest = "sha256:" + "a" * 64
    response = {"evidence_ref": "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV", "digest": digest, "revision": 1}
    connection = _Connection([(digest, response)])
    repository = PostgresEmailDraftMaterialRepository(connection)

    result = repository.replay(
        SCOPE,
        purpose="email_draft_material",
        operation="save",
        idempotency_key="draft-save-01",
        request_digest=digest,
    )

    assert result == response
    assert connection.query_cursor.calls[0][1] == (SCOPE.site_id,)
    statement, params = connection.query_cursor.calls[1]
    assert "email_draft_material_receipts" in statement
    assert params == (SCOPE.site_id, "email_draft_material", "save", "draft-save-01")

    drift = PostgresEmailDraftMaterialRepository(_Connection([("sha256:" + "b" * 64, response)]))
    with pytest.raises(EmailDraftMaterialReplayConflict, match="replay drift"):
        drift.replay(
            SCOPE,
            purpose="email_draft_material",
            operation="save",
            idempotency_key="draft-save-01",
            request_digest=digest,
        )


def test_postgres_repository_finalize_commit_locks_then_closes_binding_and_receipt() -> None:
    connection = _Connection([None])
    repository = PostgresEmailDraftMaterialRepository(connection)
    now = datetime(2026, 8, 14, tzinfo=UTC)
    digest = "sha256:" + "a" * 64
    receipt = {
        "evidence_ref": "EVR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "digest": digest,
        "role_binding": "sha256:" + "b" * 64,
        "participants": [],
    }
    fields = {
        "inbox_item_ref": "INB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_ref": "DRF-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_revision": 1,
        "evidence_ref": receipt["evidence_ref"],
        "object_ref": "obs:v1:" + "c" * 32 + ":sha256:" + "d" * 64,
        "digest": digest,
        "media_type": "message/rfc822",
        "byte_size": 100,
        "authorization_receipt_ref": "DAR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "gateway_receipt_ref": "EGR-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "publication_ref": "PUB-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "message_ref": "MSG-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_ref": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "mailbox_config_revision": 1,
        "observer_delivery_ref": "DLV-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": "sha256:" + "e" * 64,
        "participant_binding_digest": "sha256:" + "f" * 64,
        "evidence_binding_digest": "sha256:" + "1" * 64,
        "participant_roles_digest": "sha256:" + "2" * 64,
        "role_binding_digest": receipt["role_binding"],
        "source_draft_evidence_ref": "EVR-01BX5ZZKBKACTAV9WEVGEMMVRZ",
        "source_draft_digest": "sha256:" + "3" * 64,
        "created_at": now,
    }

    assert (
        repository.commit_finalize(
            SCOPE,
            purpose="email_draft_material",
            idempotency_key="draft-finalize-01",
            request_digest=digest,
            receipt=receipt,
            binding=fields,
        )
        == receipt
    )

    statements = [statement for statement, _params in connection.query_cursor.calls]
    assert "pg_advisory_xact_lock" in statements[1]
    assert "email_final_mime_evidence_bindings" in statements[3]
    assert "email_draft_material_receipts" in statements[4]


@pytest.mark.parametrize(
    "row, succeeds",
    [
        ((True, True, True, True, True, False, False), True),
        ((False, True, True, True, True, False, False), False),
        ((True, True, True, True, True, True, False), False),
    ],
)
def test_postgres_repository_preflight_requires_tables_forced_rls_and_least_grants(
    row: tuple[bool, ...],
    succeeds: bool,
) -> None:
    repository = PostgresEmailDraftMaterialRepository(_Connection([row]))
    if succeeds:
        repository.preflight()
    else:
        with pytest.raises(ValueError, match="preflight"):
            repository.preflight()
