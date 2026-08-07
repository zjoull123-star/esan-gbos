BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_media_runtime_app') THEN
        CREATE ROLE gbos_media_runtime_app
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE gbos_media_runtime_app
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN NOBYPASSRLS;

CREATE SCHEMA IF NOT EXISTS media_runtime;

CREATE TABLE IF NOT EXISTS media_runtime.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS media_runtime.local_media_jobs (
    site_id text NOT NULL CHECK (char_length(site_id) BETWEEN 1 AND 140),
    job_id text NOT NULL CHECK (char_length(job_id) BETWEEN 1 AND 256),
    request_id text NOT NULL CHECK (char_length(request_id) BETWEEN 1 AND 256),
    receipt jsonb NOT NULL CHECK (jsonb_typeof(receipt) = 'object'),
    work_spec jsonb NOT NULL CHECK (jsonb_typeof(work_spec) = 'object'),
    submission_digest char(64) NOT NULL
        CHECK (submission_digest ~ '^[a-f0-9]{64}$'),
    status text NOT NULL
        CHECK (
            status IN (
                'queued', 'leased', 'retry', 'ready', 'quarantined', 'dead_letter'
            )
        ),
    due_at timestamptz NOT NULL,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 256),
    lease_expires_at timestamptz,
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(reason_codes) = 'array'),
    transcript_ref text CHECK (char_length(transcript_ref) BETWEEN 1 AND 512),
    artifact_proof jsonb CHECK (
        artifact_proof IS NULL OR jsonb_typeof(artifact_proof) = 'object'
    ),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, job_id),
    UNIQUE (site_id, request_id),
    CHECK (attempt <= max_attempts),
    CHECK (fencing_token >= attempt),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        status <> 'ready'
        OR (
            jsonb_typeof(receipt -> 'media_type') = 'string'
            AND (
                receipt ->> 'media_type' NOT LIKE 'audio/%'
                OR (
                    transcript_ref IS NOT NULL
                    AND artifact_proof IS NOT NULL
                    AND artifact_proof ?& ARRAY[
                        'ffmpeg_output_sha256',
                        'ffmpeg_executable_sha256',
                        'whisper_model_sha256'
                    ]
                )
            )
        )
    )
);

CREATE INDEX IF NOT EXISTS local_media_jobs_claim_idx
    ON media_runtime.local_media_jobs (
        site_id, status, due_at, created_at, job_id
    )
    WHERE status IN ('queued', 'retry', 'leased');

CREATE INDEX IF NOT EXISTS local_media_jobs_terminal_idx
    ON media_runtime.local_media_jobs (site_id, status, updated_at, job_id)
    WHERE status IN ('quarantined', 'dead_letter');

CREATE OR REPLACE FUNCTION media_runtime.prevent_media_job_input_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.receipt IS DISTINCT FROM OLD.receipt
       OR NEW.work_spec IS DISTINCT FROM OLD.work_spec
       OR NEW.submission_digest IS DISTINCT FROM OLD.submission_digest
       OR NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.request_id IS DISTINCT FROM OLD.request_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts THEN
        RAISE EXCEPTION 'local media job input is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS local_media_jobs_input_immutable
    ON media_runtime.local_media_jobs;
CREATE TRIGGER local_media_jobs_input_immutable
BEFORE UPDATE ON media_runtime.local_media_jobs
FOR EACH ROW
EXECUTE FUNCTION media_runtime.prevent_media_job_input_change();

ALTER TABLE media_runtime.local_media_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_runtime.local_media_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS local_media_jobs_site_isolation
    ON media_runtime.local_media_jobs;
CREATE POLICY local_media_jobs_site_isolation
    ON media_runtime.local_media_jobs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON SCHEMA media_runtime FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA media_runtime FROM PUBLIC;
GRANT USAGE ON SCHEMA media_runtime TO gbos_media_runtime_app;
GRANT SELECT, INSERT, UPDATE ON media_runtime.local_media_jobs
    TO gbos_media_runtime_app;

INSERT INTO media_runtime.schema_migrations (migration_name, applied_at)
VALUES ('media_runtime/001_local_media_jobs.sql', now())
ON CONFLICT (migration_name) DO NOTHING;

COMMIT;
