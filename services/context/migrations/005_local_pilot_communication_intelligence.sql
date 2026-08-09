CREATE SCHEMA IF NOT EXISTS context;

CREATE TABLE IF NOT EXISTS context.communication_intelligence (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    observation_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    summary_zh text NOT NULL,
    original_language text NOT NULL,
    confidence double precision NOT NULL,
    review_status text NOT NULL,
    team_ref text,
    model_name text NOT NULL,
    model_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, intelligence_id),
    UNIQUE (site_id, observation_id),
    UNIQUE (site_id, idempotency_key),
    CHECK (site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$'),
    CHECK (length(observation_id) BETWEEN 1 AND 256),
    CHECK (processing_purpose = 'observation_processing'),
    CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    CHECK (length(summary_zh) BETWEEN 1 AND 2000),
    CHECK (length(original_language) BETWEEN 1 AND 35),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (review_status = 'AI Draft'),
    CHECK (team_ref IS NULL OR length(team_ref) BETWEEN 1 AND 256),
    CHECK (model_name = 'deepseek-v4-flash'),
    CHECK (length(model_version) BETWEEN 1 AND 160)
);

CREATE TABLE IF NOT EXISTS context.communication_intelligence_evidence (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    evidence_ref text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (site_id, intelligence_id, evidence_ref),
    UNIQUE (site_id, intelligence_id, ordinal),
    FOREIGN KEY (site_id, intelligence_id)
        REFERENCES context.communication_intelligence (site_id, intelligence_id),
    CHECK (length(evidence_ref) BETWEEN 1 AND 512),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS context.communication_intelligence_invocations (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    invocation_ref text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (site_id, intelligence_id, invocation_ref),
    UNIQUE (site_id, intelligence_id, ordinal),
    FOREIGN KEY (site_id, intelligence_id)
        REFERENCES context.communication_intelligence (site_id, intelligence_id),
    CHECK (length(invocation_ref) BETWEEN 1 AND 256),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS context.communication_fact_proposals (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    fact_proposal_id text NOT NULL,
    ordinal integer NOT NULL,
    subject_ref text NOT NULL,
    predicate text NOT NULL,
    value_display text NOT NULL,
    value_type text NOT NULL,
    unit text,
    confidence double precision NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    PRIMARY KEY (site_id, intelligence_id, fact_proposal_id),
    UNIQUE (site_id, intelligence_id, ordinal),
    FOREIGN KEY (site_id, intelligence_id)
        REFERENCES context.communication_intelligence (site_id, intelligence_id),
    CHECK (length(subject_ref) BETWEEN 1 AND 512),
    CHECK (length(predicate) BETWEEN 1 AND 160),
    CHECK (length(value_display) BETWEEN 1 AND 2000),
    CHECK (length(value_type) BETWEEN 1 AND 80),
    CHECK (unit IS NULL OR length(unit) BETWEEN 1 AND 80),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (status = 'proposed'),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS context.communication_fact_evidence (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    fact_proposal_id text NOT NULL,
    evidence_ref text NOT NULL,
    PRIMARY KEY (site_id, intelligence_id, fact_proposal_id, evidence_ref),
    FOREIGN KEY (site_id, intelligence_id, fact_proposal_id)
        REFERENCES context.communication_fact_proposals
            (site_id, intelligence_id, fact_proposal_id),
    FOREIGN KEY (site_id, intelligence_id, evidence_ref)
        REFERENCES context.communication_intelligence_evidence
            (site_id, intelligence_id, evidence_ref)
);

CREATE TABLE IF NOT EXISTS context.communication_association_suggestions (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    association_suggestion_id text NOT NULL,
    ordinal integer NOT NULL,
    association_type text NOT NULL,
    target_ref text NOT NULL,
    confidence double precision NOT NULL,
    status text NOT NULL DEFAULT 'proposed',
    PRIMARY KEY (site_id, intelligence_id, association_suggestion_id),
    UNIQUE (site_id, intelligence_id, ordinal),
    FOREIGN KEY (site_id, intelligence_id)
        REFERENCES context.communication_intelligence (site_id, intelligence_id),
    CHECK (length(association_type) BETWEEN 1 AND 80),
    CHECK (length(target_ref) BETWEEN 1 AND 512),
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (status = 'proposed'),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE TABLE IF NOT EXISTS context.communication_association_evidence (
    site_id text NOT NULL,
    intelligence_id text NOT NULL,
    association_suggestion_id text NOT NULL,
    evidence_ref text NOT NULL,
    PRIMARY KEY (
        site_id,
        intelligence_id,
        association_suggestion_id,
        evidence_ref
    ),
    FOREIGN KEY (site_id, intelligence_id, association_suggestion_id)
        REFERENCES context.communication_association_suggestions
            (site_id, intelligence_id, association_suggestion_id),
    FOREIGN KEY (site_id, intelligence_id, evidence_ref)
        REFERENCES context.communication_intelligence_evidence
            (site_id, intelligence_id, evidence_ref)
);

CREATE TABLE IF NOT EXISTS context.communication_draft_outbox (
    site_id text NOT NULL,
    draft_id text NOT NULL,
    intelligence_id text NOT NULL,
    observation_id text NOT NULL,
    processing_purpose text NOT NULL,
    subject text NOT NULL,
    summary_zh text NOT NULL,
    team_ref text NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    origin text NOT NULL,
    origin_reference text NOT NULL,
    review_status text NOT NULL,
    is_official_metric boolean NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    last_error_code text,
    receipt_doctype text,
    receipt_name text,
    receipt_revision integer,
    receipt_request_id text,
    receipt_digest char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, draft_id),
    UNIQUE (site_id, intelligence_id),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, intelligence_id)
        REFERENCES context.communication_intelligence (site_id, intelligence_id),
    CHECK (length(observation_id) BETWEEN 1 AND 256),
    CHECK (processing_purpose = 'observation_processing'),
    CHECK (length(subject) BETWEEN 1 AND 140),
    CHECK (length(summary_zh) BETWEEN 1 AND 2000),
    CHECK (length(team_ref) BETWEEN 1 AND 256),
    CHECK (model_name = 'deepseek-v4-flash'),
    CHECK (length(model_version) BETWEEN 1 AND 160),
    CHECK (origin = 'AI'),
    CHECK (origin_reference = observation_id),
    CHECK (review_status = 'AI Draft'),
    CHECK (is_official_metric = FALSE),
    CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    CHECK (status IN ('pending', 'running', 'retry', 'succeeded', 'dead_letter')),
    CHECK (attempt >= 0 AND attempt <= max_attempts),
    CHECK (max_attempts >= 1 AND max_attempts <= 5),
    CHECK (
        (
            status = 'running'
            AND attempt >= 1
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR
        (
            status <> 'running'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        (
            status = 'succeeded'
            AND receipt_doctype = 'GBOS Informal Observation'
            AND receipt_name IS NOT NULL
            AND receipt_revision >= 0
            AND receipt_request_id = draft_id
            AND receipt_digest ~ '^[a-f0-9]{64}$'
        )
        OR
        (
            status <> 'succeeded'
            AND receipt_doctype IS NULL
            AND receipt_name IS NULL
            AND receipt_revision IS NULL
            AND receipt_request_id IS NULL
            AND receipt_digest IS NULL
        )
    ),
    CHECK (
        last_error_code IS NULL
        OR last_error_code ~ '^[a-z][a-z0-9_]{0,79}$'
    ),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS context.communication_draft_evidence (
    site_id text NOT NULL,
    draft_id text NOT NULL,
    evidence_ref text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (site_id, draft_id, evidence_ref),
    UNIQUE (site_id, draft_id, ordinal),
    FOREIGN KEY (site_id, draft_id)
        REFERENCES context.communication_draft_outbox (site_id, draft_id),
    CHECK (length(evidence_ref) BETWEEN 1 AND 512),
    CHECK (ordinal BETWEEN 1 AND 100)
);

CREATE INDEX IF NOT EXISTS communication_draft_claim_idx
    ON context.communication_draft_outbox (
        site_id,
        status,
        next_attempt_at,
        created_at,
        draft_id
    )
    WHERE status IN ('pending', 'retry', 'running');

CREATE OR REPLACE FUNCTION context.prevent_communication_draft_identity_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.draft_id IS DISTINCT FROM OLD.draft_id
       OR NEW.intelligence_id IS DISTINCT FROM OLD.intelligence_id
       OR NEW.observation_id IS DISTINCT FROM OLD.observation_id
       OR NEW.processing_purpose IS DISTINCT FROM OLD.processing_purpose
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.summary_zh IS DISTINCT FROM OLD.summary_zh
       OR NEW.team_ref IS DISTINCT FROM OLD.team_ref
       OR NEW.model_name IS DISTINCT FROM OLD.model_name
       OR NEW.model_version IS DISTINCT FROM OLD.model_version
       OR NEW.origin IS DISTINCT FROM OLD.origin
       OR NEW.origin_reference IS DISTINCT FROM OLD.origin_reference
       OR NEW.review_status IS DISTINCT FROM OLD.review_status
       OR NEW.is_official_metric IS DISTINCT FROM OLD.is_official_metric
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.payload_digest IS DISTINCT FROM OLD.payload_digest
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'communication draft identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS communication_draft_identity_immutable
    ON context.communication_draft_outbox;
CREATE TRIGGER communication_draft_identity_immutable
BEFORE UPDATE ON context.communication_draft_outbox
FOR EACH ROW
EXECUTE FUNCTION context.prevent_communication_draft_identity_change();

CREATE OR REPLACE FUNCTION context.enforce_communication_draft_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT (
        (
            OLD.status IN ('pending', 'retry')
            AND NEW.status = 'running'
            AND NEW.attempt = OLD.attempt + 1
        )
        OR
        (
            OLD.status = 'running'
            AND NEW.status = 'running'
            AND NEW.attempt = OLD.attempt + 1
        )
        OR
        (
            OLD.status = 'running'
            AND NEW.status IN ('retry', 'succeeded', 'dead_letter')
            AND NEW.attempt = OLD.attempt
        )
        OR
        (
            OLD.status = 'running'
            AND NEW.status = 'running'
            AND NEW.attempt = OLD.attempt
            AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
            AND NEW.lease_expires_at > OLD.lease_expires_at
            AND NEW.next_attempt_at IS NOT DISTINCT FROM OLD.next_attempt_at
            AND NEW.last_error_code IS NOT DISTINCT FROM OLD.last_error_code
            AND NEW.receipt_doctype IS NOT DISTINCT FROM OLD.receipt_doctype
            AND NEW.receipt_name IS NOT DISTINCT FROM OLD.receipt_name
            AND NEW.receipt_revision IS NOT DISTINCT FROM OLD.receipt_revision
            AND NEW.receipt_request_id IS NOT DISTINCT FROM OLD.receipt_request_id
            AND NEW.receipt_digest IS NOT DISTINCT FROM OLD.receipt_digest
            AND NEW.updated_at >= OLD.updated_at
        )
    ) THEN
        RAISE EXCEPTION 'invalid communication draft state transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS communication_draft_state_transition
    ON context.communication_draft_outbox;
CREATE TRIGGER communication_draft_state_transition
BEFORE UPDATE ON context.communication_draft_outbox
FOR EACH ROW
EXECUTE FUNCTION context.enforce_communication_draft_transition();

ALTER TABLE context.communication_intelligence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_intelligence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_intelligence_site_isolation
    ON context.communication_intelligence;
CREATE POLICY communication_intelligence_site_isolation
    ON context.communication_intelligence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_intelligence_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_intelligence_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_intelligence_evidence_site_isolation
    ON context.communication_intelligence_evidence;
CREATE POLICY communication_intelligence_evidence_site_isolation
    ON context.communication_intelligence_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_intelligence_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_intelligence_invocations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_intelligence_invocations_site_isolation
    ON context.communication_intelligence_invocations;
CREATE POLICY communication_intelligence_invocations_site_isolation
    ON context.communication_intelligence_invocations
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_fact_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_fact_proposals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_fact_proposals_site_isolation
    ON context.communication_fact_proposals;
CREATE POLICY communication_fact_proposals_site_isolation
    ON context.communication_fact_proposals
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_fact_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_fact_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_fact_evidence_site_isolation
    ON context.communication_fact_evidence;
CREATE POLICY communication_fact_evidence_site_isolation
    ON context.communication_fact_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_association_suggestions ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_association_suggestions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_association_suggestions_site_isolation
    ON context.communication_association_suggestions;
CREATE POLICY communication_association_suggestions_site_isolation
    ON context.communication_association_suggestions
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_association_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_association_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_association_evidence_site_isolation
    ON context.communication_association_evidence;
CREATE POLICY communication_association_evidence_site_isolation
    ON context.communication_association_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_draft_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_draft_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_draft_outbox_site_isolation
    ON context.communication_draft_outbox;
CREATE POLICY communication_draft_outbox_site_isolation
    ON context.communication_draft_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.communication_draft_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.communication_draft_evidence FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS communication_draft_evidence_site_isolation
    ON context.communication_draft_evidence;
CREATE POLICY communication_draft_evidence_site_isolation
    ON context.communication_draft_evidence
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON
    context.communication_intelligence,
    context.communication_intelligence_evidence,
    context.communication_intelligence_invocations,
    context.communication_fact_proposals,
    context.communication_fact_evidence,
    context.communication_association_suggestions,
    context.communication_association_evidence,
    context.communication_draft_outbox,
    context.communication_draft_evidence
FROM gbos_context_app;

GRANT SELECT, INSERT ON
    context.communication_intelligence,
    context.communication_intelligence_evidence,
    context.communication_intelligence_invocations,
    context.communication_fact_proposals,
    context.communication_fact_evidence,
    context.communication_association_suggestions,
    context.communication_association_evidence,
    context.communication_draft_evidence
TO gbos_context_app;

GRANT SELECT, INSERT ON context.communication_draft_outbox TO gbos_context_app;
GRANT UPDATE (
    status,
    attempt,
    next_attempt_at,
    lease_owner,
    lease_expires_at,
    last_error_code,
    receipt_doctype,
    receipt_name,
    receipt_revision,
    receipt_request_id,
    receipt_digest,
    updated_at
) ON context.communication_draft_outbox TO gbos_context_app;
