CREATE TABLE IF NOT EXISTS email_gateway.retention_runs (
    site_id text NOT NULL,
    run_ref text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    attempt integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0,
    dry_run boolean NOT NULL,
    planned_count integer NOT NULL DEFAULT 0,
    expired_count integer NOT NULL DEFAULT 0,
    payload_digest text NOT NULL,
    safe_error_code text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, run_ref),
    CHECK (status IN ('queued', 'leased', 'retry', 'completed', 'dead_letter')),
    CHECK (attempt BETWEEN 0 AND 5),
    CHECK (lease_generation >= 0),
    CHECK (planned_count >= 0 AND expired_count BETWEEN 0 AND planned_count),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS email_gateway.content_expiration_receipts (
    site_id text NOT NULL,
    expiration_receipt_ref text NOT NULL,
    projection_ref text NOT NULL,
    observer_expiration_receipt_ref text NOT NULL,
    evidence_ref text NOT NULL,
    payload_digest text NOT NULL,
    expired_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, expiration_receipt_ref),
    UNIQUE (site_id, projection_ref),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

DROP TRIGGER IF EXISTS content_expiration_receipts_immutable
    ON email_gateway.content_expiration_receipts;
CREATE TRIGGER content_expiration_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.content_expiration_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.retention_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.retention_runs FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.retention_runs FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.retention_runs;
CREATE POLICY email_gateway_site_scope ON email_gateway.retention_runs
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.content_expiration_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.content_expiration_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.content_expiration_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.content_expiration_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.content_expiration_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT ON email_gateway.retention_runs TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.content_expiration_receipts TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.retention_runs TO gbos_email_gateway_worker;
GRANT SELECT, INSERT ON email_gateway.content_expiration_receipts TO gbos_email_gateway_worker;
