DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'observer'
          AND table_name = 'connector_checkpoints'
          AND column_name = 'lease_generation'
    ) AND EXISTS (
        SELECT 1
        FROM observer.connector_checkpoints
        WHERE lease_owner IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'connector_lease_generation_backfill_ambiguous'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$$;

ALTER TABLE observer.connector_checkpoints
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'connector_checkpoints_lease_generation_ck'
          AND conrelid = 'observer.connector_checkpoints'::regclass
    ) THEN
        ALTER TABLE observer.connector_checkpoints
            ADD CONSTRAINT connector_checkpoints_lease_generation_ck
            CHECK (lease_generation >= 0) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.connector_checkpoints
    VALIDATE CONSTRAINT connector_checkpoints_lease_generation_ck;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'observer'
          AND table_name = 'email_poll_batches'
          AND column_name = 'connector_lease_generation'
    ) AND EXISTS (
        SELECT 1
        FROM observer.email_poll_batches
    ) THEN
        RAISE EXCEPTION 'email_poll_batch_generation_backfill_ambiguous'
            USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END
$$;

ALTER TABLE observer.email_poll_batches
    ADD COLUMN IF NOT EXISTS connector_lease_generation bigint;

ALTER TABLE observer.email_poll_batches
    ALTER COLUMN connector_lease_generation SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'email_poll_batches_connector_lease_generation_ck'
          AND conrelid = 'observer.email_poll_batches'::regclass
    ) THEN
        ALTER TABLE observer.email_poll_batches
            ADD CONSTRAINT email_poll_batches_connector_lease_generation_ck
            CHECK (connector_lease_generation >= 1) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.email_poll_batches
    VALIDATE CONSTRAINT email_poll_batches_connector_lease_generation_ck;

ALTER TABLE observer.connector_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_checkpoints FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS connector_checkpoints_site_isolation
    ON observer.connector_checkpoints;
CREATE POLICY connector_checkpoints_site_isolation
    ON observer.connector_checkpoints
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.email_poll_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_poll_batches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS email_poll_batches_site_isolation
    ON observer.email_poll_batches;
CREATE POLICY email_poll_batches_site_isolation
    ON observer.email_poll_batches
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.connector_checkpoints FROM PUBLIC;
REVOKE ALL ON observer.email_poll_batches FROM PUBLIC;
REVOKE ALL ON observer.connector_checkpoints FROM gbos_observer_app;
REVOKE ALL ON observer.email_poll_batches FROM gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.connector_checkpoints
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.email_poll_batches
    TO gbos_observer_app;
