from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from observer.email_publication import EmailMessagePublication, EmailPublicationParticipant
from observer.email_publication_outbox import (
    EMAIL_PUBLICATION_RELAY_STATES,
    EmailPublicationConflict,
    EmailPublicationRelayConflict,
    EmailPublicationRelayFenceConflict,
    InMemoryEmailPublicationOutbox,
)

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _publication(*, subject_digest: str = "sha256:" + "a" * 64) -> EmailMessagePublication:
    return EmailMessagePublication(
        publication_id="PUB-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        site_id="alpha.example",
        mailbox_id="MBX-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        mailbox_config_revision=3,
        observer_connector_instance_ref="OCI-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        observer_delivery_ref="DLV-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        received_at=NOW,
        evidence_refs=("EVR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",),
        participants=(
            EmailPublicationParticipant(
                address_role="from",
                identity_ref="extid:v1:email:" + "d" * 43,
            ),
        ),
        subject_digest=subject_digest,
        message_id_digest="sha256:" + "e" * 64,
        in_reply_to_digest=None,
        references_digests=(),
        publication_revision=1,
        idempotency_key="idem:v1:" + "f" * 64,
    )


def test_identical_digest_replay_returns_original_append_only_record() -> None:
    outbox = InMemoryEmailPublicationOutbox()
    first = outbox.append(_publication())
    replay = outbox.append(_publication())

    assert first.publication_id == replay.publication_id
    assert first.payload_sha256 == replay.payload_sha256
    assert first.replayed is False
    assert replay.replayed is True
    assert len(outbox.records) == 1


def test_same_delivery_with_payload_drift_is_safe_conflict() -> None:
    outbox = InMemoryEmailPublicationOutbox()
    outbox.append(_publication())

    with pytest.raises(EmailPublicationConflict, match="publication_payload_conflict") as exc:
        outbox.append(_publication(subject_digest="sha256:" + "9" * 64))

    assert "PRIVATE" not in repr(exc.value)
    assert len(outbox.records) == 1


def test_migration_adds_only_observer_publication_fence_tables_and_forced_rls() -> None:
    sql = (
        ROOT / "services" / "observer" / "migrations" / "014_email_gateway_publication.sql"
    ).read_text(encoding="utf-8")
    tables = (
        "email_connector_config_projections",
        "email_poll_batches",
        "email_poll_batch_deliveries",
        "email_message_publication_outbox",
    )
    for table in tables:
        assert f"CREATE TABLE IF NOT EXISTS observer.{table}" in sql
        assert f"ALTER TABLE observer.{table} FORCE ROW LEVEL SECURITY" in sql
        assert f"REVOKE ALL ON observer.{table} FROM PUBLIC" in sql
    assert "email_message_publication_outbox_immutable" in sql
    config_sql = (
        ROOT / "services" / "observer" / "migrations" / "015_email_gateway_connector_config.sql"
    ).read_text(encoding="utf-8")
    assert "email_connector_config_projection_immutable" in config_sql
    for field in (
        "config_publication_ref",
        "entry_role",
        "business_purpose",
        "team_ref",
        "credential_ref",
        "inbound_enabled",
        "activation_not_before",
    ):
        assert field in (sql + config_sql)
    assert "UNIQUE (site_id, mailbox_id, observer_delivery_ref)" in sql
    for relay_column in (
        "relay_status",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "relay_generation",
        "delivery_receipt",
        "delivery_receipt_digest",
        "delivered_at",
    ):
        assert relay_column in sql
    assert "max_attempts BETWEEN 1 AND 5" in sql
    assert "payload IS DISTINCT FROM OLD.payload" in sql
    repository_source = (
        ROOT / "services" / "observer" / "observer" / "email_publication_outbox.py"
    ).read_text(encoding="utf-8")
    assert "FOR UPDATE SKIP LOCKED" in repository_source
    lowered = sql.lower()
    assert "create table observer.gateway_" not in lowered
    assert "create table observer.email_gateway_" not in lowered
    assert "provider_cursor" not in lowered


def test_publication_relay_uses_a_dedicated_least_privilege_role() -> None:
    sql = (
        ROOT / "services" / "observer" / "migrations" / "015_email_gateway_connector_config.sql"
    ).read_text(encoding="utf-8")
    lowered = sql.lower()

    assert "create role gbos_observer_publisher" in lowered
    assert "nologin" in lowered
    assert "grant usage on schema observer to gbos_observer_publisher" in lowered
    assert (
        "grant select on observer.email_message_publication_outbox\n    to gbos_observer_publisher"
    ) in lowered
    assert "on observer.email_message_publication_outbox to gbos_observer_publisher" in lowered
    assert (
        "insert on observer.email_message_publication_outbox to gbos_observer_publisher"
    ) not in lowered
    assert "delivery_receipt, delivery_receipt_digest, delivered_at, updated_at" in lowered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publication_id", "PUB-invalid"),
        ("site_id", "bad/site"),
        ("mailbox_id", "MBX-invalid"),
        ("mailbox_config_revision", 2_147_483_648),
        ("observer_connector_instance_ref", "sales-mailbox"),
        ("observer_delivery_ref", "delivery-001"),
        ("evidence_refs", ("EVR-invalid",)),
        ("publication_revision", 2_147_483_648),
        ("idempotency_key", "email-publication:" + "f" * 64),
    ],
)
def test_publication_constructor_rejects_non_contract_identifiers(field, value) -> None:
    values = {
        name: getattr(_publication(), name) for name in EmailMessagePublication.__dataclass_fields__
    }
    values[field] = value
    with pytest.raises(ValueError):
        EmailMessagePublication(**values)


def test_publication_constructor_rejects_duplicate_or_unbounded_collections() -> None:
    base = _publication()
    values = {name: getattr(base, name) for name in EmailMessagePublication.__dataclass_fields__}
    values["participants"] = base.participants * 257
    with pytest.raises(ValueError):
        EmailMessagePublication(**values)

    values["participants"] = base.participants * 2
    with pytest.raises(ValueError):
        EmailMessagePublication(**values)

    values["participants"] = base.participants
    values["evidence_refs"] = base.evidence_refs * 2
    with pytest.raises(ValueError):
        EmailMessagePublication(**values)

    values["evidence_refs"] = base.evidence_refs
    values["references_digests"] = ("sha256:" + "1" * 64,) * 2
    with pytest.raises(ValueError):
        EmailMessagePublication(**values)


def test_participant_constructor_rejects_bad_or_duplicate_wire_shape() -> None:
    with pytest.raises(ValueError):
        EmailPublicationParticipant("sender", "extid:v1:email:" + "a" * 43)
    with pytest.raises(ValueError):
        EmailPublicationParticipant("from", "private@example.invalid")


def test_relay_claim_heartbeat_release_and_ack_are_generation_fenced() -> None:
    outbox = InMemoryEmailPublicationOutbox()
    outbox.append(_publication(), max_attempts=2)

    claim = outbox.claim(
        site_id="alpha.example",
        worker_id="gateway-relay-1",
        now=NOW,
        lease_seconds=30,
    )
    assert claim is not None
    assert claim.status == "leased"
    assert claim.attempt_count == 1
    assert claim.max_attempts == 2
    assert claim.generation == 1
    assert claim.payload == _publication().to_wire()
    assert claim.to_delivery_envelope() == {
        "publication": _publication().to_wire(),
        "payload_digest": "sha256:" + _publication().payload_sha256,
    }
    assert "extid:v1:email" not in repr(claim)

    with pytest.raises(EmailPublicationRelayFenceConflict):
        outbox.heartbeat(
            site_id="alpha.example",
            publication_id=claim.publication_id,
            worker_id="gateway-relay-1",
            expected_generation=0,
            now=NOW,
            lease_seconds=30,
        )

    heartbeat = outbox.heartbeat(
        site_id="alpha.example",
        publication_id=claim.publication_id,
        worker_id="gateway-relay-1",
        expected_generation=claim.generation,
        now=NOW,
        lease_seconds=60,
    )
    assert heartbeat.generation == claim.generation
    assert heartbeat.lease_expires_at > claim.lease_expires_at

    released = outbox.release(
        site_id="alpha.example",
        publication_id=claim.publication_id,
        worker_id="gateway-relay-1",
        expected_generation=claim.generation,
        now=NOW,
        next_attempt_at=NOW + timedelta(seconds=10),
        error_code="gateway_503",
    )
    assert released.status == "retry_wait"
    assert (
        outbox.claim(
            site_id="alpha.example",
            worker_id="gateway-relay-2",
            now=NOW + timedelta(seconds=9),
            lease_seconds=30,
        )
        is None
    )

    retry = outbox.claim(
        site_id="alpha.example",
        worker_id="gateway-relay-2",
        now=NOW + timedelta(seconds=10),
        lease_seconds=30,
    )
    assert retry is not None
    assert retry.attempt_count == 2
    assert retry.generation == 2

    gateway_receipt = {
        "receipt_ref": "EGR-01KZQEC7B9A41Q2ZCDPFGQ7V5K",
        "publication_ref": retry.publication_id,
        "payload_digest": retry.transport_payload_digest,
    }
    delivered = outbox.acknowledge(
        site_id="alpha.example",
        publication_id=retry.publication_id,
        worker_id="gateway-relay-2",
        expected_generation=retry.generation,
        receipt=gateway_receipt,
        now=NOW + timedelta(seconds=11),
    )
    assert delivered.status == "delivered"
    assert delivered.replayed is False
    replay = outbox.acknowledge(
        site_id="alpha.example",
        publication_id=retry.publication_id,
        worker_id="stale-retry-after-success",
        expected_generation=0,
        receipt=gateway_receipt,
        now=NOW + timedelta(seconds=12),
    )
    assert replay == delivered.__class__(
        publication_id=delivered.publication_id,
        receipt_ref=delivered.receipt_ref,
        receipt_sha256=delivered.receipt_sha256,
        status="delivered",
        delivered_at=delivered.delivered_at,
        replayed=True,
    )

    with pytest.raises(EmailPublicationRelayConflict, match="receipt_replay_drift"):
        outbox.acknowledge(
            site_id="alpha.example",
            publication_id=retry.publication_id,
            worker_id="gateway-relay-2",
            expected_generation=retry.generation,
            receipt={**gateway_receipt, "receipt_ref": "EGR-drift"},
            now=NOW + timedelta(seconds=12),
        )


def test_relay_has_exact_five_states_and_dead_letters_at_bounded_attempt_limit() -> None:
    assert (
        frozenset({"queued", "leased", "retry_wait", "delivered", "dead_letter"})
        == EMAIL_PUBLICATION_RELAY_STATES
    )
    outbox = InMemoryEmailPublicationOutbox()
    with pytest.raises(ValueError, match="max_attempts"):
        outbox.append(_publication(), max_attempts=6)
    outbox.append(_publication(), max_attempts=1)
    claim = outbox.claim(
        site_id="alpha.example",
        worker_id="gateway-relay-1",
        now=NOW,
        lease_seconds=30,
    )
    assert claim is not None
    terminal = outbox.release(
        site_id="alpha.example",
        publication_id=claim.publication_id,
        worker_id="gateway-relay-1",
        expected_generation=claim.generation,
        now=NOW,
        next_attempt_at=NOW + timedelta(seconds=10),
        error_code="gateway_rejected",
    )
    assert terminal.status == "dead_letter"
