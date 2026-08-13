-- Approved outbound remains provider-neutral and default closed.  This migration
-- stores only opaque bindings and digests; final MIME bytes and raw addresses
-- remain outside the Gateway database.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_email_command_executor') THEN
        CREATE ROLE gbos_email_command_executor NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_email_send_worker') THEN
        CREATE ROLE gbos_email_send_worker NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA email_gateway TO gbos_email_command_executor;
GRANT USAGE ON SCHEMA email_gateway TO gbos_email_send_worker;

CREATE TABLE IF NOT EXISTS email_gateway.command_inbox (
    site_id text NOT NULL,
    processing_purpose text NOT NULL,
    command_receipt_ref text NOT NULL,
    publication_ref text NOT NULL,
    publication_attempt integer NOT NULL,
    publication_generation bigint NOT NULL,
    publication_fence_token text NOT NULL,
    command_ref text NOT NULL,
    idempotency_key text NOT NULL,
    stable_client_request_id text NOT NULL,
    payload_digest text NOT NULL,
    approved_envelope jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, command_receipt_ref),
    UNIQUE (site_id, publication_ref),
    UNIQUE (site_id, command_ref),
    UNIQUE (site_id, idempotency_key),
    CHECK (publication_attempt BETWEEN 1 AND 5),
    CHECK (publication_generation >= 1),
    CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    CHECK (jsonb_typeof(approved_envelope) = 'object')
);

ALTER TABLE email_gateway.send_outbox
    ADD COLUMN IF NOT EXISTS processing_purpose text,
    ADD COLUMN IF NOT EXISTS command_receipt_ref text,
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS stable_client_request_id text,
    ADD COLUMN IF NOT EXISTS review_case_ref text,
    ADD COLUMN IF NOT EXISTS review_case_revision bigint,
    ADD COLUMN IF NOT EXISTS approval_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS approved_envelope jsonb;

ALTER TABLE email_gateway.send_outbox
    DROP CONSTRAINT IF EXISTS send_outbox_state_check;
ALTER TABLE email_gateway.send_outbox ALTER COLUMN state SET DEFAULT 'queued';
ALTER TABLE email_gateway.send_outbox
    ADD CONSTRAINT send_outbox_state_check CHECK (state IN ('disabled', 'queued')) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS send_outbox_command_receipt_unique
    ON email_gateway.send_outbox (site_id, command_receipt_ref)
    WHERE command_receipt_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS send_outbox_idempotency_unique
    ON email_gateway.send_outbox (site_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS email_gateway.send_outbox_state (
    site_id text NOT NULL,
    send_outbox_ref text NOT NULL,
    state text NOT NULL,
    attempt integer NOT NULL DEFAULT 0,
    generation bigint NOT NULL DEFAULT 0,
    lease_owner text,
    fence_token text,
    lease_expires_at timestamptz,
    safe_code text,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, send_outbox_ref),
    FOREIGN KEY (site_id, send_outbox_ref)
        REFERENCES email_gateway.send_outbox (site_id, send_ref),
    CHECK (state IN (
        'queued', 'leased', 'provider_accepted', 'delivered', 'bounced',
        'provider_rejected', 'reconciliation_required', 'authority_review_required'
    )),
    CHECK (attempt >= 0),
    CHECK (generation >= 0),
    CHECK (
        (state = 'leased' AND lease_owner IS NOT NULL
            AND fence_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (state <> 'leased' AND lease_owner IS NULL
            AND fence_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS email_gateway.send_attempts (
    site_id text NOT NULL,
    attempt_event_ref text NOT NULL,
    send_outbox_ref text NOT NULL,
    attempt integer NOT NULL,
    generation bigint NOT NULL,
    fence_token text NOT NULL,
    stable_provider_request_id text NOT NULL,
    event_kind text NOT NULL,
    occurred_at timestamptz NOT NULL,
    provider_request_digest text NOT NULL,
    safe_code text,
    PRIMARY KEY (site_id, attempt_event_ref),
    FOREIGN KEY (site_id, send_outbox_ref)
        REFERENCES email_gateway.send_outbox (site_id, send_ref),
    CHECK (attempt >= 1),
    CHECK (generation >= 1),
    CHECK (event_kind IN ('started', 'completed', 'uncertain', 'authority_rejected')),
    CHECK (provider_request_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.provider_receipts (
    site_id text NOT NULL,
    provider_receipt_record_ref text NOT NULL,
    send_outbox_ref text NOT NULL,
    attempt integer NOT NULL,
    outcome text NOT NULL,
    safe_code text NOT NULL,
    provider_receipt_ref text,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, provider_receipt_record_ref),
    FOREIGN KEY (site_id, send_outbox_ref)
        REFERENCES email_gateway.send_outbox (site_id, send_ref),
    CHECK (attempt >= 1),
    CHECK (outcome IN ('accepted', 'delivered', 'bounced', 'permanently_rejected'))
);

CREATE TABLE IF NOT EXISTS email_gateway.reconciliation_receipts (
    site_id text NOT NULL,
    reconciliation_receipt_ref text NOT NULL,
    send_outbox_ref text NOT NULL,
    stable_provider_request_id text NOT NULL,
    lookup_outcome text NOT NULL,
    observed_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, reconciliation_receipt_ref),
    FOREIGN KEY (site_id, send_outbox_ref)
        REFERENCES email_gateway.send_outbox (site_id, send_ref),
    CHECK (lookup_outcome IN (
        'not_submitted', 'accepted', 'delivered', 'bounced',
        'permanently_rejected', 'unknown'
    ))
);

DROP TRIGGER IF EXISTS command_inbox_immutable ON email_gateway.command_inbox;
CREATE TRIGGER command_inbox_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.command_inbox
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS send_attempts_immutable ON email_gateway.send_attempts;
CREATE TRIGGER send_attempts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.send_attempts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS provider_receipts_immutable ON email_gateway.provider_receipts;
CREATE TRIGGER provider_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.provider_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS reconciliation_receipts_immutable
    ON email_gateway.reconciliation_receipts;
CREATE TRIGGER reconciliation_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.reconciliation_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.command_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.command_inbox FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.command_inbox FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.command_inbox;
CREATE POLICY email_gateway_site_scope ON email_gateway.command_inbox
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.send_outbox_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.send_outbox_state FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.send_outbox_state FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.send_outbox_state;
CREATE POLICY email_gateway_site_scope ON email_gateway.send_outbox_state
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.send_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.send_attempts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.send_attempts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.send_attempts;
CREATE POLICY email_gateway_site_scope ON email_gateway.send_attempts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.provider_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.provider_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.provider_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.provider_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.provider_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.reconciliation_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.reconciliation_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.reconciliation_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.reconciliation_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.reconciliation_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

REVOKE INSERT, UPDATE, DELETE ON email_gateway.send_outbox
    FROM gbos_email_gateway_app, gbos_email_gateway_worker, gbos_email_send_worker;
GRANT SELECT ON email_gateway.command_inbox, email_gateway.send_outbox,
    email_gateway.send_outbox_state TO gbos_email_command_executor;
GRANT INSERT ON email_gateway.command_inbox TO gbos_email_command_executor;
GRANT INSERT ON email_gateway.send_outbox TO gbos_email_command_executor;
GRANT INSERT ON email_gateway.send_outbox_state TO gbos_email_command_executor;

GRANT SELECT ON email_gateway.send_outbox, email_gateway.send_outbox_state,
    email_gateway.send_attempts, email_gateway.provider_receipts,
    email_gateway.reconciliation_receipts TO gbos_email_send_worker;
GRANT UPDATE ON email_gateway.send_outbox_state TO gbos_email_send_worker;
GRANT INSERT ON email_gateway.send_attempts TO gbos_email_send_worker;
GRANT INSERT ON email_gateway.provider_receipts TO gbos_email_send_worker;
GRANT INSERT ON email_gateway.reconciliation_receipts TO gbos_email_send_worker;

GRANT SELECT ON email_gateway.command_inbox, email_gateway.send_outbox_state,
    email_gateway.send_attempts, email_gateway.provider_receipts,
    email_gateway.reconciliation_receipts TO gbos_email_gateway_app;
