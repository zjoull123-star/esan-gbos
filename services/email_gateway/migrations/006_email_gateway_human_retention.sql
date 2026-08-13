-- Human-operations retention remains a Gateway metadata/content-reference
-- workflow. Observer alone owns raw EML/body/attachment/final-MIME CAS expiry,
-- legal-hold authority, and durable tombstones.

ALTER TABLE email_gateway.reply_drafts
    ADD COLUMN IF NOT EXISTS terminal_at timestamptz,
    ADD COLUMN IF NOT EXISTS content_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS observer_tombstone_receipt_ref text,
    ADD COLUMN IF NOT EXISTS legal_hold_ref text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname = 'reply_drafts_terminal_retention_window'
           AND conrelid = 'email_gateway.reply_drafts'::regclass
    ) THEN
        ALTER TABLE email_gateway.reply_drafts
            ADD CONSTRAINT reply_drafts_terminal_retention_window CHECK (
                (
                    state = 'editable'
                    AND terminal_at IS NULL
                    AND content_expires_at IS NULL
                    AND observer_tombstone_receipt_ref IS NULL
                )
                OR (
                    state IN ('discarded', 'terminal')
                    AND terminal_at IS NOT NULL
                    AND content_expires_at = terminal_at + interval '30 days'
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE email_gateway.retention_runs
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5;

CREATE UNIQUE INDEX IF NOT EXISTS retention_runs_idempotency
    ON email_gateway.retention_runs (site_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint
         WHERE conname = 'retention_runs_max_attempts_bounded'
           AND conrelid = 'email_gateway.retention_runs'::regclass
    ) THEN
        ALTER TABLE email_gateway.retention_runs
            ADD CONSTRAINT retention_runs_max_attempts_bounded
            CHECK (max_attempts BETWEEN 1 AND 5) NOT VALID;
    END IF;
END
$$;

ALTER TABLE email_gateway.content_expiration_receipts
    ADD COLUMN IF NOT EXISTS run_ref text,
    ADD COLUMN IF NOT EXISTS legal_hold_checked_at timestamptz;

CREATE TABLE IF NOT EXISTS email_gateway.worker_heartbeats (
    site_id text NOT NULL,
    worker_kind text NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    lease_generation bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, worker_kind),
    CHECK (worker_kind IN (
        'publication', 'mailbox_config_projection', 'identity', 'routing', 'retention'
    )),
    CHECK (lease_generation >= 0),
    CHECK (updated_at >= heartbeat_at)
);

ALTER TABLE email_gateway.worker_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.worker_heartbeats FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.worker_heartbeats FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.worker_heartbeats;
CREATE POLICY email_gateway_site_scope ON email_gateway.worker_heartbeats
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

CREATE OR REPLACE FUNCTION email_gateway.claim_human_retention_run(
    p_site_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_seconds integer
)
RETURNS TABLE (
    run_ref text,
    attempt integer,
    lease_generation bigint,
    lease_expires_at timestamptz,
    dry_run boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_worker_id IS NULL
       OR p_worker_id = ''
       OR p_worker_id LIKE '%@%'
       OR p_lease_seconds NOT BETWEEN 1 AND 300 THEN
        RAISE EXCEPTION 'retention claim rejected';
    END IF;

    RETURN QUERY
    WITH candidate AS (
        SELECT pending.site_id, pending.run_ref
          FROM email_gateway.retention_runs AS pending
         WHERE pending.site_id = p_site_id
           AND pending.status IN ('queued', 'retry')
           AND pending.next_attempt_at <= p_now
         ORDER BY pending.next_attempt_at, pending.created_at, pending.run_ref
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    )
    UPDATE email_gateway.retention_runs AS claimed
       SET status = 'leased',
           attempt = claimed.attempt + 1,
           lease_owner = p_worker_id,
           lease_expires_at = p_now + (p_lease_seconds * interval '1 second'),
           lease_generation = claimed.lease_generation + 1,
           safe_error_code = NULL,
           started_at = COALESCE(claimed.started_at, p_now),
           updated_at = p_now
      FROM candidate
     WHERE claimed.site_id = candidate.site_id
       AND claimed.run_ref = candidate.run_ref
    RETURNING claimed.run_ref, claimed.attempt, claimed.lease_generation,
              claimed.lease_expires_at, claimed.dry_run;
END
$$;

CREATE OR REPLACE FUNCTION email_gateway.heartbeat_human_retention_run(
    p_site_id text,
    p_run_ref text,
    p_worker_id text,
    p_attempt integer,
    p_lease_generation bigint,
    p_now timestamptz,
    p_lease_seconds integer
)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_lease_expires_at timestamptz;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_lease_seconds NOT BETWEEN 1 AND 300 THEN
        RAISE EXCEPTION 'retention heartbeat rejected';
    END IF;

    UPDATE email_gateway.retention_runs AS run
       SET lease_expires_at = p_now + (p_lease_seconds * interval '1 second'),
           updated_at = p_now
     WHERE run.site_id = p_site_id
       AND run.run_ref = p_run_ref
       AND run.status = 'leased'
       AND run.lease_owner = p_worker_id
       AND run.attempt = p_attempt
       AND run.lease_generation = p_lease_generation
       AND run.lease_expires_at >= p_now
    RETURNING run.lease_expires_at INTO v_lease_expires_at;

    IF v_lease_expires_at IS NULL THEN
        RAISE EXCEPTION 'retention lease fence conflict';
    END IF;
    RETURN v_lease_expires_at;
END
$$;

CREATE OR REPLACE FUNCTION email_gateway.record_email_gateway_worker_heartbeat(
    p_site_id text,
    p_worker_kind text,
    p_heartbeat_at timestamptz
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_generation bigint;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_worker_kind NOT IN (
           'publication', 'mailbox_config_projection', 'identity', 'routing', 'retention'
       ) THEN
        RAISE EXCEPTION 'worker heartbeat rejected';
    END IF;

    INSERT INTO email_gateway.worker_heartbeats (
        site_id, worker_kind, heartbeat_at, lease_generation, updated_at
    ) VALUES (p_site_id, p_worker_kind, p_heartbeat_at, 1, p_heartbeat_at)
    ON CONFLICT (site_id, worker_kind) DO UPDATE
       SET heartbeat_at = EXCLUDED.heartbeat_at,
           lease_generation = email_gateway.worker_heartbeats.lease_generation + 1,
           updated_at = EXCLUDED.updated_at
     WHERE email_gateway.worker_heartbeats.heartbeat_at <= EXCLUDED.heartbeat_at
    RETURNING lease_generation INTO v_generation;

    IF v_generation IS NULL THEN
        RAISE EXCEPTION 'worker heartbeat clock regression';
    END IF;
    RETURN v_generation;
END
$$;

REVOKE ALL ON FUNCTION email_gateway.claim_human_retention_run(
    text, text, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.heartbeat_human_retention_run(
    text, text, text, integer, bigint, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.record_email_gateway_worker_heartbeat(
    text, text, timestamptz
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION email_gateway.claim_human_retention_run(
    text, text, timestamptz, integer
) TO gbos_email_gateway_worker;
GRANT EXECUTE ON FUNCTION email_gateway.heartbeat_human_retention_run(
    text, text, text, integer, bigint, timestamptz, integer
) TO gbos_email_gateway_worker;
GRANT EXECUTE ON FUNCTION email_gateway.record_email_gateway_worker_heartbeat(
    text, text, timestamptz
) TO gbos_email_gateway_worker;

GRANT SELECT ON email_gateway.worker_heartbeats TO gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.worker_heartbeats
    TO gbos_email_gateway_worker;
