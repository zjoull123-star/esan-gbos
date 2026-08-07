ALTER TABLE observer.inbound_deliveries
    ADD COLUMN IF NOT EXISTS object_ref text,
    ADD COLUMN IF NOT EXISTS byte_size bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'inbound_deliveries_object_metadata_ck'
          AND conrelid = 'observer.inbound_deliveries'::regclass
    ) THEN
        ALTER TABLE observer.inbound_deliveries
            ADD CONSTRAINT inbound_deliveries_object_metadata_ck
            CHECK (
                (object_ref IS NULL AND byte_size IS NULL)
                OR (
                    object_ref IS NOT NULL
                    AND char_length(object_ref) BETWEEN 1 AND 512
                    AND byte_size IS NOT NULL
                    AND byte_size >= 0
                )
            ) NOT VALID;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION observer.reject_inbound_delivery_content_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.exact_body_sha256,
        NEW.object_ref,
        NEW.byte_size,
        NEW.media_type,
        NEW.received_at,
        NEW.correlation_id
    ) IS DISTINCT FROM ROW(
        OLD.exact_body_sha256,
        OLD.object_ref,
        OLD.byte_size,
        OLD.media_type,
        OLD.received_at,
        OLD.correlation_id
    ) THEN
        RAISE EXCEPTION 'inbound delivery content metadata is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS inbound_deliveries_immutable_content
    ON observer.inbound_deliveries;
CREATE TRIGGER inbound_deliveries_immutable_content
BEFORE UPDATE ON observer.inbound_deliveries
FOR EACH ROW
EXECUTE FUNCTION observer.reject_inbound_delivery_content_mutation();

CREATE OR REPLACE FUNCTION observer.enforce_inbound_delivery_state_machine()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.processing_status = OLD.processing_status THEN
        RETURN NEW;
    END IF;
    IF (
        (OLD.processing_status = 'received'
            AND NEW.processing_status IN ('authenticated', 'queued', 'quarantined', 'failed'))
        OR (OLD.processing_status = 'authenticated'
            AND NEW.processing_status IN ('queued', 'quarantined', 'failed'))
        OR (OLD.processing_status = 'queued'
            AND NEW.processing_status IN ('processing', 'quarantined', 'failed'))
        OR (OLD.processing_status = 'processing'
            AND NEW.processing_status IN ('succeeded', 'quarantined', 'failed'))
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal inbound delivery state transition: % -> %',
        OLD.processing_status,
        NEW.processing_status
        USING ERRCODE = 'integrity_constraint_violation';
END
$$;

DROP TRIGGER IF EXISTS inbound_deliveries_state_machine
    ON observer.inbound_deliveries;
CREATE TRIGGER inbound_deliveries_state_machine
BEFORE UPDATE OF processing_status ON observer.inbound_deliveries
FOR EACH ROW
EXECUTE FUNCTION observer.enforce_inbound_delivery_state_machine();

ALTER TABLE observer.processing_jobs
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS generation integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'processing_jobs_idempotency_key_ck'
          AND conrelid = 'observer.processing_jobs'::regclass
    ) THEN
        ALTER TABLE observer.processing_jobs
            ADD CONSTRAINT processing_jobs_idempotency_key_ck
            CHECK (
                idempotency_key IS NULL
                OR char_length(idempotency_key) BETWEEN 1 AND 256
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'processing_jobs_generation_ck'
          AND conrelid = 'observer.processing_jobs'::regclass
    ) THEN
        ALTER TABLE observer.processing_jobs
            ADD CONSTRAINT processing_jobs_generation_ck
            CHECK (generation >= 0 AND lease_generation >= 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'processing_jobs_lease_pair_ck'
          AND conrelid = 'observer.processing_jobs'::regclass
    ) THEN
        ALTER TABLE observer.processing_jobs
            ADD CONSTRAINT processing_jobs_lease_pair_ck
            CHECK (
                (lease_owner IS NULL AND lease_expires_at IS NULL)
                OR (
                    lease_owner IS NOT NULL
                    AND char_length(lease_owner) BETWEEN 1 AND 256
                    AND lease_expires_at IS NOT NULL
                )
            ) NOT VALID;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_idempotency_uq
    ON observer.processing_jobs (site_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_delivery_generation_uq
    ON observer.processing_jobs (
        site_id, connector, connector_instance_id, delivery_id, generation
    )
    WHERE delivery_id IS NOT NULL AND idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS processing_jobs_claim_idx
    ON observer.processing_jobs (
        site_id, status, next_retry_at, lease_expires_at, created_at, job_id
    )
    WHERE status IN ('queued', 'processing', 'retry_wait');

ALTER TABLE observer.connector_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_instances FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_deliveries FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_delivery_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_delivery_events FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_checkpoints FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.persistent_nonces ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.persistent_nonces FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.processing_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.processing_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_quarantine FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_dead_letter FORCE ROW LEVEL SECURITY;
