CREATE TABLE IF NOT EXISTS agent_runtime.action_proposals (
    site_id text NOT NULL,
    proposal_id text NOT NULL,
    idempotency_key text NOT NULL,
    task_id text NOT NULL,
    task_attempt integer NOT NULL,
    action_type text NOT NULL,
    status text NOT NULL,
    origin text NOT NULL,
    review_status text NOT NULL,
    subject_type text NOT NULL,
    subject_ref text NOT NULL,
    subject_revision integer NOT NULL,
    evidence_refs jsonb NOT NULL,
    fact_version_refs jsonb NOT NULL,
    invocation_ids jsonb NOT NULL,
    payload_digest char(64) NOT NULL,
    bundle_digest char(64) NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, proposal_id),
    UNIQUE (site_id, idempotency_key),
    UNIQUE (site_id, task_id, task_attempt),
    FOREIGN KEY (site_id, task_id)
        REFERENCES agent_runtime.agent_tasks (site_id, task_id)
        ON DELETE RESTRICT,
    CHECK (task_attempt >= 1),
    CHECK (subject_revision >= 0),
    CHECK (
        action_type IN (
            'internal.ai_draft.propose',
            'internal.work_item.propose',
            'internal.review_case.propose',
            'internal.work_item.transition.propose'
        )
    ),
    CHECK (status = 'proposed'),
    CHECK (origin = 'AI'),
    CHECK (review_status = 'AI Draft'),
    CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0),
    CHECK (jsonb_typeof(fact_version_refs) = 'array' AND jsonb_array_length(fact_version_refs) > 0),
    CHECK (jsonb_typeof(invocation_ids) = 'array'),
    CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    CHECK (bundle_digest ~ '^[a-f0-9]{64}$'),
    CHECK (jsonb_typeof(document) = 'object')
);

CREATE TABLE IF NOT EXISTS agent_runtime.proposal_materialization_outbox (
    site_id text NOT NULL,
    materialization_id text NOT NULL,
    proposal_id text NOT NULL,
    task_id text NOT NULL,
    task_attempt integer NOT NULL,
    status text NOT NULL,
    origin text NOT NULL,
    review_status text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, materialization_id),
    UNIQUE (site_id, proposal_id),
    UNIQUE (site_id, task_id, task_attempt),
    FOREIGN KEY (site_id, proposal_id)
        REFERENCES agent_runtime.action_proposals (site_id, proposal_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (site_id, task_id)
        REFERENCES agent_runtime.agent_tasks (site_id, task_id)
        ON DELETE RESTRICT,
    CHECK (task_attempt >= 1),
    CHECK (status = 'pending'),
    CHECK (origin = 'AI'),
    CHECK (review_status = 'AI Draft')
);

CREATE OR REPLACE FUNCTION agent_runtime.prevent_agent_runtime_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'agent runtime proposal bundles are immutable'
        USING ERRCODE = '55000';
END
$$;

CREATE OR REPLACE FUNCTION agent_runtime.prevent_agent_task_payload_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_digest IS DISTINCT FROM OLD.payload_digest THEN
        RAISE EXCEPTION 'agent task payload is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS agent_tasks_payload_immutable
    ON agent_runtime.agent_tasks;
CREATE TRIGGER agent_tasks_payload_immutable
BEFORE UPDATE OF payload, payload_digest ON agent_runtime.agent_tasks
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_agent_task_payload_change();

DROP TRIGGER IF EXISTS action_proposals_immutable
    ON agent_runtime.action_proposals;
CREATE TRIGGER action_proposals_immutable
BEFORE UPDATE OR DELETE ON agent_runtime.action_proposals
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_agent_runtime_immutable_change();

DROP TRIGGER IF EXISTS proposal_materialization_outbox_immutable
    ON agent_runtime.proposal_materialization_outbox;
CREATE TRIGGER proposal_materialization_outbox_immutable
BEFORE UPDATE OR DELETE ON agent_runtime.proposal_materialization_outbox
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_agent_runtime_immutable_change();

CREATE INDEX IF NOT EXISTS action_proposals_task_attempt_idx
    ON agent_runtime.action_proposals (site_id, task_id, task_attempt);

CREATE INDEX IF NOT EXISTS proposal_materialization_pending_idx
    ON agent_runtime.proposal_materialization_outbox (
        site_id,
        status,
        created_at,
        materialization_id
    );

ALTER TABLE agent_runtime.action_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.action_proposals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS action_proposals_site_isolation
    ON agent_runtime.action_proposals;
CREATE POLICY action_proposals_site_isolation
    ON agent_runtime.action_proposals
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE agent_runtime.proposal_materialization_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.proposal_materialization_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS proposal_materialization_outbox_site_isolation
    ON agent_runtime.proposal_materialization_outbox;
CREATE POLICY proposal_materialization_outbox_site_isolation
    ON agent_runtime.proposal_materialization_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, INSERT ON
    agent_runtime.action_proposals,
    agent_runtime.proposal_materialization_outbox
TO gbos_agent_app;
