CREATE SCHEMA IF NOT EXISTS email_gateway;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_email_gateway_app') THEN
        CREATE ROLE gbos_email_gateway_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_email_gateway_worker') THEN
        CREATE ROLE gbos_email_gateway_worker NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON SCHEMA email_gateway FROM PUBLIC;
GRANT USAGE ON SCHEMA email_gateway TO gbos_email_gateway_app;
GRANT USAGE ON SCHEMA email_gateway TO gbos_email_gateway_worker;

CREATE TABLE IF NOT EXISTS email_gateway.schema_migrations (
    migration_name text PRIMARY KEY,
    checksum_sha256 char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    CHECK (migration_name ~ '^[0-9]{3}_[a-z0-9_]+[.]sql$'),
    CHECK (checksum_sha256 ~ '^[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.mailboxes (
    site_id text NOT NULL,
    mailbox_ref text NOT NULL,
    address_display_ciphertext bytea NOT NULL,
    provider text NOT NULL,
    provider_account_ref text NOT NULL,
    observer_connector_instance_ref text NOT NULL,
    entry_role text NOT NULL,
    business_purpose text NOT NULL,
    default_team_ref text NOT NULL,
    account_owner_user_ref text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    inbound_enabled boolean NOT NULL DEFAULT false,
    outbound_enabled boolean NOT NULL DEFAULT false,
    credential_ref text NOT NULL,
    status text NOT NULL DEFAULT 'draft',
    config_revision bigint NOT NULL DEFAULT 1,
    observer_config_projection_receipt text,
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, mailbox_ref),
    UNIQUE (site_id, observer_connector_instance_ref),
    CHECK (site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$'),
    CHECK (length(address_display_ciphertext) BETWEEN 1 AND 2048),
    CHECK (provider IN ('fake', 'imap_smtp', 'wecom_app_mail')),
    CHECK (entry_role IN ('primary', 'workflow', 'migration', 'selective_archive')),
    CHECK (business_purpose IN (
        'business_operations', 'observation_processing', 'entity_resolution',
        'customer_service', 'sales_follow_up', 'procurement_coordination',
        'product_sample_management', 'risk_review', 'metric_reporting', 'audit_compliance'
    )),
    CHECK (priority BETWEEN 0 AND 1000),
    CHECK (status IN ('draft', 'active', 'paused', 'revoked', 'error')),
    CHECK (config_revision >= 1),
    CHECK (length(credential_ref) BETWEEN 1 AND 80),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS email_gateway.mailbox_config_outbox (
    site_id text NOT NULL,
    config_publication_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    mailbox_config_revision bigint NOT NULL,
    processing_purpose text NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    activation_not_before timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL DEFAULT 'queued',
    attempt integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    safe_error_code text,
    receipt_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, config_publication_ref),
    UNIQUE (site_id, mailbox_ref, mailbox_config_revision),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    CHECK (mailbox_config_revision >= 1),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (status IN ('queued', 'leased', 'retry', 'delivered', 'dead_letter')),
    CHECK (attempt BETWEEN 0 AND 5),
    CHECK (lease_generation >= 0),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);

ALTER TABLE email_gateway.mailbox_config_outbox
    ADD COLUMN IF NOT EXISTS activation_not_before timestamptz;
UPDATE email_gateway.mailbox_config_outbox
SET activation_not_before = COALESCE(created_at, clock_timestamp())
WHERE activation_not_before IS NULL;
ALTER TABLE email_gateway.mailbox_config_outbox
    ALTER COLUMN activation_not_before SET DEFAULT clock_timestamp();
ALTER TABLE email_gateway.mailbox_config_outbox
    ALTER COLUMN activation_not_before SET NOT NULL;

CREATE TABLE IF NOT EXISTS email_gateway.identity_projection_receipts (
    site_id text NOT NULL,
    processing_purpose text NOT NULL,
    opaque_address_ref text NOT NULL,
    external_identity_ref text NOT NULL,
    external_identity_revision bigint NOT NULL,
    identity_type text NOT NULL,
    team_ref text NOT NULL,
    status text NOT NULL,
    projection_receipt_ref text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, projection_receipt_ref),
    UNIQUE (site_id, opaque_address_ref, external_identity_revision),
    CHECK (opaque_address_ref ~ '^extid:v1:[a-z][a-z0-9_]{0,31}:[A-Za-z0-9_-]{43}$'),
    CHECK (external_identity_revision >= 1),
    CHECK (identity_type IN ('User', 'Party')),
    CHECK (status IN ('confirmed', 'revoked')),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.routing_rules (
    site_id text NOT NULL,
    rule_ref text NOT NULL,
    team_ref text NOT NULL,
    mailbox_ref text NOT NULL,
    owner_user_ref text NOT NULL,
    priority integer NOT NULL,
    revision bigint NOT NULL,
    enabled boolean NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, rule_ref),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, mailbox_ref)
        REFERENCES email_gateway.mailboxes (site_id, mailbox_ref),
    CHECK (priority BETWEEN 0 AND 1000),
    CHECK (revision >= 1),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.audit_events (
    site_id text NOT NULL,
    audit_ref text NOT NULL,
    actor_ref text NOT NULL,
    event_type text NOT NULL,
    subject_ref text NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, audit_ref),
    UNIQUE (site_id, idempotency_key),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE OR REPLACE FUNCTION email_gateway.reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, email_gateway
AS $$
BEGIN
    RAISE EXCEPTION 'immutable email gateway record'
        USING ERRCODE = 'integrity_constraint_violation';
END
$$;
REVOKE ALL ON FUNCTION email_gateway.reject_immutable_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS identity_projection_receipts_immutable
    ON email_gateway.identity_projection_receipts;
CREATE TRIGGER identity_projection_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.identity_projection_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS audit_events_immutable ON email_gateway.audit_events;
CREATE TRIGGER audit_events_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.audit_events
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.mailboxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.mailboxes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.mailboxes FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.mailboxes;
CREATE POLICY email_gateway_site_scope ON email_gateway.mailboxes
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.mailbox_config_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.mailbox_config_outbox FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.mailbox_config_outbox FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.mailbox_config_outbox;
CREATE POLICY email_gateway_site_scope ON email_gateway.mailbox_config_outbox
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.identity_projection_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.identity_projection_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.identity_projection_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.identity_projection_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.identity_projection_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.routing_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.routing_rules FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.routing_rules FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.routing_rules;
CREATE POLICY email_gateway_site_scope ON email_gateway.routing_rules
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.audit_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.audit_events FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.audit_events;
CREATE POLICY email_gateway_site_scope ON email_gateway.audit_events
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT, INSERT, UPDATE ON email_gateway.mailboxes TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.mailbox_config_outbox TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.identity_projection_receipts TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.routing_rules TO gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.audit_events TO gbos_email_gateway_app;
GRANT SELECT, UPDATE ON email_gateway.mailbox_config_outbox TO gbos_email_gateway_worker;
GRANT SELECT ON email_gateway.mailboxes TO gbos_email_gateway_worker;
