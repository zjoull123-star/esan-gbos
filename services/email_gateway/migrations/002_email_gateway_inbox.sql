CREATE TABLE IF NOT EXISTS email_gateway.channel_messages (
    site_id text NOT NULL,
    message_ref text NOT NULL,
    direction text NOT NULL,
    received_at timestamptz NOT NULL,
    subject_projection_ciphertext bytea,
    subject_digest text NOT NULL,
    message_id_digest text NOT NULL,
    in_reply_to_digest text,
    references_digests text[] NOT NULL DEFAULT '{}',
    evidence_refs text[] NOT NULL,
    provider text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, message_ref),
    UNIQUE (site_id, message_id_digest),
    CHECK (direction = 'inbound'),
    CHECK (subject_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (message_id_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (in_reply_to_digest IS NULL OR in_reply_to_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (provider IN ('fake', 'imap_smtp', 'wecom_app_mail')),
    CHECK (revision >= 1),
    CHECK (cardinality(evidence_refs) BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS email_gateway.message_participants (
    site_id text NOT NULL,
    message_ref text NOT NULL,
    role text NOT NULL,
    identity_ref text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (site_id, message_ref, role, identity_ref),
    UNIQUE (site_id, message_ref, ordinal),
    FOREIGN KEY (site_id, message_ref)
        REFERENCES email_gateway.channel_messages (site_id, message_ref),
    CHECK (role IN ('from', 'to', 'cc', 'bcc')),
    CHECK (
        identity_ref ~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'
        OR identity_ref ~ '^unresolved:delivery:[0-9A-HJKMNP-TV-Z]{26}$'
    ),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS email_gateway.inbox_items (
    site_id text NOT NULL,
    inbox_item_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    message_ref text NOT NULL,
    team_ref text NOT NULL,
    assignee_user_ref text,
    priority integer NOT NULL DEFAULT 0,
    sla_due_at timestamptz,
    state text NOT NULL DEFAULT 'identity_pending',
    conversation_ref text,
    business_links text[] NOT NULL DEFAULT '{}',
    revision bigint NOT NULL DEFAULT 1,
    received_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, inbox_item_ref),
    UNIQUE (site_id, mailbox_ref, message_ref),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    FOREIGN KEY (site_id, message_ref)
        REFERENCES email_gateway.channel_messages (site_id, message_ref),
    CHECK (state IN (
        'identity_pending', 'unassigned', 'assigned', 'draft', 'waiting_internal',
        'waiting_customer', 'converted', 'closed', 'quarantined',
        'send_queued', 'send_uncertain'
    )),
    CHECK (priority BETWEEN 0 AND 1000),
    CHECK (revision >= 1),
    CHECK (updated_at >= received_at)
);

CREATE TABLE IF NOT EXISTS email_gateway.publication_receipts (
    site_id text NOT NULL,
    receipt_ref text NOT NULL,
    publication_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    observer_delivery_ref text NOT NULL,
    message_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    payload_digest text NOT NULL,
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, receipt_ref),
    UNIQUE (site_id, publication_ref),
    UNIQUE (site_id, mailbox_ref, observer_delivery_ref),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    FOREIGN KEY (site_id, message_ref)
        REFERENCES email_gateway.channel_messages (site_id, message_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.route_decisions (
    site_id text NOT NULL,
    decision_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    route_status text NOT NULL,
    team_ref text NOT NULL,
    party_ref text,
    party_revision bigint,
    owner_user_ref text,
    owner_eligibility_revision text,
    safe_reason_code text,
    decided_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, decision_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    CHECK (route_status IN ('assigned', 'unassigned')),
    CHECK (party_revision IS NULL OR party_revision >= 1),
    CHECK (owner_eligibility_revision IS NULL OR owner_eligibility_revision ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.conversations (
    site_id text NOT NULL,
    conversation_ref text NOT NULL,
    team_ref text NOT NULL,
    party_ref text,
    contact_ref text,
    owner_user_ref text,
    lifecycle_state text NOT NULL,
    first_message_at timestamptz NOT NULL,
    last_message_at timestamptz NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, conversation_ref),
    CHECK (lifecycle_state IN ('open', 'closed')),
    CHECK (revision >= 1),
    CHECK (last_message_at >= first_message_at),
    CHECK (updated_at >= created_at)
);

ALTER TABLE email_gateway.inbox_items
    DROP CONSTRAINT IF EXISTS inbox_items_conversation_fk;
ALTER TABLE email_gateway.inbox_items
    ADD CONSTRAINT inbox_items_conversation_fk
    FOREIGN KEY (site_id, conversation_ref)
    REFERENCES email_gateway.conversations (site_id, conversation_ref)
    NOT VALID;

CREATE TABLE IF NOT EXISTS email_gateway.conversation_messages (
    site_id text NOT NULL,
    conversation_ref text NOT NULL,
    message_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (site_id, conversation_ref, message_ref, inbox_item_ref),
    UNIQUE (site_id, conversation_ref, ordinal),
    FOREIGN KEY (site_id, conversation_ref)
        REFERENCES email_gateway.conversations (site_id, conversation_ref),
    FOREIGN KEY (site_id, message_ref)
        REFERENCES email_gateway.channel_messages (site_id, message_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (ordinal BETWEEN 1 AND 10000)
);

CREATE TABLE IF NOT EXISTS email_gateway.thread_suggestions (
    site_id text NOT NULL,
    suggestion_ref text NOT NULL,
    team_ref text NOT NULL,
    left_inbox_ref text NOT NULL,
    right_inbox_ref text NOT NULL,
    signals text[] NOT NULL,
    confidence double precision NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    revision bigint NOT NULL DEFAULT 1,
    reviewed_by text,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, suggestion_ref),
    FOREIGN KEY (site_id, left_inbox_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, right_inbox_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (left_inbox_ref <> right_inbox_ref),
    CHECK (cardinality(signals) BETWEEN 1 AND 20),
    CHECK (confidence BETWEEN 0 AND 1),
    CHECK (status IN ('proposed', 'accepted', 'rejected', 'expired')),
    CHECK (revision >= 1)
);

DROP TRIGGER IF EXISTS publication_receipts_immutable ON email_gateway.publication_receipts;
CREATE TRIGGER publication_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.publication_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS channel_messages_immutable ON email_gateway.channel_messages;
CREATE TRIGGER channel_messages_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.channel_messages
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS message_participants_immutable ON email_gateway.message_participants;
CREATE TRIGGER message_participants_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.message_participants
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS route_decisions_immutable ON email_gateway.route_decisions;
CREATE TRIGGER route_decisions_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.route_decisions
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.channel_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.channel_messages FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.channel_messages FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.channel_messages;
CREATE POLICY email_gateway_site_scope ON email_gateway.channel_messages
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.message_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.message_participants FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.message_participants FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.message_participants;
CREATE POLICY email_gateway_site_scope ON email_gateway.message_participants
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.inbox_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.inbox_items FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.inbox_items FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.inbox_items;
CREATE POLICY email_gateway_site_scope ON email_gateway.inbox_items
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.publication_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.publication_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.publication_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.publication_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.publication_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.route_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.route_decisions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.route_decisions FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.route_decisions;
CREATE POLICY email_gateway_site_scope ON email_gateway.route_decisions
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.conversations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.conversations FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.conversations;
CREATE POLICY email_gateway_site_scope ON email_gateway.conversations
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.conversation_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.conversation_messages FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.conversation_messages FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.conversation_messages;
CREATE POLICY email_gateway_site_scope ON email_gateway.conversation_messages
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.thread_suggestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.thread_suggestions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.thread_suggestions FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.thread_suggestions;
CREATE POLICY email_gateway_site_scope ON email_gateway.thread_suggestions
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT, INSERT ON email_gateway.channel_messages TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.message_participants TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.inbox_items TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.publication_receipts TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.route_decisions TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.conversations TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.conversation_messages TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.thread_suggestions TO gbos_email_gateway_app;
