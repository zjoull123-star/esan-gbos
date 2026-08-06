ALTER TABLE observer.manual_import_jobs
    ADD COLUMN IF NOT EXISTS result_event_id text,
    ADD COLUMN IF NOT EXISTS checkpoint_disposition text;

ALTER TABLE observer.observation_events
    ADD COLUMN IF NOT EXISTS raw_sha256 char(64),
    ADD COLUMN IF NOT EXISTS occurred_minute timestamptz;

UPDATE observer.observation_events
SET
    raw_sha256 = COALESCE(raw_sha256, document ->> 'raw_sha256'),
    occurred_minute = COALESCE(
        occurred_minute,
        date_trunc('minute', occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
    )
WHERE raw_sha256 IS NULL OR occurred_minute IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS observation_events_fallback_dedup_uq
    ON observer.observation_events
        (site_id, connector, raw_sha256, occurred_minute)
    WHERE provider_event_id IS NULL;

ALTER TABLE observer.event_evidence
    ADD COLUMN IF NOT EXISTS evidence_ordinal integer;

ALTER TABLE observer.checkpoints
    ADD COLUMN IF NOT EXISTS cursor_occurred_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_event_id text;

ALTER TABLE observer.dead_letter
    ADD COLUMN IF NOT EXISTS job_id text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'manual_import_jobs_result_event_fk'
          AND conrelid = 'observer.manual_import_jobs'::regclass
    ) THEN
        ALTER TABLE observer.manual_import_jobs
            ADD CONSTRAINT manual_import_jobs_result_event_fk
            FOREIGN KEY (site_id, result_event_id)
            REFERENCES observer.observation_events (site_id, event_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'checkpoints_last_event_fk'
          AND conrelid = 'observer.checkpoints'::regclass
    ) THEN
        ALTER TABLE observer.checkpoints
            ADD CONSTRAINT checkpoints_last_event_fk
            FOREIGN KEY (site_id, last_event_id)
            REFERENCES observer.observation_events (site_id, event_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'dead_letter_job_fk'
          AND conrelid = 'observer.dead_letter'::regclass
    ) THEN
        ALTER TABLE observer.dead_letter
            ADD CONSTRAINT dead_letter_job_fk
            FOREIGN KEY (site_id, job_id)
            REFERENCES observer.manual_import_jobs (site_id, job_id);
    END IF;
END
$$;

GRANT USAGE ON SCHEMA observer TO gbos_observer_app;
GRANT SELECT, INSERT ON
    observer.raw_objects,
    observer.observation_events,
    observer.participants,
    observer.evidence_refs,
    observer.event_evidence,
    observer.processor_runs,
    observer.derivation_edges
TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON
    observer.manual_import_jobs,
    observer.checkpoints,
    observer.dead_letter
TO gbos_observer_app;
