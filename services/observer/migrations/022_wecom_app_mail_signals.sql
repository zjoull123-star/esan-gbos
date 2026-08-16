CREATE TABLE IF NOT EXISTS observer.email_signals (
    site_id text NOT NULL,
    signal_ref text NOT NULL
        CHECK (signal_ref ~ '^ESG-[0-9A-HJKMNP-TV-Z]{26}$'),
    signal_kind text NOT NULL CHECK (signal_kind IN ('callback', 'reconciliation')),
    connector text NOT NULL DEFAULT 'email' CHECK (connector = 'email'),
    connector_instance_id text NOT NULL
        CHECK (connector_instance_id ~ '^OCI-[0-9A-HJKMNP-TV-Z]{26}$'),
    mailbox_id text NOT NULL CHECK (mailbox_id ~ '^MBX-[0-9A-HJKMNP-TV-Z]{26}$'),
    mailbox_config_revision bigint NOT NULL CHECK (mailbox_config_revision >= 1),
    activation_not_before timestamptz NOT NULL,
    count_hint bigint CHECK (count_hint BETWEEN 0 AND 4294967295),
    callback_timestamp timestamptz,
    payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    signal_digest char(64) NOT NULL CHECK (signal_digest ~ '^[a-f0-9]{64}$'),
    nonce_digest char(64) CHECK (nonce_digest ~ '^[a-f0-9]{64}$'),
    replay_key_digest char(64) NOT NULL CHECK (replay_key_digest ~ '^[a-f0-9]{64}$'),
    idempotency_key text NOT NULL
        CHECK (idempotency_key ~ '^email-signal:[a-f0-9]{64}$'),
    accepted_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, signal_ref),
    UNIQUE (site_id, replay_key_digest),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, mailbox_id, mailbox_config_revision)
        REFERENCES observer.email_connector_config_projections (
            site_id, mailbox_id, mailbox_config_revision
        ),
    CHECK (
        (
            signal_kind = 'callback'
            AND count_hint IS NOT NULL
            AND callback_timestamp IS NOT NULL
            AND nonce_digest IS NOT NULL
        ) OR (
            signal_kind = 'reconciliation'
            AND count_hint IS NULL
            AND callback_timestamp IS NULL
            AND nonce_digest IS NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS observer.email_signal_work (
    site_id text NOT NULL,
    signal_ref text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'leased', 'retry', 'acked', 'dead_letter')),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
    max_attempts smallint NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 5),
    next_attempt_at timestamptz NOT NULL,
    worker_id text CHECK (char_length(worker_id) BETWEEN 1 AND 128),
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    heartbeat_at timestamptz,
    ack_receipt_ref text CHECK (ack_receipt_ref ~ '^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$'),
    acked_at timestamptz,
    safe_error_code text CHECK (safe_error_code ~ '^[a-z][a-z0-9_]{0,79}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, signal_ref),
    FOREIGN KEY (site_id, signal_ref)
        REFERENCES observer.email_signals (site_id, signal_ref),
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (status = 'leased' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased' AND worker_id IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'acked' AND ack_receipt_ref IS NOT NULL AND acked_at IS NOT NULL)
        OR (status <> 'acked' AND ack_receipt_ref IS NULL AND acked_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS email_signal_work_due_idx
    ON observer.email_signal_work (site_id, status, next_attempt_at, created_at);

CREATE OR REPLACE FUNCTION observer.reject_email_signal_rewrite()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'email_signal_fact_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

DROP TRIGGER IF EXISTS email_signal_fact_immutable ON observer.email_signals;
CREATE TRIGGER email_signal_fact_immutable
    BEFORE UPDATE OR DELETE ON observer.email_signals
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_signal_rewrite();

CREATE OR REPLACE FUNCTION observer.enqueue_email_signal_work(
    p_site_id text,
    p_signal_ref text,
    p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS NULL OR p_signal_ref IS NULL OR p_now IS NULL THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.set_config('app.site_id', p_site_id, true);
    INSERT INTO observer.email_signal_work (
        site_id, signal_ref, status, attempt_count, max_attempts,
        next_attempt_at, lease_generation, created_at, updated_at
    )
    SELECT p_site_id, p_signal_ref, 'queued', 0, 5, p_now, 0, p_now, p_now
      FROM observer.email_signals AS signal
     WHERE signal.site_id = p_site_id AND signal.signal_ref = p_signal_ref
    ON CONFLICT (site_id, signal_ref) DO NOTHING;
    RETURN EXISTS (
        SELECT 1 FROM observer.email_signal_work AS work
         WHERE work.site_id = p_site_id AND work.signal_ref = p_signal_ref
    );
END
$function$;

CREATE OR REPLACE FUNCTION observer.claim_email_signal(
    p_site_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_until timestamptz
) RETURNS TABLE (
    signal_ref text,
    signal_kind text,
    connector_instance_id text,
    mailbox_id text,
    mailbox_config_revision bigint,
    activation_not_before timestamptz,
    count_hint bigint,
    callback_timestamp timestamptz,
    payload_digest text,
    nonce_digest text,
    replay_key_digest text,
    idempotency_key text,
    accepted_at timestamptz,
    worker_id text,
    attempt_count smallint,
    lease_generation bigint,
    lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS NULL OR p_worker_id IS NULL
       OR char_length(p_worker_id) NOT BETWEEN 1 AND 128
       OR p_now IS NULL OR p_lease_until <= p_now
       OR p_lease_until > p_now + interval '10 minutes' THEN
        RETURN;
    END IF;
    PERFORM pg_catalog.set_config('app.site_id', p_site_id, true);
    RETURN QUERY
    WITH candidate AS (
        SELECT work.site_id, work.signal_ref
          FROM observer.email_signal_work AS work
         WHERE work.site_id = p_site_id
           AND work.attempt_count < work.max_attempts
           AND (
               (work.status IN ('queued', 'retry') AND work.next_attempt_at <= p_now)
               OR (work.status = 'leased' AND work.lease_expires_at < p_now)
           )
         ORDER BY work.next_attempt_at, work.created_at, work.signal_ref
         FOR UPDATE SKIP LOCKED
         LIMIT 1
    ), updated AS (
        UPDATE observer.email_signal_work AS work
           SET status = 'leased', worker_id = p_worker_id,
               attempt_count = work.attempt_count + 1,
               lease_generation = work.lease_generation + 1,
               lease_expires_at = p_lease_until,
               heartbeat_at = p_now, safe_error_code = NULL, updated_at = p_now
          FROM candidate
         WHERE work.site_id = candidate.site_id
           AND work.signal_ref = candidate.signal_ref
        RETURNING work.*
    )
    SELECT signal.signal_ref, signal.signal_kind, signal.connector_instance_id,
           signal.mailbox_id, signal.mailbox_config_revision,
           signal.activation_not_before, signal.count_hint, signal.callback_timestamp,
           signal.payload_digest::text, signal.nonce_digest::text,
           signal.replay_key_digest::text, signal.idempotency_key, signal.accepted_at,
           updated.worker_id, updated.attempt_count, updated.lease_generation,
           updated.lease_expires_at
      FROM updated
      JOIN observer.email_signals AS signal
        ON signal.site_id = updated.site_id AND signal.signal_ref = updated.signal_ref;
END
$function$;

CREATE OR REPLACE FUNCTION observer.heartbeat_email_signal(
    p_site_id text,
    p_signal_ref text,
    p_worker_id text,
    p_attempt_count integer,
    p_lease_generation bigint,
    p_now timestamptz,
    p_lease_until timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_lease_until <= p_now OR p_lease_until > p_now + interval '10 minutes' THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.set_config('app.site_id', p_site_id, true);
    UPDATE observer.email_signal_work AS work
       SET heartbeat_at = p_now, lease_expires_at = p_lease_until, updated_at = p_now
     WHERE work.site_id = p_site_id AND work.signal_ref = p_signal_ref
       AND work.status = 'leased' AND work.worker_id = p_worker_id
       AND work.attempt_count = p_attempt_count
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION observer.ack_email_signal(
    p_site_id text,
    p_signal_ref text,
    p_worker_id text,
    p_attempt_count integer,
    p_lease_generation bigint,
    p_ack_receipt_ref text,
    p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    PERFORM pg_catalog.set_config('app.site_id', p_site_id, true);
    UPDATE observer.email_signal_work AS work
       SET status = 'acked', worker_id = NULL, lease_expires_at = NULL,
           ack_receipt_ref = p_ack_receipt_ref, acked_at = p_now,
           heartbeat_at = p_now, updated_at = p_now
     WHERE work.site_id = p_site_id AND work.signal_ref = p_signal_ref
       AND work.status = 'leased' AND work.worker_id = p_worker_id
       AND work.attempt_count = p_attempt_count
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION observer.fail_email_signal(
    p_site_id text,
    p_signal_ref text,
    p_worker_id text,
    p_attempt_count integer,
    p_lease_generation bigint,
    p_safe_error_code text,
    p_next_attempt_at timestamptz,
    p_now timestamptz
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_safe_error_code !~ '^[a-z][a-z0-9_]{0,79}$'
       OR p_next_attempt_at < p_now
       OR p_next_attempt_at > p_now + interval '1 hour' THEN
        RETURN false;
    END IF;
    PERFORM pg_catalog.set_config('app.site_id', p_site_id, true);
    UPDATE observer.email_signal_work AS work
       SET status = CASE
               WHEN work.attempt_count >= work.max_attempts THEN 'dead_letter'
               ELSE 'retry'
           END,
           worker_id = NULL, lease_expires_at = NULL,
           next_attempt_at = p_next_attempt_at,
           safe_error_code = p_safe_error_code, updated_at = p_now
     WHERE work.site_id = p_site_id AND work.signal_ref = p_signal_ref
       AND work.status = 'leased' AND work.worker_id = p_worker_id
       AND work.attempt_count = p_attempt_count
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

ALTER TABLE observer.email_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_signals FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_signal_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_signal_work FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_signals_site_isolation ON observer.email_signals;
CREATE POLICY email_signals_site_isolation ON observer.email_signals
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));
DROP POLICY IF EXISTS email_signal_work_site_isolation ON observer.email_signal_work;
CREATE POLICY email_signal_work_site_isolation ON observer.email_signal_work
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_signals FROM PUBLIC;
REVOKE ALL ON observer.email_signal_work FROM PUBLIC;
REVOKE ALL ON observer.email_signals FROM gbos_observer_app;
REVOKE ALL ON observer.email_signal_work FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_signals TO gbos_observer_app;
GRANT SELECT ON observer.email_signal_work TO gbos_observer_app;

REVOKE ALL ON FUNCTION observer.reject_email_signal_rewrite() FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.enqueue_email_signal_work(text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.claim_email_signal(text, text, timestamptz, timestamptz)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.heartbeat_email_signal(
    text, text, text, integer, bigint, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.ack_email_signal(
    text, text, text, integer, bigint, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.fail_email_signal(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION observer.enqueue_email_signal_work(text, text, timestamptz)
    TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.claim_email_signal(text, text, timestamptz, timestamptz)
    TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.heartbeat_email_signal(
    text, text, text, integer, bigint, timestamptz, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.ack_email_signal(
    text, text, text, integer, bigint, text, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.fail_email_signal(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) TO gbos_observer_app;
