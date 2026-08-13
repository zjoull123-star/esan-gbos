from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.observer.observer.identity_resolution import ParticipantIdentityResolution

NOW = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
SITE = "alpha.example"
OPAQUE = "extid:v1:email:" + "A" * 43
EID = "EID-01ARZ3NDEKTSV4RRFFQ69G5FAV"
TEAM = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _resolution(*, provider: str = "email") -> ParticipantIdentityResolution:
    return ParticipantIdentityResolution(
        site_id=SITE,
        identity_provider=provider,
        external_subject_ref=(
            OPAQUE if provider == "email" else f"extid:v1:{provider}:" + "B" * 43
        ),
        mapping_ref=EID,
        mapping_revision=3,
        team_ref=TEAM,
        target_type="Party",
        target_ref="protected-target@example.invalid",
        status="confirmed",
        resolved_at=NOW,
        recorded_at=NOW + timedelta(seconds=1),
    )


def test_frozen_projection_is_schema_exact_deterministic_and_target_free() -> None:
    from services.observer.observer.identity_projection_outbox import (
        build_identity_projection_payload,
    )

    first = build_identity_projection_payload(_resolution(), "sales_follow_up")
    replay = build_identity_projection_payload(_resolution(), "sales_follow_up")

    assert first == replay
    assert set(first) == {
        "site_id",
        "processing_purpose",
        "opaque_address_ref",
        "external_identity_ref",
        "external_identity_revision",
        "identity_type",
        "team_ref",
        "status",
        "projection_receipt",
        "observed_at",
    }
    assert first["processing_purpose"] == "sales_follow_up"
    assert first["opaque_address_ref"] == OPAQUE
    assert first["projection_receipt"].startswith("sha256:")
    assert "protected-target" not in repr(first)
    assert "target_ref" not in first
    source = Path("services/observer/observer/identity_projection_outbox.py").read_text(
        encoding="utf-8"
    )
    assert "target_ref" not in source


def test_only_email_resolutions_and_contract_purposes_can_enter_relay() -> None:
    from services.observer.observer.identity_projection_outbox import (
        build_identity_projection_payload,
    )

    with pytest.raises(ValueError, match="email"):
        build_identity_projection_payload(_resolution(provider="wecom"), "sales_follow_up")
    with pytest.raises(ValueError, match="purpose"):
        build_identity_projection_payload(_resolution(), "identity_resolution")


def test_in_memory_outbox_fences_lease_and_dead_letters_after_five_attempts() -> None:
    from services.observer.observer.identity_projection_outbox import (
        IdentityProjectionRelayFenceConflict,
        InMemoryIdentityProjectionOutbox,
    )

    outbox = InMemoryIdentityProjectionOutbox()
    payload = build_payload = __import__(
        "services.observer.observer.identity_projection_outbox",
        fromlist=["build_identity_projection_payload"],
    ).build_identity_projection_payload(_resolution(), "sales_follow_up")
    assert build_payload["projection_receipt"] == payload["projection_receipt"]
    outbox.append(payload, queued_at=NOW)

    for attempt in range(1, 6):
        current = NOW + timedelta(minutes=attempt - 1)
        claim = outbox.claim(
            site_id=SITE,
            worker_id="identity-projector-1",
            now=current,
            lease_duration=timedelta(seconds=30),
        )
        assert claim is not None and claim.attempt == attempt
        assert OPAQUE not in repr(claim)
        outbox.heartbeat(
            claim,
            worker_id="identity-projector-1",
            now=current + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )
        state = outbox.fail(
            claim,
            worker_id="identity-projector-1",
            now=current + timedelta(seconds=2),
            retry_at=current + timedelta(minutes=1),
            error_code="gateway_timeout",
            retryable=True,
        )
        assert state == ("dead_letter" if attempt == 5 else "retry")

    with pytest.raises(IdentityProjectionRelayFenceConflict):
        outbox.acknowledge(
            claim,
            worker_id="identity-projector-1",
            now=NOW + timedelta(hours=1),
            receipt_ref=str(payload["projection_receipt"]),
        )


def test_non_retryable_failure_dead_letters_on_first_attempt() -> None:
    from services.observer.observer.identity_projection_outbox import (
        InMemoryIdentityProjectionOutbox,
        build_identity_projection_payload,
    )

    outbox = InMemoryIdentityProjectionOutbox()
    payload = build_identity_projection_payload(_resolution(), "customer_service")
    outbox.append(payload, queued_at=NOW)
    claim = outbox.claim(
        site_id=SITE,
        worker_id="identity-projector-1",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    assert (
        outbox.fail(
            claim,
            worker_id="identity-projector-1",
            now=NOW + timedelta(seconds=1),
            retry_at=NOW + timedelta(minutes=1),
            error_code="gateway_rejected",
            retryable=False,
        )
        == "dead_letter"
    )


class _AtomicCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []
        self._one: tuple[Any, ...] | None = None
        self._many: list[tuple[Any, ...]] = []

    def __enter__(self) -> _AtomicCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if (
            "FROM observer.participant_identity_resolutions" in normalized
            and "FOR UPDATE" in normalized
        ):
            self._many = []
            self._one = None
        elif "INSERT INTO observer.participant_identity_resolutions" in normalized:
            resolution = _resolution()
            self._one = (
                resolution.site_id,
                resolution.identity_provider,
                resolution.external_subject_ref,
                resolution.mapping_ref,
                resolution.mapping_revision,
                resolution.team_ref,
                resolution.target_type,
                resolution.target_ref,
                resolution.status,
                resolution.resolved_at,
                resolution.recorded_at,
            )
            self._many = []
        elif "SELECT DISTINCT latest.business_purpose" in normalized:
            self._many = [("customer_service",), ("sales_follow_up",)]
            self._one = self._many[0]
        elif "INSERT INTO observer.identity_projection_outbox" in normalized:
            assert params is not None
            import json

            self._one = (params[4], params[6], json.loads(str(params[5])))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._many


class _AtomicConnection:
    def __init__(self) -> None:
        self.cursor_value = _AtomicCursor()
        self.transactions = 0

    def transaction(self) -> nullcontext[None]:
        self.transactions += 1
        return nullcontext()

    def cursor(self) -> _AtomicCursor:
        return self.cursor_value


def test_postgres_resolution_enqueues_each_current_exact_purpose_in_same_transaction() -> None:
    from services.observer.observer.identity_resolution import PostgresIdentityResolutionRepository
    from services.observer.observer.models import TenantScope

    connection = _AtomicConnection()
    repository = PostgresIdentityResolutionRepository(connection)  # type: ignore[arg-type]
    recorded = repository.record(
        TenantScope(SITE, "observation_processing"),
        _resolution(),
    )

    assert recorded.mapping_revision == 3
    assert connection.transactions == 1
    outbox_inserts = [
        (sql, params)
        for sql, params in connection.cursor_value.statements
        if "INSERT INTO observer.identity_projection_outbox" in sql
    ]
    assert len(outbox_inserts) == 2
    assert {params[1] for _sql, params in outbox_inserts if params is not None} == {
        "customer_service",
        "sales_follow_up",
    }
    assert all("protected-target" not in repr(params) for _sql, params in outbox_inserts)
    shared_locks = [
        params
        for sql, params in connection.cursor_value.statements
        if "pg_advisory_xact_lock" in sql and params == (f"identity-projection-seed:{SITE}:{TEAM}",)
    ]
    assert shared_locks == [(f"identity-projection-seed:{SITE}:{TEAM}",)]


def test_outbox_same_revision_collision_rejects_durable_payload_drift() -> None:
    from services.observer.observer.identity_projection_outbox import (
        IdentityProjectionRelayConflict,
        _insert_payload,
        build_identity_projection_payload,
    )

    class ConflictCursor:
        def __init__(self) -> None:
            self.row: tuple[object, ...] | None = None

        def execute(self, sql: str, _params: tuple[object, ...]) -> None:
            if "INSERT INTO observer.identity_projection_outbox" in sql:
                self.row = None
            else:
                self.row = (
                    "sha256:" + "f" * 64,
                    "sha256:" + "e" * 64,
                    {"unexpected": "drift"},
                )

        def fetchone(self) -> tuple[object, ...] | None:
            return self.row

    with pytest.raises(IdentityProjectionRelayConflict, match="conflict"):
        _insert_payload(
            ConflictCursor(),
            build_identity_projection_payload(_resolution(), "sales_follow_up"),
        )
