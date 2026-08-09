DROP TRIGGER IF EXISTS proposal_materialization_outbox_immutable
    ON agent_runtime.proposal_materialization_outbox;
DROP TRIGGER IF EXISTS proposal_materialization_outbox_delete_immutable
    ON agent_runtime.proposal_materialization_outbox;
CREATE TRIGGER proposal_materialization_outbox_delete_immutable
BEFORE DELETE ON agent_runtime.proposal_materialization_outbox
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_agent_runtime_immutable_change();

ALTER TABLE agent_runtime.proposal_materialization_outbox
    DROP CONSTRAINT IF EXISTS proposal_materialization_outbox_status_check;

ALTER TABLE agent_runtime.proposal_materialization_outbox
    ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error_code text,
    ADD COLUMN IF NOT EXISTS receipt_doctype text,
    ADD COLUMN IF NOT EXISTS receipt_name text,
    ADD COLUMN IF NOT EXISTS receipt_revision integer,
    ADD COLUMN IF NOT EXISTS receipt_request_id text,
    ADD COLUMN IF NOT EXISTS receipt_digest char(64),
    ADD COLUMN IF NOT EXISTS updated_at timestamptz;

UPDATE agent_runtime.proposal_materialization_outbox
SET next_attempt_at = COALESCE(next_attempt_at, created_at),
    updated_at = COALESCE(updated_at, created_at)
WHERE next_attempt_at IS NULL OR updated_at IS NULL;

ALTER TABLE agent_runtime.proposal_materialization_outbox
    ALTER COLUMN next_attempt_at SET NOT NULL,
    ALTER COLUMN updated_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'agent_runtime.proposal_materialization_outbox'::regclass
          AND conname = 'proposal_materialization_outbox_state_check'
    ) THEN
        ALTER TABLE agent_runtime.proposal_materialization_outbox
            ADD CONSTRAINT proposal_materialization_outbox_state_check
            CHECK (
                status IN ('pending', 'running', 'succeeded', 'retry', 'dead_letter')
                AND attempt >= 0
                AND max_attempts >= 1
                AND attempt <= max_attempts
                AND (
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
                )
                AND (
                    (
                        status = 'succeeded'
                        AND receipt_doctype IS NOT NULL
                        AND receipt_name IS NOT NULL
                        AND receipt_revision >= 0
                        AND receipt_request_id IS NOT NULL
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
                )
                AND (
                    last_error_code IS NULL
                    OR last_error_code ~ '^[a-z][a-z0-9_]{0,79}$'
                )
            );
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION agent_runtime.prevent_materialization_identity_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.materialization_id IS DISTINCT FROM OLD.materialization_id
       OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
       OR NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.task_attempt IS DISTINCT FROM OLD.task_attempt
       OR NEW.origin IS DISTINCT FROM OLD.origin
       OR NEW.review_status IS DISTINCT FROM OLD.review_status
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts THEN
        RAISE EXCEPTION 'materialization outbox identity is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS proposal_materialization_outbox_identity_immutable
    ON agent_runtime.proposal_materialization_outbox;
CREATE TRIGGER proposal_materialization_outbox_identity_immutable
BEFORE UPDATE ON agent_runtime.proposal_materialization_outbox
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_materialization_identity_change();

CREATE OR REPLACE FUNCTION agent_runtime.enforce_materialization_state_transition()
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
            AND NEW.status IN ('succeeded', 'retry', 'dead_letter')
            AND NEW.attempt = OLD.attempt
        )
    ) THEN
        RAISE EXCEPTION 'invalid materialization state transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS proposal_materialization_outbox_state_transition
    ON agent_runtime.proposal_materialization_outbox;
CREATE TRIGGER proposal_materialization_outbox_state_transition
BEFORE UPDATE ON agent_runtime.proposal_materialization_outbox
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.enforce_materialization_state_transition();

DROP TRIGGER IF EXISTS action_proposals_immutable
    ON agent_runtime.action_proposals;
CREATE TRIGGER action_proposals_immutable
BEFORE UPDATE OR DELETE ON agent_runtime.action_proposals
FOR EACH ROW
EXECUTE FUNCTION agent_runtime.prevent_agent_runtime_immutable_change();

DROP INDEX IF EXISTS agent_runtime.proposal_materialization_pending_idx;
CREATE INDEX IF NOT EXISTS proposal_materialization_claim_idx
    ON agent_runtime.proposal_materialization_outbox (
        site_id,
        status,
        next_attempt_at,
        created_at,
        materialization_id
    )
    WHERE status IN ('pending', 'retry', 'running');

ALTER TABLE agent_runtime.proposal_materialization_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.proposal_materialization_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS proposal_materialization_outbox_site_isolation
    ON agent_runtime.proposal_materialization_outbox;
CREATE POLICY proposal_materialization_outbox_site_isolation
    ON agent_runtime.proposal_materialization_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON agent_runtime.proposal_materialization_outbox FROM gbos_agent_app;
GRANT SELECT, INSERT, UPDATE
    ON agent_runtime.proposal_materialization_outbox
    TO gbos_agent_app;

REVOKE UPDATE, DELETE ON agent_runtime.action_proposals FROM gbos_agent_app;
