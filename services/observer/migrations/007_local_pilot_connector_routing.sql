ALTER TABLE observer.connector_instances
    ADD COLUMN IF NOT EXISTS team_ref text,
    ADD COLUMN IF NOT EXISTS agent_task_type text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'connector_instances_team_ref_safe_ck'
          AND conrelid = 'observer.connector_instances'::regclass
    ) THEN
        ALTER TABLE observer.connector_instances
            ADD CONSTRAINT connector_instances_team_ref_safe_ck
            CHECK (
                team_ref IS NULL
                OR (
                    char_length(team_ref) BETWEEN 1 AND 256
                    AND team_ref !~ E'[\r\n]'
                )
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'connector_instances_agent_task_type_ck'
          AND conrelid = 'observer.connector_instances'::regclass
    ) THEN
        ALTER TABLE observer.connector_instances
            ADD CONSTRAINT connector_instances_agent_task_type_ck
            CHECK (
                agent_task_type IS NULL
                OR agent_task_type IN (
                    'sales', 'purchase', 'product_sample', 'ceo'
                )
            ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'connector_instances_agent_requires_team_ck'
          AND conrelid = 'observer.connector_instances'::regclass
    ) THEN
        ALTER TABLE observer.connector_instances
            ADD CONSTRAINT connector_instances_agent_requires_team_ck
            CHECK (agent_task_type IS NULL OR team_ref IS NOT NULL) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.connector_instances
    VALIDATE CONSTRAINT connector_instances_team_ref_safe_ck,
    VALIDATE CONSTRAINT connector_instances_agent_task_type_ck,
    VALIDATE CONSTRAINT connector_instances_agent_requires_team_ck;

CREATE INDEX IF NOT EXISTS connector_instances_routing_idx
    ON observer.connector_instances (
        site_id, team_ref, agent_task_type, connector, connector_instance_id
    )
    WHERE team_ref IS NOT NULL;

ALTER TABLE observer.connector_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_instances FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS connector_instances_site_isolation
    ON observer.connector_instances;
CREATE POLICY connector_instances_site_isolation
    ON observer.connector_instances
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.connector_instances FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON observer.connector_instances TO gbos_observer_app;
