CREATE SCHEMA IF NOT EXISTS context;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_context_app') THEN
        CREATE ROLE gbos_context_app
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

ALTER ROLE gbos_context_app
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS context.evidence_records (
    site_id text NOT NULL,
    evidence_record_id text NOT NULL,
    observer_evidence_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    review_status text NOT NULL,
    data_classification text NOT NULL,
    document jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, evidence_record_id),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, observer_evidence_id)
        REFERENCES observer.evidence_refs (site_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS context.fact_proposals (
    site_id text NOT NULL,
    fact_proposal_record_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    status text NOT NULL DEFAULT 'proposed' CHECK (status = 'proposed'),
    subject_ref text NOT NULL,
    predicate text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    document jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, fact_proposal_record_id),
    UNIQUE (site_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS context.fact_evidence (
    site_id text NOT NULL,
    fact_proposal_record_id text NOT NULL,
    evidence_record_id text NOT NULL,
    PRIMARY KEY (site_id, fact_proposal_record_id, evidence_record_id),
    FOREIGN KEY (site_id, fact_proposal_record_id)
        REFERENCES context.fact_proposals (site_id, fact_proposal_record_id)
        ON DELETE CASCADE,
    FOREIGN KEY (site_id, evidence_record_id)
        REFERENCES context.evidence_records (site_id, evidence_record_id)
);

CREATE TABLE IF NOT EXISTS context.entity_resolution_proposals (
    site_id text NOT NULL,
    entity_resolution_proposal_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    status text NOT NULL DEFAULT 'proposed' CHECK (status = 'proposed'),
    entity_type text NOT NULL,
    source_entity_ref text NOT NULL,
    document jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, entity_resolution_proposal_id),
    UNIQUE (site_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS context.candidates (
    site_id text NOT NULL,
    entity_resolution_proposal_id text NOT NULL,
    candidate_id text NOT NULL,
    entity_ref text NOT NULL,
    confidence double precision NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    matching_attributes jsonb NOT NULL,
    PRIMARY KEY (site_id, entity_resolution_proposal_id, candidate_id),
    FOREIGN KEY (site_id, entity_resolution_proposal_id)
        REFERENCES context.entity_resolution_proposals
            (site_id, entity_resolution_proposal_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS context.restrictions (
    site_id text NOT NULL,
    restriction_id text NOT NULL,
    evidence_record_id text NOT NULL,
    restriction_type text NOT NULL,
    reason_code text NOT NULL,
    effective_at timestamptz NOT NULL,
    released_at timestamptz,
    PRIMARY KEY (site_id, restriction_id),
    FOREIGN KEY (site_id, evidence_record_id)
        REFERENCES context.evidence_records (site_id, evidence_record_id)
);

CREATE TABLE IF NOT EXISTS context.inbox_messages (
    site_id text NOT NULL,
    inbox_message_id text NOT NULL,
    processing_purpose text NOT NULL,
    record_kind text NOT NULL CHECK (
        record_kind IN (
            'evidence_record',
            'fact_proposal',
            'entity_resolution_proposal'
        )
    ),
    record_id text NOT NULL,
    payload_digest char(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    PRIMARY KEY (site_id, inbox_message_id)
);

ALTER TABLE context.evidence_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.evidence_records FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS evidence_records_site_isolation ON context.evidence_records;
CREATE POLICY evidence_records_site_isolation ON context.evidence_records
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.fact_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.fact_proposals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS fact_proposals_site_isolation ON context.fact_proposals;
CREATE POLICY fact_proposals_site_isolation ON context.fact_proposals
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.fact_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.fact_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS fact_evidence_site_isolation ON context.fact_evidence;
CREATE POLICY fact_evidence_site_isolation ON context.fact_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.entity_resolution_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.entity_resolution_proposals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_resolution_proposals_site_isolation ON context.entity_resolution_proposals;
CREATE POLICY entity_resolution_proposals_site_isolation
    ON context.entity_resolution_proposals
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.candidates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS candidates_site_isolation ON context.candidates;
CREATE POLICY candidates_site_isolation ON context.candidates
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.restrictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.restrictions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS restrictions_site_isolation ON context.restrictions;
CREATE POLICY restrictions_site_isolation ON context.restrictions
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.inbox_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.inbox_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbox_messages_site_isolation ON context.inbox_messages;
CREATE POLICY inbox_messages_site_isolation ON context.inbox_messages
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT USAGE ON SCHEMA context TO gbos_context_app;
GRANT SELECT, INSERT ON
    context.evidence_records,
    context.fact_proposals,
    context.fact_evidence,
    context.entity_resolution_proposals,
    context.candidates
TO gbos_context_app;
GRANT SELECT, INSERT, UPDATE ON
    context.restrictions,
    context.inbox_messages
TO gbos_context_app;
