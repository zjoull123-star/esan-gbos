from __future__ import annotations

from datetime import UTC, datetime

import pytest
from observer.email_connector_config import (
    EmailConnectorConfigConflict,
    InMemoryEmailConnectorConfigRepository,
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
