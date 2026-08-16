from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.email_signal_queue import (
    CurrentEmailConnectorConfig,
    EmailSignalConflict,
    EmailSignalRequest,
    InMemoryEmailSignalRepository,
)
from observer.models import TenantScope

NOW = datetime(2026, 8, 14, 8, tzinfo=UTC)
SITE = "alpha.example"
OCI = "OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV"
MAILBOX = "MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV"
ACTIVATION = NOW - timedelta(hours=1)
MIGRATION = (
    Path(__file__).parents[2]
    / "services"
    / "observer"
    / "migrations"
    / "022_wecom_app_mail_signals.sql"
)


def _request(**changes: object) -> EmailSignalRequest:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "site_id": SITE,
        "signal_kind": "callback",
        "observer_connector_instance_ref": OCI,
        "activation_watermark": {
            "mailbox_id": MAILBOX,
            "mailbox_config_revision": 7,
            "not_before": ACTIVATION.isoformat().replace("+00:00", "Z"),
        },
        "count_hint": 2,
        "callback_timestamp": NOW.isoformat().replace("+00:00", "Z"),
        "payload_digest": "sha256:" + "1" * 64,
        "nonce_digest": "sha256:" + "2" * 64,
        "replay_key_digest": "sha256:" + "3" * 64,
        "idempotency_key": "email-signal:" + "4" * 64,
    }
    value.update(changes)
    return EmailSignalRequest.from_wire(value)


def _repository(*, provider_kind: str = "wecom_app_mail") -> InMemoryEmailSignalRepository:
    return InMemoryEmailSignalRepository(
        configs=(
            CurrentEmailConnectorConfig(
                site_id=SITE,
                observer_connector_instance_ref=OCI,
                provider_kind=provider_kind,
                mailbox_ref=MAILBOX,
                mailbox_config_revision=7,
                activation_not_before=ACTIVATION,
                inbound_enabled=True,
            ),
        )
    )


def test_duplicate_signal_returns_exact_original_receipt_and_drift_fails_closed() -> None:
    repository = _repository()
    scope = TenantScope(SITE, "observation_processing")
    request = _request()

    original = repository.accept(scope, request=request, accepted_at=NOW)
    duplicate = repository.accept(scope, request=request, accepted_at=NOW + timedelta(seconds=1))

    assert duplicate == original
    assert duplicate.to_wire() == original.to_wire()
    assert len(repository.signals) == 1
    with pytest.raises(EmailSignalConflict, match="replay"):
        repository.accept(
            scope,
            request=_request(payload_digest="sha256:" + "9" * 64),
            accepted_at=NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize(
    "signal_request",
    [
        _request(site_id="other.example"),
        _request(observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAA"),
        _request(
            activation_watermark={
                "mailbox_id": MAILBOX,
                "mailbox_config_revision": 8,
                "not_before": ACTIVATION.isoformat().replace("+00:00", "Z"),
            }
        ),
        _request(
            activation_watermark={
                "mailbox_id": MAILBOX,
                "mailbox_config_revision": 7,
                "not_before": (ACTIVATION + timedelta(seconds=1))
                .isoformat()
                .replace("+00:00", "Z"),
            }
        ),
    ],
)
def test_signal_acceptance_requires_exact_current_config_binding(
    signal_request: EmailSignalRequest,
) -> None:
    with pytest.raises(PermissionError):
        _repository().accept(
            TenantScope(SITE, "observation_processing"),
            request=signal_request,
            accepted_at=NOW,
        )

    with pytest.raises(PermissionError):
        _repository(provider_kind="imap_smtp").accept(
            TenantScope(SITE, "observation_processing"),
            request=_request(),
            accepted_at=NOW,
        )


def test_signal_wire_is_closed_bounded_and_contains_no_message_or_provider_cursor() -> None:
    request = _request()
    assert set(request.to_wire()) == {
        "schema_version",
        "site_id",
        "signal_kind",
        "observer_connector_instance_ref",
        "activation_watermark",
        "count_hint",
        "callback_timestamp",
        "payload_digest",
        "nonce_digest",
        "replay_key_digest",
        "idempotency_key",
    }
    for forbidden in ("mail_id", "cursor", "delivery_id", "raw_callback", "plaintext", "eml"):
        assert forbidden not in repr(request).lower()
        with pytest.raises(ValueError):
            EmailSignalRequest.from_wire({**request.to_wire(), forbidden: "private"})


def test_reconciliation_signal_has_no_fabricated_callback_identity() -> None:
    value = _request().to_wire()
    value.update(
        {
            "signal_kind": "reconciliation",
            "count_hint": None,
            "callback_timestamp": None,
            "nonce_digest": None,
        }
    )
    request = EmailSignalRequest.from_wire(value)

    assert request.signal_kind == "reconciliation"
    assert request.count_hint is None
    assert request.callback_timestamp is None
    assert request.nonce_digest is None


def test_migration_has_immutable_facts_durable_work_force_rls_and_least_grants() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for table in ("email_signals", "email_signal_work"):
        assert f"create table if not exists observer.{table}" in sql
        assert f"alter table observer.{table} enable row level security" in sql
        assert f"alter table observer.{table} force row level security" in sql
        assert f"revoke all on observer.{table} from public" in sql
    for required in (
        "signal_kind",
        "count_hint",
        "payload_digest",
        "callback_timestamp",
        "nonce_digest",
        "replay_key_digest",
        "activation_not_before",
        "lease_generation",
        "lease_expires_at",
        "heartbeat_at",
        "ack_receipt_ref",
        "dead_letter",
        "max_attempts",
        "for update skip locked",
    ):
        assert required in sql
    facts = sql[: sql.index("create table if not exists observer.email_signal_work")]
    for forbidden in (
        "mail_id",
        "provider_cursor",
        "raw_callback",
        "plaintext",
        "raw_eml",
        "message_body",
        "gateway_state",
    ):
        assert forbidden not in facts
    assert "grant select, insert on observer.email_signals to gbos_observer_app" in sql
    assert "grant select, insert, update" not in sql
