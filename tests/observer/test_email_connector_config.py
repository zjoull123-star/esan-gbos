from __future__ import annotations

from datetime import UTC, datetime

import pytest
from observer.email_connector_config import (
    EmailConnectorConfigConflict,
    EmailConnectorConfigProjection,
    InMemoryEmailConnectorConfigRepository,
    PostgresEmailConnectorConfigRepository,
)

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)


def _projection(
    *,
    revision: int = 1,
    digest: str | None = None,
    team_ref: str = "TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
) -> dict[str, object]:
    from services.email_gateway.models import canonical_digest

    payload: dict[str, object] = {
        "site_id": "alpha.example",
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "provider_kind": "imap_smtp",
        "entry_role": "primary",
        "business_purpose": "sales_follow_up",
        "team_ref": team_ref,
        "credential_ref": "secretref:v1/email-primary",
        "inbound_enabled": True,
        "activation_watermark": {
            "mailbox_id": "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "mailbox_config_revision": revision,
            "not_before": "2026-08-13T09:30:00Z",
        },
        "projection_revision": revision,
    }
    return {
        **payload,
        "projection_digest": digest or canonical_digest(payload),
    }


def _projection_v2(
    *,
    revision: int = 1,
    mailbox_address_identity_ref: str = "extid:v1:email:" + "M" * 43,
) -> dict[str, object]:
    from services.email_gateway.models import canonical_digest

    legacy = _projection(revision=revision)
    payload = {key: value for key, value in legacy.items() if key != "projection_digest"}
    payload["mailbox_address_identity_ref"] = mailbox_address_identity_ref
    return {**payload, "projection_digest": canonical_digest(payload)}


def test_config_projection_is_exactly_idempotent_and_returns_stable_receipt() -> None:
    repository = InMemoryEmailConnectorConfigRepository()

    first = repository.apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        projection=_projection(),
        projected_at=NOW,
    )
    replay = repository.apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        projection=_projection(),
        projected_at=NOW,
    )

    assert first.to_wire() == {
        "schema_version": "1.0",
        "receipt_ref": first.receipt_ref,
        "config_publication_ref": "MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "payload_digest": _projection()["projection_digest"],
    }
    assert replay.to_wire() == first.to_wire()
    assert first.replayed is False
    assert replay.replayed is True
    assert len(repository.projections) == 1
    assert "secretref" not in repr(first)


def test_config_projection_rejects_digest_drift_same_revision_and_unseen_stale() -> None:
    repository = InMemoryEmailConnectorConfigRepository()
    repository.apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        projection=_projection(),
        projected_at=NOW,
    )
    repository.apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAW",
        projection=_projection(revision=3),
        projected_at=NOW,
    )

    with pytest.raises(EmailConnectorConfigConflict):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            projection=_projection(team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAX"),
            projected_at=NOW,
        )
    with pytest.raises(EmailConnectorConfigConflict):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAY",
            projection=_projection(revision=2),
            projected_at=NOW,
        )
    with pytest.raises(ValueError):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAZ",
            projection=_projection(digest="sha256:" + "0" * 64),
            projected_at=NOW,
        )


def test_config_projection_rejects_fake_provider_extra_fields_and_identity_drift() -> None:
    repository = InMemoryEmailConnectorConfigRepository()

    with pytest.raises(ValueError):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            projection={**_projection(), "provider_kind": "fake"},
            projected_at=NOW,
        )
    with pytest.raises(ValueError):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            projection={**_projection(), "unexpected": "raw-address@example.invalid"},
            projected_at=NOW,
        )

    repository.apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        projection=_projection(),
        projected_at=NOW,
    )
    changed_instance = {
        **_projection(revision=2),
        "observer_connector_instance_ref": "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAX",
    }
    from services.email_gateway.models import canonical_digest

    changed_instance["projection_digest"] = canonical_digest(
        {key: value for key, value in changed_instance.items() if key != "projection_digest"}
    )
    with pytest.raises(EmailConnectorConfigConflict):
        repository.apply(
            config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAW",
            projection=changed_instance,
            projected_at=NOW,
        )


def test_config_projection_accepts_exact_legacy_v1_and_identity_bound_v2() -> None:
    legacy = EmailConnectorConfigProjection.from_wire(_projection())
    current = EmailConnectorConfigProjection.from_wire(_projection_v2(revision=2))

    assert legacy.mailbox_address_identity_ref is None
    assert current.mailbox_address_identity_ref == "extid:v1:email:" + "M" * 43
    assert legacy.comparable() != current.comparable()
    assert current.mailbox_address_identity_ref not in repr(current)
    assert "mailbox_address_identity_ref=<redacted>" in repr(current)

    with pytest.raises(ValueError):
        EmailConnectorConfigProjection.from_wire(
            {**_projection_v2(), "unexpected": "owner@example.invalid"}
        )
    with pytest.raises(ValueError):
        EmailConnectorConfigProjection.from_wire(
            _projection_v2(mailbox_address_identity_ref="owner@example.invalid")
        )


def test_config_projection_replay_rejects_v1_v2_identity_drift() -> None:
    repository = InMemoryEmailConnectorConfigRepository()
    publication_ref = "MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    repository.apply(
        config_publication_ref=publication_ref,
        projection=_projection(),
        projected_at=NOW,
    )

    with pytest.raises(EmailConnectorConfigConflict):
        repository.apply(
            config_publication_ref=publication_ref,
            projection=_projection_v2(),
            projected_at=NOW,
        )


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> None:
        return None


class _RecordingTransaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = _RecordingCursor()

    def transaction(self) -> _RecordingTransaction:
        return _RecordingTransaction()

    def cursor(self) -> _RecordingCursor:
        return self.recording_cursor


@pytest.mark.parametrize(
    ("projection", "expected_identity_ref"),
    [(_projection(), None), (_projection_v2(), "extid:v1:email:" + "M" * 43)],
)
def test_postgres_config_insert_writes_nullable_mailbox_identity_ref(
    projection: dict[str, object],
    expected_identity_ref: str | None,
) -> None:
    connection = _RecordingConnection()
    PostgresEmailConnectorConfigRepository(connection).apply(
        config_publication_ref="MCP-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        projection=projection,
        projected_at=NOW,
    )

    insert_sql, insert_params = next(
        (sql, params)
        for sql, params in connection.recording_cursor.statements
        if "INSERT INTO observer.email_connector_config_projections" in sql
    )
    assert "mailbox_address_identity_ref" in insert_sql
    assert insert_params is not None
    assert insert_params[-1] == expected_identity_ref
