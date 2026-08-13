CREATE TABLE IF NOT EXISTS email_gateway.reply_drafts (
    site_id text NOT NULL,
    draft_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    conversation_ref text,
    content_evidence_ref text NOT NULL,
    content_digest text NOT NULL,
    state text NOT NULL DEFAULT 'editable',
    revision bigint NOT NULL DEFAULT 1,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, draft_ref),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, conversation_ref)
        REFERENCES email_gateway.conversations (site_id, conversation_ref),
    CHECK (content_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (state IN ('editable', 'discarded', 'terminal')),
    CHECK (revision >= 1),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS email_gateway.send_outbox (
    site_id text NOT NULL,
    send_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    conversation_ref text,
    draft_ref text NOT NULL,
    approved_command_ref text NOT NULL,
    approved_payload_digest text NOT NULL,
    final_mime_evidence_ref text NOT NULL,
    final_mime_digest text NOT NULL,
    state text NOT NULL DEFAULT 'disabled',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, send_ref),
    UNIQUE (site_id, approved_command_ref),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    FOREIGN KEY (site_id, conversation_ref)
        REFERENCES email_gateway.conversations (site_id, conversation_ref),
    FOREIGN KEY (site_id, draft_ref)
        REFERENCES email_gateway.reply_drafts (site_id, draft_ref),
    CHECK (approved_payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (final_mime_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (state = 'disabled')
);

DROP TRIGGER IF EXISTS send_outbox_immutable ON email_gateway.send_outbox;
CREATE TRIGGER send_outbox_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.send_outbox
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.reply_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.reply_drafts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.reply_drafts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.reply_drafts;
CREATE POLICY email_gateway_site_scope ON email_gateway.reply_drafts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.send_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.send_outbox FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.send_outbox FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.send_outbox;
CREATE POLICY email_gateway_site_scope ON email_gateway.send_outbox
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT, INSERT, UPDATE ON email_gateway.reply_drafts TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.send_outbox TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.reply_drafts TO gbos_email_gateway_worker;
GRANT SELECT ON email_gateway.send_outbox TO gbos_email_gateway_worker;
