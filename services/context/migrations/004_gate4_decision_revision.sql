ALTER TABLE context.decisions
    DROP CONSTRAINT IF EXISTS decisions_site_id_decision_id_key;

CREATE INDEX IF NOT EXISTS decisions_latest_revision_idx
    ON context.decisions (
        site_id,
        decision_id,
        decision_revision DESC
    );
