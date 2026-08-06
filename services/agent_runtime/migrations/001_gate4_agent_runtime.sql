CREATE SCHEMA IF NOT EXISTS agent_runtime;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_agent_app') THEN
        CREATE ROLE gbos_agent_app
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

ALTER ROLE gbos_agent_app
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS agent_runtime.agent_tasks (
    site_id text NOT NULL,
    task_id text NOT NULL,
    processing_purpose text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL,
    payload jsonb NOT NULL,
    agent_type text NOT NULL,
    subject_type text NOT NULL,
    subject_ref text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    due_at timestamptz NOT NULL,
    recheck_at timestamptz,
    priority smallint NOT NULL,
    attempt smallint NOT NULL DEFAULT 0,
    max_attempts smallint NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    output_artifact_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    failure_classification text,
    parent_task_id text,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, task_id),
    UNIQUE (site_id, idempotency_key),
    CHECK (priority BETWEEN 0 AND 100),
    CHECK (attempt >= 0 AND max_attempts BETWEEN 1 AND 100 AND attempt <= max_attempts),
    CHECK (
        (
            status IN ('leased', 'running')
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR
        (
            status NOT IN ('leased', 'running')
            AND lease_owner IS NULL AND lease_expires_at IS NULL
        )
    ),
    CHECK (
        status IN (
            'queued',
            'leased',
            'running',
            'recheck',
            'succeeded',
            'failed',
            'dead_letter',
            'cancelled'
        )
    ),
    CHECK (
        failure_classification IS NULL
        OR failure_classification IN (
            'budget_exhausted',
            'policy_denied',
            'tool_failure',
            'invalid_output',
            'dependency',
            'internal'
        )
    )
);

CREATE TABLE IF NOT EXISTS agent_runtime.timeline (
    site_id text NOT NULL,
    task_id text NOT NULL,
    sequence bigint NOT NULL,
    timeline_event_id text NOT NULL,
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    actor_type text NOT NULL,
    actor_ref text,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    PRIMARY KEY (site_id, task_id, sequence),
    UNIQUE (site_id, timeline_event_id),
    FOREIGN KEY (site_id, task_id) REFERENCES agent_runtime.agent_tasks (site_id, task_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS agent_runtime.dead_letter (
    site_id text NOT NULL,
    task_id text NOT NULL,
    dead_letter_id text NOT NULL,
    attempts smallint NOT NULL,
    failure_classification text NOT NULL,
    reason_code text NOT NULL,
    dead_lettered_at timestamptz NOT NULL,
    causation_id text NOT NULL,
    correlation_id text NOT NULL,
    PRIMARY KEY (site_id, task_id),
    UNIQUE (site_id, dead_letter_id),
    FOREIGN KEY (site_id, task_id) REFERENCES agent_runtime.agent_tasks (site_id, task_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS agent_tasks_claim_order_idx
    ON agent_runtime.agent_tasks (site_id, priority DESC, due_at ASC, created_at ASC, task_id ASC)
    WHERE status IN ('queued', 'recheck');

CREATE INDEX IF NOT EXISTS agent_tasks_expired_lease_idx
    ON agent_runtime.agent_tasks (site_id, lease_expires_at ASC, task_id ASC)
    WHERE status IN ('leased', 'running');

ALTER TABLE agent_runtime.agent_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.agent_tasks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_tasks_site_isolation ON agent_runtime.agent_tasks;
CREATE POLICY agent_tasks_site_isolation ON agent_runtime.agent_tasks
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE agent_runtime.timeline ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.timeline FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS timeline_site_isolation ON agent_runtime.timeline;
CREATE POLICY timeline_site_isolation ON agent_runtime.timeline
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE agent_runtime.dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.dead_letter FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dead_letter_site_isolation ON agent_runtime.dead_letter;
CREATE POLICY dead_letter_site_isolation ON agent_runtime.dead_letter
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT USAGE ON SCHEMA agent_runtime TO gbos_agent_app;
GRANT SELECT, INSERT, UPDATE ON
    agent_runtime.agent_tasks,
    agent_runtime.timeline,
    agent_runtime.dead_letter
TO gbos_agent_app;
