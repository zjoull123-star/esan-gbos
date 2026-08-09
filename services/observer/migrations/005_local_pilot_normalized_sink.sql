ALTER TABLE observer.observation_events
    ADD COLUMN IF NOT EXISTS processing_job_id text,
    ADD COLUMN IF NOT EXISTS delivery_id text,
    ADD COLUMN IF NOT EXISTS team_ref text,
    ADD COLUMN IF NOT EXISTS party_ref text,
    ADD COLUMN IF NOT EXISTS normalized_payload_sha256 char(64),
    ADD COLUMN IF NOT EXISTS retention_until timestamptz;

ALTER TABLE observer.raw_objects
    ADD COLUMN IF NOT EXISTS retention_until timestamptz;

ALTER TABLE observer.evidence_refs
    ADD COLUMN IF NOT EXISTS content_object_ref text;

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
        OR (
            OLD.processing_status = 'failed'
            AND NEW.processing_status = 'queued'
        )
    ) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal inbound delivery state transition: % -> %',
        OLD.processing_status,
        NEW.processing_status
        USING ERRCODE = 'integrity_constraint_violation';
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'observation_events_normalized_payload_sha256_ck'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_normalized_payload_sha256_ck
            CHECK (
                normalized_payload_sha256 IS NULL
                OR normalized_payload_sha256 ~ '^[a-f0-9]{64}$'
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'observation_events_retention_until_ck'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_retention_until_ck
            CHECK (
                retention_until IS NULL OR retention_until > ingested_at
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'raw_objects_retention_until_ck'
          AND conrelid = 'observer.raw_objects'::regclass
    ) THEN
        ALTER TABLE observer.raw_objects
            ADD CONSTRAINT raw_objects_retention_until_ck
            CHECK (
                retention_until IS NULL OR retention_until > created_at
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'observation_events_job_source_ck'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_job_source_ck
            CHECK (
                (
                    connector = 'manual_import'
                    AND processing_job_id IS NULL
                    AND delivery_id IS NULL
                )
                OR (
                    connector <> 'manual_import'
                    AND job_id IS NULL
                    AND processing_job_id IS NOT NULL
                    AND delivery_id IS NOT NULL
                )
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'observation_events_processing_job_fk'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_processing_job_fk
            FOREIGN KEY (site_id, processing_job_id)
            REFERENCES observer.processing_jobs (site_id, job_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'observation_events_delivery_fk'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_delivery_fk
            FOREIGN KEY (
                site_id, connector, connector_instance_id, delivery_id
            )
            REFERENCES observer.inbound_deliveries (
                site_id, connector, connector_instance_id, delivery_id
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'evidence_refs_content_object_ref_ck'
          AND conrelid = 'observer.evidence_refs'::regclass
    ) THEN
        ALTER TABLE observer.evidence_refs
            ADD CONSTRAINT evidence_refs_content_object_ref_ck
            CHECK (
                content_object_ref IS NULL
                OR char_length(content_object_ref) BETWEEN 1 AND 512
            ) NOT VALID;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS observation_events_instance_provider_dedup_uq
    ON observer.observation_events (
        site_id, connector, connector_instance_id, provider_event_id
    )
    WHERE provider_event_id IS NOT NULL;

ALTER TABLE observer.observation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.observation_events FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.raw_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.raw_objects FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.participants FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.evidence_refs FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.event_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.event_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox FORCE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA observer TO gbos_observer_app;
GRANT SELECT, INSERT ON
    observer.raw_objects,
    observer.observation_events,
    observer.participants,
    observer.evidence_refs,
    observer.event_evidence
TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON
    observer.context_publication_outbox
TO gbos_observer_app;
