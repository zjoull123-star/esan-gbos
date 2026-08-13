DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'gbos_observer_publisher'
    ) THEN
        CREATE ROLE gbos_observer_publisher
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$$;

ALTER TABLE observer.email_connector_config_projections
    ADD COLUMN IF NOT EXISTS config_publication_ref text,
    ADD COLUMN IF NOT EXISTS entry_role text,
    ADD COLUMN IF NOT EXISTS business_purpose text,
    ADD COLUMN IF NOT EXISTS team_ref text,
    ADD COLUMN IF NOT EXISTS credential_ref text,
    ADD COLUMN IF NOT EXISTS inbound_enabled boolean;

-- These values cannot be reconstructed safely from a legacy projection. If any
-- pre-release row exists without them, the migration stops instead of guessing.
ALTER TABLE observer.email_connector_config_projections
    ALTER COLUMN config_publication_ref SET NOT NULL,
    ALTER COLUMN entry_role SET NOT NULL,
    ALTER COLUMN business_purpose SET NOT NULL,
    ALTER COLUMN team_ref SET NOT NULL,
    ALTER COLUMN credential_ref SET NOT NULL,
    ALTER COLUMN inbound_enabled SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'email_connector_config_publication_ref_ck'
          AND conrelid = 'observer.email_connector_config_projections'::regclass
    ) THEN
        ALTER TABLE observer.email_connector_config_projections
            ADD CONSTRAINT email_connector_config_publication_ref_ck
            CHECK (config_publication_ref ~ '^MCP-[0-9A-HJKMNP-TV-Z]{26}$')
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'email_connector_config_entry_role_ck'
          AND conrelid = 'observer.email_connector_config_projections'::regclass
    ) THEN
        ALTER TABLE observer.email_connector_config_projections
            ADD CONSTRAINT email_connector_config_entry_role_ck
            CHECK (entry_role IN (
                'primary', 'workflow', 'migration', 'selective_archive'
            )) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'email_connector_config_business_purpose_ck'
          AND conrelid = 'observer.email_connector_config_projections'::regclass
    ) THEN
        ALTER TABLE observer.email_connector_config_projections
            ADD CONSTRAINT email_connector_config_business_purpose_ck
            CHECK (business_purpose IN (
                'business_operations', 'observation_processing',
                'entity_resolution', 'customer_service', 'sales_follow_up',
                'procurement_coordination', 'product_sample_management',
                'risk_review', 'metric_reporting', 'audit_compliance'
            )) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'email_connector_config_team_ref_ck'
          AND conrelid = 'observer.email_connector_config_projections'::regclass
    ) THEN
        ALTER TABLE observer.email_connector_config_projections
            ADD CONSTRAINT email_connector_config_team_ref_ck
            CHECK (team_ref ~ '^TEM-[0-9A-HJKMNP-TV-Z]{26}$') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'email_connector_config_credential_ref_ck'
          AND conrelid = 'observer.email_connector_config_projections'::regclass
    ) THEN
        ALTER TABLE observer.email_connector_config_projections
            ADD CONSTRAINT email_connector_config_credential_ref_ck
            CHECK (
                char_length(credential_ref) BETWEEN 14 AND 128
                AND credential_ref ~ '^secretref:v1/[A-Za-z0-9][A-Za-z0-9._/-]*$'
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.email_connector_config_projections
    VALIDATE CONSTRAINT email_connector_config_publication_ref_ck,
    VALIDATE CONSTRAINT email_connector_config_entry_role_ck,
    VALIDATE CONSTRAINT email_connector_config_business_purpose_ck,
    VALIDATE CONSTRAINT email_connector_config_team_ref_ck,
    VALIDATE CONSTRAINT email_connector_config_credential_ref_ck;

CREATE UNIQUE INDEX IF NOT EXISTS email_connector_config_publication_ref_uq
    ON observer.email_connector_config_projections (site_id, config_publication_ref);

CREATE OR REPLACE FUNCTION observer.reject_email_connector_config_rewrite()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'email_connector_config_projection_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_connector_config_rewrite() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_connector_config_projection_immutable
    ON observer.email_connector_config_projections;
CREATE TRIGGER email_connector_config_projection_immutable
    BEFORE UPDATE OR DELETE ON observer.email_connector_config_projections
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_connector_config_rewrite();

REVOKE ALL ON observer.email_connector_config_projections
    FROM gbos_observer_publisher;
REVOKE ALL ON observer.email_poll_batches FROM gbos_observer_publisher;
REVOKE ALL ON observer.email_poll_batch_deliveries FROM gbos_observer_publisher;
REVOKE ALL ON observer.email_message_publication_outbox
    FROM gbos_observer_publisher;

-- The Observer API applies connector configuration; the relay login can only
-- lease and acknowledge the Observer-owned publication outbox.
REVOKE ALL ON observer.email_message_publication_outbox FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_message_publication_outbox
    TO gbos_observer_app;
GRANT USAGE ON SCHEMA observer TO gbos_observer_publisher;
GRANT SELECT ON observer.email_message_publication_outbox
    TO gbos_observer_publisher;
GRANT UPDATE (
    relay_status, attempt_count, next_attempt_at, lease_owner,
    lease_expires_at, relay_generation, last_error_code,
    delivery_receipt, delivery_receipt_digest, delivered_at, updated_at
) ON observer.email_message_publication_outbox TO gbos_observer_publisher;

ALTER TABLE observer.email_connector_config_projections
    ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_connector_config_projections
    FORCE ROW LEVEL SECURITY;
