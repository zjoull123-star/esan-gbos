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
        RAISE EXCEPTION 'invalid materialization state transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;
