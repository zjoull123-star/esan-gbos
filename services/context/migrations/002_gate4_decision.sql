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

ALTER TABLE context.fact_proposals
    ADD COLUMN IF NOT EXISTS proposal_version text
        GENERATED ALWAYS AS (document ->> 'output_version') STORED,
    ADD COLUMN IF NOT EXISTS proposal_revision integer NOT NULL DEFAULT 1
        CHECK (proposal_revision >= 1);

ALTER TABLE context.fact_proposals
    ADD CONSTRAINT fact_proposals_exact_revision_unique
    UNIQUE (
        site_id,
        fact_proposal_record_id,
        proposal_version,
        proposal_revision
    );

CREATE OR REPLACE FUNCTION context.reject_fact_proposal_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'fact proposals are immutable after insertion'
        USING ERRCODE = '55000';
END
$$;

DROP TRIGGER IF EXISTS fact_proposals_immutable ON context.fact_proposals;
CREATE TRIGGER fact_proposals_immutable
    BEFORE UPDATE OR DELETE ON context.fact_proposals
    FOR EACH ROW
    EXECUTE FUNCTION context.reject_fact_proposal_mutation();

CREATE TABLE IF NOT EXISTS context.decisions (
    site_id text NOT NULL,
    decision_id text NOT NULL,
    decision_revision integer NOT NULL CHECK (decision_revision >= 1),
    processing_purpose text NOT NULL,
    proposal_ref text NOT NULL,
    proposal_version text NOT NULL,
    proposal_revision integer NOT NULL CHECK (proposal_revision >= 1),
    decision_type text NOT NULL CHECK (decision_type IN ('human', 'rule')),
    operator_ref text NOT NULL,
    rule_version text,
    valid_start timestamptz NOT NULL,
    valid_end timestamptz,
    effective_at timestamptz NOT NULL,
    recorded_time timestamptz NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (site_id, decision_id, decision_revision),
    UNIQUE (site_id, decision_id),
    FOREIGN KEY (
        site_id,
        proposal_ref,
        proposal_version,
        proposal_revision
    ) REFERENCES context.fact_proposals (
        site_id,
        fact_proposal_record_id,
        proposal_version,
        proposal_revision
    ),
    CHECK (valid_end IS NULL OR valid_end > valid_start),
    CHECK (
        (decision_type = 'rule' AND rule_version IS NOT NULL)
        OR decision_type = 'human'
    )
);

CREATE TABLE IF NOT EXISTS context.verified_facts (
    site_id text NOT NULL,
    fact_id text NOT NULL,
    fact_version integer NOT NULL CHECK (fact_version >= 1),
    processing_purpose text NOT NULL,
    proposal_ref text NOT NULL,
    proposal_version text NOT NULL,
    proposal_revision integer NOT NULL CHECK (proposal_revision >= 1),
    subject_ref text NOT NULL,
    predicate text NOT NULL,
    valid_start timestamptz NOT NULL,
    valid_end timestamptz,
    recorded_time timestamptz NOT NULL,
    confirmation_decision_id text NOT NULL,
    confirmation_decision_revision integer NOT NULL DEFAULT 1,
    supersedes_fact_id text,
    supersedes_fact_version integer,
    document jsonb NOT NULL,
    PRIMARY KEY (site_id, fact_id, fact_version),
    UNIQUE (site_id, proposal_ref, proposal_version, proposal_revision),
    FOREIGN KEY (
        site_id,
        proposal_ref,
        proposal_version,
        proposal_revision
    ) REFERENCES context.fact_proposals (
        site_id,
        fact_proposal_record_id,
        proposal_version,
        proposal_revision
    ),
    FOREIGN KEY (
        site_id,
        confirmation_decision_id,
        confirmation_decision_revision
    ) REFERENCES context.decisions (
        site_id,
        decision_id,
        decision_revision
    ),
    FOREIGN KEY (
        site_id,
        supersedes_fact_id,
        supersedes_fact_version
    ) REFERENCES context.verified_facts (site_id, fact_id, fact_version),
    CHECK (valid_end IS NULL OR valid_end > valid_start),
    CHECK (
        (fact_version = 1 AND supersedes_fact_id IS NULL
            AND supersedes_fact_version IS NULL)
        OR
        (fact_version > 1 AND supersedes_fact_id IS NOT NULL
            AND supersedes_fact_version = fact_version - 1)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS verified_facts_one_successor
    ON context.verified_facts (
        site_id,
        supersedes_fact_id,
        supersedes_fact_version
    )
    WHERE supersedes_fact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS context.decision_fact_refs (
    site_id text NOT NULL,
    decision_id text NOT NULL,
    decision_revision integer NOT NULL,
    ref_role text NOT NULL CHECK (ref_role IN ('input', 'output')),
    fact_id text NOT NULL,
    fact_version integer NOT NULL,
    PRIMARY KEY (
        site_id,
        decision_id,
        decision_revision,
        ref_role,
        fact_id,
        fact_version
    ),
    FOREIGN KEY (site_id, decision_id, decision_revision)
        REFERENCES context.decisions (site_id, decision_id, decision_revision),
    FOREIGN KEY (site_id, fact_id, fact_version)
        REFERENCES context.verified_facts (site_id, fact_id, fact_version)
);

CREATE TABLE IF NOT EXISTS context.decision_evidence_refs (
    site_id text NOT NULL,
    decision_id text NOT NULL,
    decision_revision integer NOT NULL,
    evidence_record_id text NOT NULL,
    PRIMARY KEY (
        site_id,
        decision_id,
        decision_revision,
        evidence_record_id
    ),
    FOREIGN KEY (site_id, decision_id, decision_revision)
        REFERENCES context.decisions (site_id, decision_id, decision_revision),
    FOREIGN KEY (site_id, evidence_record_id)
        REFERENCES context.evidence_records (site_id, evidence_record_id)
);

CREATE TABLE IF NOT EXISTS context.fact_evidence_refs (
    site_id text NOT NULL,
    fact_id text NOT NULL,
    fact_version integer NOT NULL,
    evidence_record_id text NOT NULL,
    PRIMARY KEY (site_id, fact_id, fact_version, evidence_record_id),
    FOREIGN KEY (site_id, fact_id, fact_version)
        REFERENCES context.verified_facts (site_id, fact_id, fact_version),
    FOREIGN KEY (site_id, evidence_record_id)
        REFERENCES context.evidence_records (site_id, evidence_record_id)
);

CREATE TABLE IF NOT EXISTS context.conflicts (
    site_id text NOT NULL,
    conflict_id text NOT NULL,
    processing_purpose text NOT NULL,
    proposal_ref text NOT NULL,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved', 'dismissed')),
    detected_at timestamptz NOT NULL,
    recorded_time timestamptz NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (site_id, conflict_id),
    FOREIGN KEY (site_id, proposal_ref)
        REFERENCES context.fact_proposals (site_id, fact_proposal_record_id)
);

CREATE TABLE IF NOT EXISTS context.conflict_fact_refs (
    site_id text NOT NULL,
    conflict_id text NOT NULL,
    fact_id text NOT NULL,
    fact_version integer NOT NULL,
    PRIMARY KEY (site_id, conflict_id, fact_id, fact_version),
    FOREIGN KEY (site_id, conflict_id)
        REFERENCES context.conflicts (site_id, conflict_id),
    FOREIGN KEY (site_id, fact_id, fact_version)
        REFERENCES context.verified_facts (site_id, fact_id, fact_version)
);

CREATE TABLE IF NOT EXISTS context.conflict_evidence_refs (
    site_id text NOT NULL,
    conflict_id text NOT NULL,
    evidence_record_id text NOT NULL,
    PRIMARY KEY (site_id, conflict_id, evidence_record_id),
    FOREIGN KEY (site_id, conflict_id)
        REFERENCES context.conflicts (site_id, conflict_id),
    FOREIGN KEY (site_id, evidence_record_id)
        REFERENCES context.evidence_records (site_id, evidence_record_id)
);

ALTER TABLE context.conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.conflicts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conflicts_site_isolation ON context.conflicts;
CREATE POLICY conflicts_site_isolation ON context.conflicts
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.verified_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.verified_facts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS verified_facts_site_isolation ON context.verified_facts;
CREATE POLICY verified_facts_site_isolation ON context.verified_facts
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.decisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decisions_site_isolation ON context.decisions;
CREATE POLICY decisions_site_isolation ON context.decisions
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.decision_fact_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.decision_fact_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decision_fact_refs_site_isolation
    ON context.decision_fact_refs;
CREATE POLICY decision_fact_refs_site_isolation ON context.decision_fact_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.decision_evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.decision_evidence_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS decision_evidence_refs_site_isolation
    ON context.decision_evidence_refs;
CREATE POLICY decision_evidence_refs_site_isolation
    ON context.decision_evidence_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.fact_evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.fact_evidence_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS fact_evidence_refs_site_isolation
    ON context.fact_evidence_refs;
CREATE POLICY fact_evidence_refs_site_isolation ON context.fact_evidence_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.conflict_fact_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.conflict_fact_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conflict_fact_refs_site_isolation
    ON context.conflict_fact_refs;
CREATE POLICY conflict_fact_refs_site_isolation ON context.conflict_fact_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE context.conflict_evidence_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE context.conflict_evidence_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conflict_evidence_refs_site_isolation
    ON context.conflict_evidence_refs;
CREATE POLICY conflict_evidence_refs_site_isolation
    ON context.conflict_evidence_refs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, INSERT ON
    context.conflicts,
    context.verified_facts,
    context.decisions,
    context.decision_fact_refs,
    context.decision_evidence_refs,
    context.fact_evidence_refs,
    context.conflict_fact_refs,
    context.conflict_evidence_refs
TO gbos_context_app;
