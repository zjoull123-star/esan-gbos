DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fact_proposals_version_required'
          AND conrelid = 'context.fact_proposals'::regclass
    ) THEN
        ALTER TABLE context.fact_proposals
            ADD CONSTRAINT fact_proposals_version_required
            CHECK (
                proposal_version IS NOT NULL
                AND btrim(proposal_version) <> ''
            )
            NOT VALID;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS verified_facts_subject_predicate_version_uq
    ON context.verified_facts (
        site_id,
        subject_ref,
        predicate,
        fact_version
    );
