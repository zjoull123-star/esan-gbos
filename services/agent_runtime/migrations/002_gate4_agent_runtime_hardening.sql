DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'agent_tasks_parent_not_self'
          AND conrelid = 'agent_runtime.agent_tasks'::regclass
    ) THEN
        ALTER TABLE agent_runtime.agent_tasks
            ADD CONSTRAINT agent_tasks_parent_not_self
            CHECK (parent_task_id IS NULL OR parent_task_id <> task_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'agent_tasks_parent_same_site_fk'
          AND conrelid = 'agent_runtime.agent_tasks'::regclass
    ) THEN
        ALTER TABLE agent_runtime.agent_tasks
            ADD CONSTRAINT agent_tasks_parent_same_site_fk
            FOREIGN KEY (site_id, parent_task_id)
            REFERENCES agent_runtime.agent_tasks (site_id, task_id)
            ON DELETE RESTRICT;
    END IF;
END
$$;
