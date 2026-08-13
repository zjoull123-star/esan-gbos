from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_RLS_TABLES = {
    "audit_events",
    "channel_messages",
    "content_expiration_receipts",
    "conversation_messages",
    "conversations",
    "identity_projection_receipts",
    "inbox_items",
    "inbox_operation_requests",
    "inbox_sla_clocks",
    "mailbox_config_outbox",
    "mailboxes",
    "mailbox_sla_policies",
    "message_participants",
    "publication_receipts",
    "reply_drafts",
    "retention_runs",
    "route_decisions",
    "routing_rules",
    "send_outbox",
    "thread_suggestions",
    "worker_heartbeats",
    "command_inbox",
    "send_outbox_state",
    "send_attempts",
    "provider_receipts",
    "reconciliation_receipts",
}


def _encrypt(value: str) -> bytes:
    return bytes(byte ^ 0xA5 for byte in value.encode("utf-8"))


def _decrypt(value: bytes) -> str:
    return bytes(byte ^ 0xA5 for byte in value).decode("utf-8")


def _gateway_enabled() -> str:
    if os.environ.get("GBOS_RUN_EMAIL_GATEWAY_POSTGRES") != "1":
        pytest.skip("set GBOS_RUN_EMAIL_GATEWAY_POSTGRES=1 for the disposable Gateway database")
    return os.environ["GBOS_EMAIL_GATEWAY_POSTGRES_DSN"]


def _apply_gateway_migrations(connection) -> None:
    root = Path(__file__).resolve().parents[2]
    migrations = sorted((root / "services" / "email_gateway" / "migrations").glob("*.sql"))
    assert len(migrations) == 8
    for path in migrations:
        connection.execute(path.read_text())


@pytest.mark.postgres_integration
def test_email_gateway_migrations_run_twice_with_forced_rls_and_no_forbidden_tables() -> None:
    dsn = _gateway_enabled()
    import psycopg

    root = Path(__file__).resolve().parents[2]
    migrations = sorted((root / "services" / "email_gateway" / "migrations").glob("*.sql"))
    assert len(migrations) == 8
    with psycopg.connect(dsn, autocommit=True) as connection:
        for _ in range(2):
            for path in migrations:
                connection.execute(path.read_text())
        rows = connection.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'email_gateway' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        ).fetchall()
        by_name = {str(row[0]): (bool(row[1]), bool(row[2])) for row in rows}
        assert set(by_name) == {*_RLS_TABLES, "schema_migrations"}
        assert by_name["schema_migrations"] == (False, False)
        assert {name for name, flags in by_name.items() if flags == (True, True)} == _RLS_TABLES
        assert not set(by_name).intersection(
            {"provider_deliveries", "provider_cursor", "raw_eml", "quarantine"}
        )

        public_grants = connection.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.table_privileges
            WHERE table_schema = 'email_gateway' AND grantee = 'PUBLIC'
            """
        ).fetchall()
        assert public_grants == []
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege(
                    'gbos_email_gateway_app',
                    'email_gateway.send_outbox',
                    'SELECT'
                ),
                has_table_privilege(
                    'gbos_email_gateway_app',
                    'email_gateway.send_outbox',
                    'INSERT'
                ),
                has_table_privilege(
                    'gbos_email_gateway_worker',
                    'email_gateway.mailbox_config_outbox',
                    'UPDATE'
                ),
                has_table_privilege(
                    'gbos_email_gateway_worker',
                    'email_gateway.mailboxes',
                    'UPDATE'
                )
            """
        ).fetchone()
        assert privileges == (True, False, True, False)

        mailbox_insert = """
            INSERT INTO email_gateway.mailboxes (
                site_id, mailbox_ref, address_display_ciphertext, provider,
                provider_account_ref, observer_connector_instance_ref, entry_role,
                business_purpose, default_team_ref, account_owner_user_ref,
                credential_ref, created_by, updated_by
            ) VALUES (
                %s, %s, %s, 'fake', %s, %s, 'primary', 'sales_follow_up',
                %s, %s, %s, %s, %s
            )
        """
        for site_id, suffix in (("alpha.example", "alpha"), ("beta.example", "beta")):
            connection.execute(
                mailbox_insert,
                (
                    site_id,
                    f"mailbox-{suffix}",
                    b"encrypted-address",
                    f"provider-{suffix}",
                    f"observer-{suffix}",
                    f"team-{suffix}",
                    f"owner-{suffix}",
                    f"credential-{suffix}",
                    "integration-test",
                    "integration-test",
                ),
            )

        connection.execute("SET ROLE gbos_email_gateway_app")
        connection.execute("SET gbos.site_id = 'alpha.example'")
        visible = connection.execute(
            "SELECT site_id, mailbox_ref FROM email_gateway.mailboxes ORDER BY mailbox_ref"
        ).fetchall()
        assert visible == [("alpha.example", "mailbox-alpha")]
        cross_site_update = connection.execute(
            """
            UPDATE email_gateway.mailboxes
            SET priority = 17
            WHERE site_id = 'beta.example'
            RETURNING mailbox_ref
            """
        ).fetchall()
        assert cross_site_update == []
        connection.execute("RESET ROLE")


@pytest.mark.postgres_integration
def test_email_gateway_postgres_repositories_are_atomic_scoped_and_replay_safe() -> None:
    dsn = _gateway_enabled()
    import psycopg

    from services.email_gateway.conversations import ConversationService
    from services.email_gateway.models import (
        AuditEvent,
        Conversation,
        Draft,
        EmailMessagePublication,
        GatewayActorScope,
        IdempotencyConflict,
        IdentityProjection,
        Mailbox,
        PublicationParticipant,
        RevisionConflict,
        TenantScope,
        ThreadSuggestion,
        ValidationError,
        canonical_digest,
    )
    from services.email_gateway.operations import InboxOperations
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

    now = datetime(2026, 8, 13, 4, 5, 6, tzinfo=UTC)
    alpha = TenantScope("repos-alpha.example", "sales_follow_up")
    beta = TenantScope("repos-beta.example", "sales_follow_up")
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    opaque_to = "extid:v1:email:" + "B" * 43
    unresolved_from = "unresolved:delivery:01ARZ3NDEKTSV4RRFFQ69G5FAV"

    with psycopg.connect(dsn, autocommit=True) as connection:
        _apply_gateway_migrations(connection)
        connection.execute("SET ROLE gbos_email_gateway_app")
        mailboxes = PostgresMailboxRepository(
            connection,
            encrypt_restricted_text=_encrypt,
            decrypt_restricted_text=_decrypt,
        )
        intake = PostgresIntakeRepository(
            connection,
            encrypt_restricted_text=_encrypt,
            decrypt_restricted_text=_decrypt,
        )
        identities = PostgresIdentityProjectionRepository(connection)
        workflow = PostgresWorkflowRepository(connection)
        audits = PostgresAuditRepository(connection)

        mailbox = Mailbox(
            mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            site_id=alpha.site_id,
            address_display="private-alpha@example.invalid",
            provider="imap_smtp",
            provider_account_ref="provider-alpha",
            observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            entry_role="primary",
            business_purpose=alpha.processing_purpose,
            default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAV",
            account_owner_user_ref="owner-alpha",
            priority=10,
            inbound_enabled=False,
            outbound_enabled=False,
            credential_ref="secretref:v1/email/alpha",
            status="draft",
            config_revision=1,
            observer_config_projection_receipt=None,
        )
        created = mailboxes.upsert(
            alpha,
            mailbox,
            expected_revision=0,
            actor_ref="admin-alpha",
            request_id="request-mailbox-create",
            idempotency_key="mailbox-create",
        )
        assert created.mailbox.config_revision == 1
        assert (
            mailboxes.upsert(
                alpha,
                mailbox,
                expected_revision=0,
                actor_ref="admin-alpha",
                request_id="request-mailbox-create",
                idempotency_key="mailbox-create",
            )
            == created
        )
        with pytest.raises(IdempotencyConflict):
            mailboxes.upsert(
                alpha,
                replace(mailbox, priority=99),
                expected_revision=1,
                actor_ref="admin-alpha",
                request_id="request-mailbox-drift",
                idempotency_key="mailbox-create",
            )
        mailbox_update = replace(
            mailbox,
            address_display="private-alpha-updated@example.invalid",
            provider="wecom_app_mail",
            provider_account_ref="provider-alpha-updated",
            observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAX",
            entry_role="workflow",
            default_team_ref="TEM-01ARZ3NDEKTSV4RRFFQ69G5FAX",
            account_owner_user_ref="owner-alpha-updated",
            priority=11,
            inbound_enabled=True,
            outbound_enabled=False,
            credential_ref="secretref:v1/email/alpha-updated",
            status="active",
            observer_config_projection_receipt="observer-projection-prior",
        )
        updated = mailboxes.upsert(
            alpha,
            mailbox_update,
            expected_revision=1,
            actor_ref="admin-alpha",
            request_id="request-mailbox-update",
            idempotency_key="mailbox-update",
        )
        mailbox = updated.mailbox
        assert mailbox.config_revision == 2
        assert mailbox == replace(mailbox_update, config_revision=2)
        with pytest.raises(RevisionConflict):
            mailboxes.upsert(
                alpha,
                replace(mailbox, priority=12),
                expected_revision=1,
                actor_ref="admin-alpha",
                request_id="request-mailbox-stale",
                idempotency_key="mailbox-stale",
            )
        assert mailboxes.get(alpha, mailbox.mailbox_ref) == mailbox
        assert mailboxes.list(alpha) == (mailbox,)
        assert mailboxes.list(beta) == ()

        second_mailbox = replace(
            mailbox,
            mailbox_ref="MBX-01ARZ3NDEKTSV4RRFFQ69G5FAW",
            address_display="private-alpha-2@example.invalid",
            provider_account_ref="provider-alpha-2",
            observer_connector_instance_ref="OCI-01ARZ3NDEKTSV4RRFFQ69G5FAW",
            config_revision=1,
        )
        second_mailbox = mailboxes.upsert(
            alpha,
            second_mailbox,
            expected_revision=0,
            actor_ref="admin-alpha",
            request_id="request-mailbox-create-2",
            idempotency_key="mailbox-create-2",
        ).mailbox

        publication = EmailMessagePublication(
            publication_ref="repo-publication-alpha",
            site_id=alpha.site_id,
            processing_purpose=alpha.processing_purpose,
            mailbox_ref=mailbox.mailbox_ref,
            mailbox_config_revision=mailbox.config_revision,
            observer_connector_instance_ref=mailbox.observer_connector_instance_ref,
            observer_delivery_ref="repo-delivery-alpha",
            received_at=now,
            participants=(
                PublicationParticipant(role="from", identity_ref=unresolved_from),
                PublicationParticipant(role="to", identity_ref=opaque_to),
            ),
            subject_projection="Private customer subject",
            subject_digest=None,
            message_id_digest=digest_a,
            in_reply_to_digest=None,
            references_digests=(),
            evidence_refs=("evidence-alpha",),
            publication_revision=1,
            idempotency_key="publication-alpha",
            payload_digest=digest_b,
        )
        publication = replace(publication, payload_digest=canonical_digest(publication.to_wire()))
        accepted = intake.accept(alpha, publication, mailbox)
        assert intake.accept(alpha, publication, mailbox) == accepted
        binding = intake.load_participant_authority_binding(
            alpha,
            inbox_item_ref=accepted.inbox_item.inbox_item_ref,
        )
        assert binding is not None
        assert binding["mailbox_config_revision"] == mailbox.config_revision
        assert binding["participant_binding_digest"] == canonical_digest(
            publication.to_wire()["participants"]
        )
        assert binding["evidence_binding_digest"] == canonical_digest(
            publication.to_wire()["evidence_refs"]
        )
        with pytest.raises(IdempotencyConflict):
            intake.accept(alpha, replace(publication, payload_digest=digest_b), mailbox)

        second_publication = replace(
            publication,
            publication_ref="repo-publication-alpha-2",
            mailbox_ref=second_mailbox.mailbox_ref,
            mailbox_config_revision=second_mailbox.config_revision,
            observer_connector_instance_ref=second_mailbox.observer_connector_instance_ref,
            observer_delivery_ref="repo-delivery-alpha-2",
            evidence_refs=("evidence-alpha-2",),
            idempotency_key="publication-alpha-2",
        )
        second_publication = replace(
            second_publication,
            payload_digest=canonical_digest(second_publication.to_wire()),
        )
        accepted_second = intake.accept(alpha, second_publication, second_mailbox)
        assert accepted_second.message.message_ref == accepted.message.message_ref
        assert accepted_second.inbox_item.inbox_item_ref != accepted.inbox_item.inbox_item_ref
        assert accepted_second.receipt.inbox_item_ref == accepted_second.inbox_item.inbox_item_ref
        second_binding = intake.load_participant_authority_binding(
            alpha,
            inbox_item_ref=accepted_second.inbox_item.inbox_item_ref,
        )
        assert second_binding is not None
        assert second_binding["evidence_binding_digest"] != binding["evidence_binding_digest"]

        third_publication = replace(
            publication,
            publication_ref="repo-publication-alpha-3",
            observer_delivery_ref="repo-delivery-alpha-3",
            message_id_digest="sha256:" + "d" * 64,
            idempotency_key="publication-alpha-3",
        )
        third_publication = replace(
            third_publication,
            payload_digest=canonical_digest(third_publication.to_wire()),
        )
        accepted_third = intake.accept(alpha, third_publication, mailbox)
        assert accepted_third.message.message_ref != accepted.message.message_ref

        too_many_participants = (
            PublicationParticipant(role="from", identity_ref=unresolved_from),
            *(
                PublicationParticipant(
                    role="to",
                    identity_ref=f"extid:v1:email:{index:043d}",
                )
                for index in range(100)
            ),
        )
        rollback_publication = replace(
            publication,
            publication_ref="repo-publication-rollback",
            observer_delivery_ref="repo-delivery-rollback",
            participants=too_many_participants,
            message_id_digest="sha256:" + "c" * 64,
            idempotency_key="publication-rollback",
        )
        rollback_publication = replace(
            rollback_publication,
            payload_digest=canonical_digest(rollback_publication.to_wire()),
        )
        with pytest.raises(ValidationError, match="persistence operation rejected"):
            intake.accept(alpha, rollback_publication, mailbox)
        connection.execute("RESET ROLE")
        rollback_counts = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM email_gateway.channel_messages "
            " WHERE site_id = %s AND message_id_digest = %s), "
            "(SELECT count(*) FROM email_gateway.publication_receipts "
            " WHERE site_id = %s AND publication_ref = %s)",
            (
                alpha.site_id,
                rollback_publication.message_id_digest,
                alpha.site_id,
                rollback_publication.publication_ref,
            ),
        ).fetchone()
        assert rollback_counts == (0, 0)
        connection.execute("SET ROLE gbos_email_gateway_app")

        projection = IdentityProjection(
            site_id=alpha.site_id,
            processing_purpose=alpha.processing_purpose,
            opaque_address_ref="extid:v1:email:" + "C" * 43,
            external_identity_ref="identity-alpha",
            external_identity_revision=2,
            identity_type="Party",
            team_ref=mailbox.default_team_ref,
            status="confirmed",
            projection_receipt_ref="projection-alpha-2",
            observed_at=now,
            payload_digest=digest_a,
        )
        assert identities.apply(alpha, projection) == projection
        assert identities.apply(alpha, projection) == projection
        assert identities.get(alpha, projection.opaque_address_ref) == projection
        assert identities.get(beta, projection.opaque_address_ref) is None
        with pytest.raises(RevisionConflict):
            identities.apply(
                alpha,
                replace(
                    projection,
                    external_identity_revision=1,
                    projection_receipt_ref="projection-alpha-1",
                ),
            )
        with pytest.raises(IdempotencyConflict):
            identities.apply(alpha, replace(projection, payload_digest=digest_b))

        assert workflow.save_inbox(alpha, accepted.inbox_item) == accepted.inbox_item
        assert workflow.get_inbox(alpha, accepted.inbox_item.inbox_item_ref) == accepted.inbox_item
        claimable = replace(
            accepted_second.inbox_item,
            state="unassigned",
            revision=accepted_second.inbox_item.revision + 1,
            updated_at=now + timedelta(seconds=1),
        )
        assert workflow.save_inbox(alpha, claimable) == claimable
        actor = GatewayActorScope(
            site_id=alpha.site_id,
            actor_ref="sales-alpha",
            team_refs=(mailbox.default_team_ref,),
            roles=("Sales User",),
        )
        poison = AuditEvent(
            audit_ref="audit-poison-inbox-operation",
            site_id=alpha.site_id,
            actor_ref="test-injector",
            event_type="injected_conflict",
            subject_ref=claimable.inbox_item_ref,
            request_id="request-poison-inbox-operation",
            idempotency_key="audit:claim-pg-rollback",
            payload_digest=digest_a,
            occurred_at=now + timedelta(seconds=2),
        )
        workflow.append_audit(alpha, poison)
        operations = InboxOperations(workflow)
        rollback_audit_count = workflow.audit_count(alpha)
        with pytest.raises(IdempotencyConflict, match="audit"):
            operations.claim(
                alpha,
                actor=actor,
                actor_enabled=True,
                inbox_item_ref=claimable.inbox_item_ref,
                expected_revision=claimable.revision,
                request_id="REQ-CLAIM-PG-ROLLBACK",
                idempotency_key="claim-pg-rollback",
                now=now + timedelta(seconds=3),
            )
        assert workflow.get_inbox(alpha, claimable.inbox_item_ref) == claimable
        assert workflow.audit_count(alpha) == rollback_audit_count
        assert workflow.replay(alpha, "claim-pg-rollback", digest_a) is None

        claim_command = dict(
            actor=actor,
            actor_enabled=True,
            inbox_item_ref=claimable.inbox_item_ref,
            expected_revision=claimable.revision,
            request_id="REQ-CLAIM-PG-LOSS",
            idempotency_key="claim-pg-loss",
            now=now + timedelta(seconds=4),
        )
        claimed = operations.claim(alpha, **claim_command)
        replayed_claim = operations.claim(alpha, **claim_command)
        assert replayed_claim == claimed
        assert claimed.revision == claimable.revision + 1
        assert workflow.audit_count(alpha) == rollback_audit_count + 1
        suggestion = ThreadSuggestion(
            suggestion_ref="suggestion-alpha",
            site_id=alpha.site_id,
            team_ref=mailbox.default_team_ref,
            left_inbox_ref=accepted.inbox_item.inbox_item_ref,
            right_inbox_ref=accepted_second.inbox_item.inbox_item_ref,
            signals=("message_id_family",),
            confidence=0.9,
            status="proposed",
            revision=1,
            reviewed_by=None,
            reviewed_at=None,
            created_at=now,
        )
        assert workflow.save_suggestion(alpha, suggestion) == suggestion
        assert workflow.get_suggestion(alpha, suggestion.suggestion_ref) == suggestion
        conversation = Conversation(
            conversation_ref="conversation-alpha",
            site_id=alpha.site_id,
            team_ref=mailbox.default_team_ref,
            party_ref=None,
            contact_ref=None,
            owner_user_ref=None,
            lifecycle_state="open",
            first_message_at=now,
            last_message_at=now,
            message_refs=(accepted.message.message_ref, accepted_third.message.message_ref),
            inbox_item_refs=(
                accepted.inbox_item.inbox_item_ref,
                accepted_third.inbox_item.inbox_item_ref,
            ),
            revision=1,
        )
        assert workflow.save_conversation(alpha, conversation) == conversation
        assert (
            workflow.get_conversation_for(alpha, accepted.inbox_item.inbox_item_ref) == conversation
        )
        workflow.remember(alpha, "workflow-conversation", digest_a, conversation)
        assert workflow.replay(alpha, "workflow-conversation", digest_a) == conversation
        with pytest.raises(IdempotencyConflict):
            workflow.replay(alpha, "workflow-conversation", digest_b)
        split = ConversationService(workflow).split(
            alpha,
            actor=GatewayActorScope(
                site_id=alpha.site_id,
                actor_ref="manager-alpha",
                team_refs=(mailbox.default_team_ref,),
                roles=("Sales Manager",),
            ),
            conversation=conversation,
            moved_inbox_refs=(accepted_third.inbox_item.inbox_item_ref,),
            expected_revision=conversation.revision,
            request_id="REQ-SPLIT-PG",
            idempotency_key="split-pg",
            now=now + timedelta(seconds=5),
        )
        replayed_split = ConversationService(workflow).split(
            alpha,
            actor=GatewayActorScope(
                site_id=alpha.site_id,
                actor_ref="manager-alpha",
                team_refs=(mailbox.default_team_ref,),
                roles=("Sales Manager",),
            ),
            conversation=conversation,
            moved_inbox_refs=(accepted_third.inbox_item.inbox_item_ref,),
            expected_revision=conversation.revision,
            request_id="REQ-SPLIT-PG",
            idempotency_key="split-pg",
            now=now + timedelta(seconds=5),
        )
        assert replayed_split == split
        source_revised = workflow.get_conversation(alpha, conversation.conversation_ref)
        assert source_revised is not None
        assert source_revised.inbox_item_refs == (accepted.inbox_item.inbox_item_ref,)
        assert source_revised.revision == conversation.revision + 1
        assert (
            workflow.get_conversation_for(alpha, accepted.inbox_item.inbox_item_ref)
            == source_revised
        )
        assert (
            workflow.get_conversation_for(alpha, accepted_third.inbox_item.inbox_item_ref) == split
        )

        draft = Draft(
            draft_ref="draft-alpha",
            site_id=alpha.site_id,
            inbox_item_ref=accepted.inbox_item.inbox_item_ref,
            conversation_ref=conversation.conversation_ref,
            content_evidence_ref="evidence-draft-alpha",
            content_digest=digest_a,
            revision=1,
            state="editable",
            updated_at=now,
        )
        assert (
            workflow.save_draft(
                alpha,
                draft,
                idempotency_key="draft-create",
                payload_digest=digest_a,
            )
            == draft
        )
        assert (
            workflow.save_draft(
                alpha,
                draft,
                idempotency_key="draft-create",
                payload_digest=digest_a,
            )
            == draft
        )
        assert workflow.get_draft(alpha, draft.draft_ref) == draft
        with pytest.raises(IdempotencyConflict):
            workflow.save_draft(
                alpha,
                draft,
                idempotency_key="draft-create",
                payload_digest=digest_b,
            )

        audit_count_before_manual = workflow.audit_count(alpha)
        event = AuditEvent(
            audit_ref="audit-alpha",
            site_id=alpha.site_id,
            actor_ref="actor-alpha",
            event_type="repository_verified",
            subject_ref="subject-alpha",
            request_id="request-audit-alpha",
            idempotency_key="audit-alpha",
            payload_digest=digest_a,
            occurred_at=now,
        )
        assert audits.append(alpha, event) == event
        assert audits.append(alpha, event) == event
        assert workflow.append_audit(
            alpha,
            replace(
                event,
                audit_ref="audit-alpha-2",
                idempotency_key="audit-alpha-2",
            ),
        )
        with pytest.raises(IdempotencyConflict):
            audits.append(alpha, replace(event, payload_digest=digest_b))
        assert workflow.audit_count(alpha) == audit_count_before_manual + 2

        connection.execute("RESET ROLE")
        connection.execute("SET ROLE gbos_email_gateway_worker")
        config_outbox = PostgresMailboxConfigOutboxRepository(connection)
        lease_now = datetime.now(UTC) + timedelta(seconds=1)
        claim = config_outbox.claim(
            alpha,
            worker_id="config-worker-alpha",
            now=lease_now,
            lease_duration=timedelta(minutes=1),
        )
        assert claim is not None
        assert claim.site_id == alpha.site_id
        assert claim.mailbox_ref in {mailbox.mailbox_ref, second_mailbox.mailbox_ref}
        assert claim.activation_not_before.tzinfo is not None
        assert "private-alpha" not in repr(claim)
        projection_wire = claim.to_connector_projection_wire()
        assert set(projection_wire) == {
            "site_id",
            "observer_connector_instance_ref",
            "provider_kind",
            "entry_role",
            "business_purpose",
            "team_ref",
            "credential_ref",
            "inbound_enabled",
            "activation_watermark",
            "projection_revision",
            "projection_digest",
        }
        config_outbox.heartbeat(
            alpha,
            claim.config_publication_ref,
            worker_id=claim.lease_owner,
            expected_attempt=claim.attempt,
            fence_token=claim.fence_token,
            now=lease_now + timedelta(seconds=10),
            lease_duration=timedelta(minutes=1),
        )
        outcome = config_outbox.mark_failed(
            alpha,
            claim.config_publication_ref,
            worker_id=claim.lease_owner,
            expected_attempt=claim.attempt,
            fence_token=claim.fence_token,
            now=lease_now + timedelta(seconds=20),
            retry_at=lease_now + timedelta(minutes=2),
            error_code="observer_unavailable",
        )
        assert outcome == "retry"
        reclaimed = config_outbox.claim(
            alpha,
            worker_id="config-worker-alpha",
            now=lease_now + timedelta(minutes=2),
            lease_duration=timedelta(minutes=1),
        )
        assert reclaimed is not None
        if reclaimed.config_publication_ref != claim.config_publication_ref:
            config_outbox.mark_delivered(
                alpha,
                reclaimed.config_publication_ref,
                worker_id=reclaimed.lease_owner,
                expected_attempt=reclaimed.attempt,
                fence_token=reclaimed.fence_token,
                receipt_ref="observer-config-receipt-other",
                now=lease_now + timedelta(minutes=2, seconds=1),
            )
            reclaimed = config_outbox.claim(
                alpha,
                worker_id="config-worker-alpha",
                now=lease_now + timedelta(minutes=2, seconds=2),
                lease_duration=timedelta(minutes=1),
            )
            assert reclaimed is not None
        assert reclaimed.config_publication_ref == claim.config_publication_ref
        assert reclaimed.activation_not_before == claim.activation_not_before
        assert reclaimed.to_connector_projection_wire() == projection_wire
        config_outbox.mark_delivered(
            alpha,
            reclaimed.config_publication_ref,
            worker_id=reclaimed.lease_owner,
            expected_attempt=reclaimed.attempt,
            fence_token=reclaimed.fence_token,
            receipt_ref="observer-config-receipt-alpha",
            now=lease_now + timedelta(minutes=2, seconds=5),
        )
        config_outbox.mark_delivered(
            alpha,
            reclaimed.config_publication_ref,
            worker_id=reclaimed.lease_owner,
            expected_attempt=reclaimed.attempt,
            fence_token=reclaimed.fence_token,
            receipt_ref="observer-config-receipt-alpha",
            now=lease_now + timedelta(minutes=2, seconds=6),
        )

        connection.execute("RESET ROLE")
        watermarks = connection.execute(
            "SELECT mailbox_config_revision, activation_not_before "
            "FROM email_gateway.mailbox_config_outbox "
            "WHERE site_id = %s AND mailbox_ref = %s "
            "ORDER BY mailbox_config_revision",
            (alpha.site_id, mailbox.mailbox_ref),
        ).fetchall()
        assert len(watermarks) == 2
        assert watermarks[0][1].tzinfo is not None
        assert watermarks[1][1].tzinfo is not None
        assert watermarks[0][1] != watermarks[1][1]
        ciphertext = connection.execute(
            "SELECT address_display_ciphertext FROM email_gateway.mailboxes "
            "WHERE site_id = %s AND mailbox_ref = %s",
            (alpha.site_id, mailbox.mailbox_ref),
        ).fetchone()[0]
        subject_ciphertext = connection.execute(
            "SELECT subject_projection_ciphertext FROM email_gateway.channel_messages "
            "WHERE site_id = %s AND message_ref = %s",
            (alpha.site_id, accepted.message.message_ref),
        ).fetchone()[0]
        assert b"private-alpha@example.invalid" not in bytes(ciphertext)
        assert b"Private customer subject" not in bytes(subject_ciphertext)
        counts = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM email_gateway.publication_receipts WHERE site_id = %s), "
            "(SELECT count(*) FROM email_gateway.channel_messages WHERE site_id = %s), "
            "(SELECT count(*) FROM email_gateway.inbox_items WHERE site_id = %s)",
            (alpha.site_id, alpha.site_id, alpha.site_id),
        ).fetchone()
        assert counts == (3, 2, 3)
