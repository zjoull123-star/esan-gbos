CREATE SCHEMA IF NOT EXISTS observer;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_observer_app') THEN
        CREATE ROLE gbos_observer_app
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

ALTER ROLE gbos_observer_app
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS observer.manual_import_jobs (
    site_id text NOT NULL,
    job_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_sha256 char(64) NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, job_id),
    UNIQUE (site_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS observer.raw_objects (
    site_id text NOT NULL,
    object_id text NOT NULL,
    object_ref text NOT NULL,
    sha256 char(64) NOT NULL,
    media_type text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    retention_class text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, object_id),
    UNIQUE (site_id, sha256)
);

CREATE TABLE IF NOT EXISTS observer.observation_events (
    site_id text NOT NULL,
    event_id text NOT NULL,
    job_id text,
    raw_object_id text,
    provider_event_id text,
    connector text NOT NULL,
    channel text NOT NULL,
    processing_purpose text NOT NULL,
    consent_basis text NOT NULL,
    data_classification text NOT NULL,
    retention_class text NOT NULL,
    correlation_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (site_id, event_id),
    UNIQUE (site_id, connector, provider_event_id),
    FOREIGN KEY (site_id, job_id)
        REFERENCES observer.manual_import_jobs (site_id, job_id),
    FOREIGN KEY (site_id, raw_object_id)
        REFERENCES observer.raw_objects (site_id, object_id)
);

CREATE TABLE IF NOT EXISTS observer.participants (
    site_id text NOT NULL,
    event_id text NOT NULL,
    participant_id text NOT NULL,
    role text NOT NULL,
    identity_ref text NOT NULL,
    display_name text,
    PRIMARY KEY (site_id, event_id, participant_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observer.evidence_refs (
    site_id text NOT NULL,
    evidence_id text NOT NULL,
    event_id text NOT NULL,
    raw_object_id text NOT NULL,
    raw_sha256 char(64) NOT NULL,
    media_type text NOT NULL,
    locator jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, evidence_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id),
    FOREIGN KEY (site_id, raw_object_id)
        REFERENCES observer.raw_objects (site_id, object_id)
);

CREATE TABLE IF NOT EXISTS observer.event_evidence (
    site_id text NOT NULL,
    event_id text NOT NULL,
    evidence_id text NOT NULL,
    PRIMARY KEY (site_id, event_id, evidence_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id)
        ON DELETE CASCADE,
    FOREIGN KEY (site_id, evidence_id)
        REFERENCES observer.evidence_refs (site_id, evidence_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS observer.checkpoints (
    site_id text NOT NULL,
    checkpoint_id text NOT NULL,
    connector text NOT NULL,
    cursor_value text,
    replay_window_seconds integer NOT NULL CHECK (replay_window_seconds >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    status text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, checkpoint_id),
    UNIQUE (site_id, connector)
);

CREATE TABLE IF NOT EXISTS observer.quarantine (
    site_id text NOT NULL,
    quarantine_id text NOT NULL,
    job_id text,
    reason_code text NOT NULL,
    payload_sha256 char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    PRIMARY KEY (site_id, quarantine_id),
    FOREIGN KEY (site_id, job_id)
        REFERENCES observer.manual_import_jobs (site_id, job_id)
);

CREATE TABLE IF NOT EXISTS observer.dead_letter (
    site_id text NOT NULL,
    dead_letter_id text NOT NULL,
    event_id text,
    reason_code text NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error_at timestamptz NOT NULL DEFAULT now(),
    recover_after timestamptz,
    PRIMARY KEY (site_id, dead_letter_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id)
);

CREATE TABLE IF NOT EXISTS observer.processor_runs (
    site_id text NOT NULL,
    processor_run_id text NOT NULL,
    event_id text NOT NULL,
    processor_id text NOT NULL,
    processor_version text NOT NULL,
    rule_version text NOT NULL,
    output_version text NOT NULL,
    network_calls integer NOT NULL DEFAULT 0 CHECK (network_calls = 0),
    tool_calls integer NOT NULL DEFAULT 0 CHECK (tool_calls = 0),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    PRIMARY KEY (site_id, processor_run_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id)
);

CREATE TABLE IF NOT EXISTS observer.derivation_edges (
    site_id text NOT NULL,
    derivation_edge_id text NOT NULL,
    source_type text NOT NULL,
    source_id text NOT NULL,
    derived_type text NOT NULL,
    derived_id text NOT NULL,
    processor_run_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, derivation_edge_id),
    FOREIGN KEY (site_id, processor_run_id)
        REFERENCES observer.processor_runs (site_id, processor_run_id)
);

CREATE TABLE IF NOT EXISTS observer.consent (
    site_id text NOT NULL,
    consent_id text NOT NULL,
    event_id text NOT NULL,
    consent_basis text NOT NULL,
    status text NOT NULL,
    effective_at timestamptz NOT NULL,
    withdrawn_at timestamptz,
    PRIMARY KEY (site_id, consent_id),
    FOREIGN KEY (site_id, event_id)
        REFERENCES observer.observation_events (site_id, event_id)
);

CREATE TABLE IF NOT EXISTS observer.legal_holds (
    site_id text NOT NULL,
    hold_id text NOT NULL,
    evidence_id text NOT NULL,
    owner_ref text NOT NULL,
    reason text NOT NULL,
    started_at timestamptz NOT NULL,
    released_at timestamptz,
    PRIMARY KEY (site_id, hold_id),
    FOREIGN KEY (site_id, evidence_id)
        REFERENCES observer.evidence_refs (site_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS observer.deletion_receipts (
    site_id text NOT NULL,
    receipt_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    target_sha256 char(64),
    outcome text NOT NULL,
    retained_reason text,
    deleted_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, receipt_id)
);

ALTER TABLE observer.manual_import_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.manual_import_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS manual_import_jobs_site_isolation ON observer.manual_import_jobs;
CREATE POLICY manual_import_jobs_site_isolation ON observer.manual_import_jobs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.raw_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.raw_objects FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS raw_objects_site_isolation ON observer.raw_objects;
CREATE POLICY raw_objects_site_isolation ON observer.raw_objects
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.observation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.observation_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS observation_events_site_isolation ON observer.observation_events;
CREATE POLICY observation_events_site_isolation ON observer.observation_events
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.participants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS participants_site_isolation ON observer.participants;
CREATE POLICY participants_site_isolation ON observer.participants
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.evidence_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_refs_site_isolation ON observer.evidence_refs;
CREATE POLICY evidence_refs_site_isolation ON observer.evidence_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.event_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.event_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS event_evidence_site_isolation ON observer.event_evidence;
CREATE POLICY event_evidence_site_isolation ON observer.event_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.checkpoints FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS checkpoints_site_isolation ON observer.checkpoints;
CREATE POLICY checkpoints_site_isolation ON observer.checkpoints
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.quarantine FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS quarantine_site_isolation ON observer.quarantine;
CREATE POLICY quarantine_site_isolation ON observer.quarantine
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.dead_letter FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dead_letter_site_isolation ON observer.dead_letter;
CREATE POLICY dead_letter_site_isolation ON observer.dead_letter
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.processor_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.processor_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS processor_runs_site_isolation ON observer.processor_runs;
CREATE POLICY processor_runs_site_isolation ON observer.processor_runs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.derivation_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.derivation_edges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS derivation_edges_site_isolation ON observer.derivation_edges;
CREATE POLICY derivation_edges_site_isolation ON observer.derivation_edges
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.consent FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS consent_site_isolation ON observer.consent;
CREATE POLICY consent_site_isolation ON observer.consent
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.legal_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.legal_holds FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legal_holds_site_isolation ON observer.legal_holds;
CREATE POLICY legal_holds_site_isolation ON observer.legal_holds
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.deletion_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.deletion_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS deletion_receipts_site_isolation ON observer.deletion_receipts;
CREATE POLICY deletion_receipts_site_isolation ON observer.deletion_receipts
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT USAGE ON SCHEMA observer TO gbos_observer_app;
GRANT SELECT, INSERT ON
    observer.raw_objects,
    observer.observation_events,
    observer.participants,
    observer.evidence_refs,
    observer.event_evidence,
    observer.processor_runs,
    observer.derivation_edges,
    observer.deletion_receipts
TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON
    observer.manual_import_jobs,
    observer.checkpoints,
    observer.quarantine,
    observer.dead_letter,
    observer.consent,
    observer.legal_holds
TO gbos_observer_app;
