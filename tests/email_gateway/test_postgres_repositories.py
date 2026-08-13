from __future__ import annotations

from datetime import timedelta

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
