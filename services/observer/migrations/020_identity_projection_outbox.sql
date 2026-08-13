DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'gbos_observer_identity_projector'
    ) THEN
        CREATE ROLE gbos_observer_identity_projector
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

CREATE TABLE IF NOT EXISTS observer.identity_projection_outbox (
    site_id text NOT NULL,
    processing_purpose text NOT NULL,
    opaque_address_ref text NOT NULL,
    external_identity_revision integer NOT NULL,
    projection_receipt char(71) NOT NULL,
    payload jsonb NOT NULL,
    payload_digest char(71) NOT NULL,
    relay_status text NOT NULL DEFAULT 'queued',
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    next_attempt_at timestamptz NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0,
    last_error_code text,
    delivery_receipt char(71),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (
        site_id, processing_purpose, opaque_address_ref,
        external_identity_revision
    ),
    UNIQUE (site_id, projection_receipt),
    CHECK (site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$'),
    CHECK (processing_purpose IN (
        'business_operations', 'observation_processing', 'entity_resolution',
        'customer_service', 'sales_follow_up', 'procurement_coordination',
        'product_sample_management', 'risk_review', 'metric_reporting',
        'audit_compliance'
    )),
    CHECK (opaque_address_ref ~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'),
    CHECK (external_identity_revision BETWEEN 1 AND 2147483647),
    CHECK (projection_receipt ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK (payload ?& ARRAY[
        'site_id', 'processing_purpose', 'opaque_address_ref',
        'external_identity_ref', 'external_identity_revision',
        'identity_type', 'team_ref', 'status', 'projection_receipt',
        'observed_at'
    ]),
    CHECK (payload - ARRAY[
        'site_id', 'processing_purpose', 'opaque_address_ref',
        'external_identity_ref', 'external_identity_revision',
        'identity_type', 'team_ref', 'status', 'projection_receipt',
        'observed_at'
    ] = '{}'::jsonb),
    CHECK (payload->>'site_id' = site_id),
    CHECK (payload->>'processing_purpose' = processing_purpose),
    CHECK (payload->>'opaque_address_ref' = opaque_address_ref),
    CHECK ((payload->>'external_identity_revision')::integer = external_identity_revision),
    CHECK (payload->>'projection_receipt' = projection_receipt),
    CHECK (relay_status IN ('queued', 'leased', 'retry', 'delivered', 'dead_letter')),
    CHECK (attempt_count BETWEEN 0 AND 5),
    CHECK (max_attempts = 5),
    CHECK (attempt_count <= max_attempts),
    CHECK (lease_generation >= 0),
    CHECK (
        (relay_status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (relay_status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (delivered_at IS NULL OR relay_status = 'delivered'),
    CHECK (delivery_receipt IS NULL OR delivery_receipt ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS identity_projection_outbox_claim_idx
    ON observer.identity_projection_outbox (
        site_id, relay_status, next_attempt_at, projection_receipt
    )
    WHERE relay_status IN ('queued', 'leased', 'retry');

CREATE OR REPLACE FUNCTION observer.reject_identity_projection_payload_rewrite()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.processing_purpose IS DISTINCT FROM OLD.processing_purpose
       OR NEW.opaque_address_ref IS DISTINCT FROM OLD.opaque_address_ref
       OR NEW.external_identity_revision IS DISTINCT FROM OLD.external_identity_revision
       OR NEW.projection_receipt IS DISTINCT FROM OLD.projection_receipt
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_digest IS DISTINCT FROM OLD.payload_digest
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'identity projection payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_identity_projection_payload_rewrite() FROM PUBLIC;

DROP TRIGGER IF EXISTS identity_projection_outbox_payload_immutable
    ON observer.identity_projection_outbox;
CREATE TRIGGER identity_projection_outbox_payload_immutable
    BEFORE UPDATE ON observer.identity_projection_outbox
    FOR EACH ROW EXECUTE FUNCTION observer.reject_identity_projection_payload_rewrite();

ALTER TABLE observer.identity_projection_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_projection_outbox FORCE ROW LEVEL SECURITY;
REVOKE ALL ON observer.identity_projection_outbox FROM PUBLIC;
DROP POLICY IF EXISTS identity_projection_outbox_site_isolation
    ON observer.identity_projection_outbox;
CREATE POLICY identity_projection_outbox_site_isolation
    ON observer.identity_projection_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.identity_projection_outbox FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.identity_projection_outbox TO gbos_observer_app;

GRANT USAGE ON SCHEMA observer TO gbos_observer_identity_projector;
REVOKE ALL ON observer.identity_projection_outbox FROM gbos_observer_identity_projector;
GRANT SELECT ON observer.identity_projection_outbox TO gbos_observer_identity_projector;
GRANT UPDATE (
    relay_status, attempt_count, next_attempt_at, lease_owner,
    lease_expires_at, lease_generation, last_error_code,
    delivery_receipt, delivered_at, updated_at
) ON observer.identity_projection_outbox TO gbos_observer_identity_projector;
