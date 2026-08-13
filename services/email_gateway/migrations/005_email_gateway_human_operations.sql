CREATE TABLE IF NOT EXISTS email_gateway.mailbox_sla_policies (
    site_id text NOT NULL,
    mailbox_ref text NOT NULL,
    policy_ref text NOT NULL,
    revision bigint NOT NULL,
    first_response_duration_seconds integer NOT NULL,
    effective_at timestamptz NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, policy_ref, revision),
    UNIQUE (site_id, mailbox_ref, revision),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    CHECK (policy_ref ~ '^SLA-[0-9A-HJKMNP-TV-Z]{26}$'),
    CHECK (revision >= 1),
    CHECK (first_response_duration_seconds BETWEEN 60 AND 604800),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.inbox_sla_clocks (
    site_id text NOT NULL,
    inbox_item_ref text NOT NULL,
    policy_ref text NOT NULL,
    policy_revision bigint NOT NULL,
    started_at timestamptz,
    due_at timestamptz,
    status text NOT NULL,
    completed_at timestamptz,
    provider_accepted_receipt_ref text,
    closed_at timestamptz,
    closed_outcome text,
    audit_revision bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, policy_ref, policy_revision)
        REFERENCES email_gateway.mailbox_sla_policies (site_id, policy_ref, revision),
    CHECK (policy_revision >= 1),
    CHECK (status IN (
        'running', 'met', 'overdue', 'closed_met', 'closed_overdue', 'not_applicable'
    )),
    CHECK (closed_outcome IS NULL OR closed_outcome IN ('met', 'overdue')),
    CHECK (audit_revision >= 1),
    CHECK (
        (status = 'not_applicable' AND started_at IS NULL AND due_at IS NULL)
        OR (status <> 'not_applicable' AND started_at IS NOT NULL AND due_at IS NOT NULL)
    ),
    CHECK (due_at IS NULL OR due_at >= started_at),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    CHECK (closed_at IS NULL OR closed_at >= started_at),
    CHECK (
        (completed_at IS NULL AND provider_accepted_receipt_ref IS NULL)
        OR (completed_at IS NOT NULL AND provider_accepted_receipt_ref IS NOT NULL)
    ),
    CHECK (
        (closed_at IS NULL AND closed_outcome IS NULL)
        OR (closed_at IS NOT NULL AND closed_outcome IS NOT NULL)
    ),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS email_gateway.inbox_operation_requests (
    site_id text NOT NULL,
    operation_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    actor_ref text NOT NULL,
    actor_kind text NOT NULL,
    operation_type text NOT NULL,
    expected_revision bigint NOT NULL,
    result_revision bigint NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, operation_ref),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (actor_kind IN ('human', 'identity_worker', 'routing_worker')),
    CHECK (operation_type IN (
        'claim', 'reassign', 'transition', 'reopen', 'identity_route',
        'link_business', 'conversation_merge', 'conversation_split'
    )),
    CHECK (expected_revision >= 1),
    CHECK (result_revision = expected_revision + 1),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

DROP TRIGGER IF EXISTS mailbox_sla_policies_immutable
    ON email_gateway.mailbox_sla_policies;
CREATE TRIGGER mailbox_sla_policies_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.mailbox_sla_policies
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

DROP TRIGGER IF EXISTS inbox_operation_requests_immutable
    ON email_gateway.inbox_operation_requests;
CREATE TRIGGER inbox_operation_requests_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.inbox_operation_requests
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.mailbox_sla_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.mailbox_sla_policies FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.mailbox_sla_policies FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.mailbox_sla_policies;
CREATE POLICY email_gateway_site_scope ON email_gateway.mailbox_sla_policies
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.inbox_sla_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.inbox_sla_clocks FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.inbox_sla_clocks FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.inbox_sla_clocks;
CREATE POLICY email_gateway_site_scope ON email_gateway.inbox_sla_clocks
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.inbox_operation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.inbox_operation_requests FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.inbox_operation_requests FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.inbox_operation_requests;
CREATE POLICY email_gateway_site_scope ON email_gateway.inbox_operation_requests
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT, INSERT ON email_gateway.mailbox_sla_policies TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.inbox_sla_clocks TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.inbox_operation_requests TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.mailbox_sla_policies TO gbos_email_gateway_worker;
GRANT SELECT, INSERT, UPDATE ON email_gateway.inbox_sla_clocks TO gbos_email_gateway_worker;
GRANT SELECT, INSERT ON email_gateway.inbox_operation_requests TO gbos_email_gateway_worker;

-- Conversation membership is otherwise immutable to the application role. A
-- split needs one narrowly scoped removal after the source Conversation CAS
-- update and before the same transaction inserts the revised memberships.
-- Keep DELETE off the application role and bind the command to the transaction
-- site/purpose plus the already-persisted expected source revision.
CREATE OR REPLACE FUNCTION email_gateway.clear_conversation_members_for_split(
    p_site_id text,
    p_processing_purpose text,
    p_conversation_ref text,
    p_expected_source_revision bigint,
    p_expected_member_count integer
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_deleted integer;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_processing_purpose IS DISTINCT FROM current_setting(
           'gbos.processing_purpose', true
       )
       OR p_expected_source_revision < 2
       OR p_expected_member_count < 2 THEN
        RAISE EXCEPTION 'conversation split scope rejected';
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM email_gateway.conversations AS conversation
         WHERE conversation.site_id = p_site_id
           AND conversation.conversation_ref = p_conversation_ref
           AND conversation.revision = p_expected_source_revision
    ) THEN
        RAISE EXCEPTION 'conversation split revision rejected';
    END IF;

    IF (
        SELECT count(*)
          FROM email_gateway.conversation_messages AS member
          JOIN email_gateway.inbox_items AS inbox
            ON inbox.site_id = member.site_id
           AND inbox.inbox_item_ref = member.inbox_item_ref
          JOIN email_gateway.mailboxes AS mailbox
            ON mailbox.site_id = inbox.site_id
           AND mailbox.mailbox_ref = inbox.mailbox_ref
         WHERE member.site_id = p_site_id
           AND member.conversation_ref = p_conversation_ref
           AND mailbox.business_purpose = p_processing_purpose
    ) IS DISTINCT FROM p_expected_member_count::bigint THEN
        RAISE EXCEPTION 'conversation split membership rejected';
    END IF;

    DELETE FROM email_gateway.conversation_messages AS member
     WHERE member.site_id = p_site_id
       AND member.conversation_ref = p_conversation_ref;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    IF v_deleted <> p_expected_member_count THEN
        RAISE EXCEPTION 'conversation split membership rejected';
    END IF;
    RETURN v_deleted;
END
$$;

REVOKE ALL ON FUNCTION email_gateway.clear_conversation_members_for_split(
    text, text, text, bigint, integer
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION email_gateway.clear_conversation_members_for_split(
    text, text, text, bigint, integer
) TO gbos_email_gateway_app;
