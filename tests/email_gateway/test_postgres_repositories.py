from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest


class _ExplodingConnection:
    def cursor(self):
        raise RuntimeError("postgresql://user:super-secret@example.invalid/db")

    def commit(self) -> None:  # pragma: no cover - the cursor fails first
        raise AssertionError("commit must not run")

    def rollback(self) -> None:
        pass


def _encrypt(value: str) -> bytes:
    return bytes(byte ^ 0xA5 for byte in value.encode("utf-8"))


def _decrypt(value: bytes) -> str:
    return bytes(byte ^ 0xA5 for byte in value).decode("utf-8")


def test_postgres_repository_exceptions_are_redacted(scope) -> None:
    from services.email_gateway.models import ValidationError
    from services.email_gateway.repositories.mailboxes import PostgresMailboxRepository

    repository = PostgresMailboxRepository(
        _ExplodingConnection(),
        encrypt_restricted_text=_encrypt,
        decrypt_restricted_text=_decrypt,
    )

    with pytest.raises(ValidationError) as caught:
        repository.list(scope)

    assert "super-secret" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_postgres_repository_surface_is_complete() -> None:
    from services.email_gateway.repositories.audit import PostgresAuditRepository
    from services.email_gateway.repositories.identity import (
        PostgresIdentityProjectionRepository,
    )
    from services.email_gateway.repositories.intake import PostgresIntakeRepository
    from services.email_gateway.repositories.mailboxes import (
        PostgresMailboxConfigOutboxRepository,
        PostgresMailboxRepository,
    )
    from services.email_gateway.repositories.workflow import PostgresWorkflowRepository

    mailbox = PostgresMailboxRepository(
        _ExplodingConnection(),
        encrypt_restricted_text=_encrypt,
        decrypt_restricted_text=_decrypt,
    )
    config_outbox = PostgresMailboxConfigOutboxRepository(_ExplodingConnection())
    intake = PostgresIntakeRepository(
        _ExplodingConnection(),
        encrypt_restricted_text=_encrypt,
        decrypt_restricted_text=_decrypt,
    )
    identity = PostgresIdentityProjectionRepository(_ExplodingConnection())
    workflow = PostgresWorkflowRepository(_ExplodingConnection())
    audit = PostgresAuditRepository(_ExplodingConnection())

    expected = {
        mailbox: {
            "get",
            "list",
            "upsert",
            "claim",
            "heartbeat",
            "mark_delivered",
            "mark_failed",
        },
        intake: {"accept"},
        identity: {"get", "apply"},
        workflow: {
            "save_inbox",
            "get_inbox",
            "save_suggestion",
            "get_suggestion",
            "save_conversation",
            "get_conversation_for",
            "save_draft",
            "get_draft",
            "append_audit",
            "replay",
            "remember",
            "audit_count",
            "apply_inbox_operation",
            "get_conversation",
            "split_conversation",
        },
        audit: {"append"},
        config_outbox: {"claim", "heartbeat", "mark_delivered", "mark_failed"},
    }
    for repository, methods in expected.items():
        assert methods <= {name for name in dir(repository) if callable(getattr(repository, name))}

    assert timedelta(seconds=1) < timedelta(hours=1)


def test_config_claim_projection_wire_builder_is_exposed() -> None:
    from services.email_gateway.repositories.mailboxes import MailboxConfigOutboxClaim

    assert callable(getattr(MailboxConfigOutboxClaim, "to_connector_projection_wire", None))


def test_mailbox_repository_sql_carries_only_opaque_mailbox_identity_ref() -> None:
    from services.email_gateway.repositories.mailboxes import PostgresMailboxRepository

    source = inspect.getsource(PostgresMailboxRepository)

    assert "mailbox_address_identity_ref" in PostgresMailboxRepository.GET_SQL
    assert "mailbox_address_identity_ref" in PostgresMailboxRepository.LIST_SQL
    assert "mailbox_address_identity_ref" in source
    assert "canonical_mailbox_address" not in source
    assert "mailbox@example.invalid" not in source


def test_config_claim_builds_v2_projection_and_redacts_mailbox_identity_ref() -> None:
    from services.email_gateway.repositories.mailboxes import MailboxConfigOutboxClaim

    identity_ref = "extid:v1:email:" + "M" * 43
    now = datetime(2026, 8, 13, tzinfo=UTC)
    claim = MailboxConfigOutboxClaim(
        site_id="site.local",
        config_publication_ref="MCP-01",
        mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        mailbox_config_revision=2,
        observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        provider="imap_smtp",
        entry_role="primary",
        business_purpose="sales_follow_up",
        default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        credential_ref="secretref:v1/email/primary",
        inbound_enabled=True,
        outbound_enabled=False,
        mailbox_status="active",
        mailbox_address_identity_ref=identity_ref,
        activation_not_before=now,
        processing_purpose="sales_follow_up",
        request_id="REQ-01",
        idempotency_key="IDEM-01",
        payload_digest="sha256:" + "a" * 64,
        status="leased",
        attempt=1,
        lease_owner="worker-01",
        lease_expires_at=now + timedelta(minutes=1),
        lease_generation=1,
        fence_token="redacted-test-token",
    )

    wire = claim.to_connector_projection_wire()
    assert wire["mailbox_address_identity_ref"] == identity_ref
    digest_input = dict(wire)
    digest_input.pop("projection_digest")
    from services.email_gateway.models import canonical_digest

    assert wire["projection_digest"] == canonical_digest(digest_input)
    assert identity_ref not in repr(claim)


def test_config_claim_sql_dead_letters_legacy_null_mailbox_identity() -> None:
    from services.email_gateway.repositories.mailboxes import PostgresMailboxRepository

    source = inspect.getsource(PostgresMailboxRepository.claim)

    assert "mailbox_address_identity_ref IS NULL" in source
    assert "missing_mailbox_identity" in source
    assert "mailbox_address_identity_ref IS NOT NULL" in source


def test_postgres_conversation_split_calls_narrow_database_command_not_table_delete() -> None:
    from services.email_gateway.repositories.workflow import PostgresWorkflowRepository

    source = inspect.getsource(PostgresWorkflowRepository.split_conversation)
    assert "clear_conversation_members_for_split" in source
    assert "DELETE FROM email_gateway.conversation_messages" not in source
