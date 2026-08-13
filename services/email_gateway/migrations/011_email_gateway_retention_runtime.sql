-- Durable Email Gateway draft-reference retention. Observer remains the sole
-- owner of raw content/CAS deletion and of the tombstone being verified.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
         WHERE rolname = 'gbos_email_gateway_retention_worker'
    ) THEN
        CREATE ROLE gbos_email_gateway_retention_worker NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA email_gateway TO gbos_email_gateway_retention_worker;

CREATE TABLE IF NOT EXISTS email_gateway.retention_run_items (
    site_id text NOT NULL,
    run_ref text NOT NULL,
    projection_ref text NOT NULL,
    evidence_ref text NOT NULL,
    observer_tombstone_receipt_ref text NOT NULL,
    payload_digest text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, run_ref, projection_ref),
    FOREIGN KEY (site_id, run_ref)
        REFERENCES email_gateway.retention_runs (site_id, run_ref),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.retention_audit_events (
    site_id text NOT NULL,
    audit_event_ref text NOT NULL,
    run_ref text NOT NULL,
    projection_ref text,
    event_kind text NOT NULL,
    payload_digest text NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, audit_event_ref),
    FOREIGN KEY (site_id, run_ref)
        REFERENCES email_gateway.retention_runs (site_id, run_ref),
    CHECK (event_kind IN (
        'enqueued', 'claimed', 'receipt_verified', 'content_expired',
        'completed', 'retry', 'dead_letter'
    )),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

DROP TRIGGER IF EXISTS retention_run_items_immutable
    ON email_gateway.retention_run_items;
CREATE TRIGGER retention_run_items_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.retention_run_items
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

DROP TRIGGER IF EXISTS retention_audit_events_immutable
    ON email_gateway.retention_audit_events;
CREATE TRIGGER retention_audit_events_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.retention_audit_events
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

-- Keep the already-append-only logical expiry projection explicit. The
-- underlying reply_drafts.content_evidence_ref remains NOT NULL and immutable
-- history is not erased.
DROP TRIGGER IF EXISTS content_expiration_receipts_immutable
    ON email_gateway.content_expiration_receipts;
CREATE TRIGGER content_expiration_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.content_expiration_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

CREATE OR REPLACE VIEW email_gateway.visible_reply_draft_content
WITH (security_barrier = true)
AS
SELECT draft.site_id,
       draft.draft_ref,
       draft.content_evidence_ref,
       draft.content_digest,
       draft.state,
       draft.revision
  FROM email_gateway.reply_drafts AS draft
 WHERE NOT EXISTS (
     SELECT 1
       FROM email_gateway.content_expiration_receipts AS expired
      WHERE expired.site_id = draft.site_id
        AND expired.projection_ref = draft.draft_ref
 );

-- Existing application reads continue to use reply_drafts. This restrictive
-- policy makes the preserved content reference logically inaccessible after
-- the immutable expiry projection is appended, while the dedicated worker can
-- still perform fenced replay/recovery.
DROP POLICY IF EXISTS reply_draft_content_expiry_scope
    ON email_gateway.reply_drafts;
CREATE POLICY reply_draft_content_expiry_scope
    ON email_gateway.reply_drafts
    AS RESTRICTIVE
    FOR ALL
    USING (
        pg_has_role(
            current_user,
            'gbos_email_gateway_retention_worker',
            'USAGE'
        )
        OR NOT EXISTS (
            SELECT 1
              FROM email_gateway.content_expiration_receipts AS expired
             WHERE expired.site_id = reply_drafts.site_id
               AND expired.projection_ref = reply_drafts.draft_ref
        )
    )
    WITH CHECK (
        pg_has_role(
            current_user,
            'gbos_email_gateway_retention_worker',
            'USAGE'
        )
        OR NOT EXISTS (
            SELECT 1
              FROM email_gateway.content_expiration_receipts AS expired
             WHERE expired.site_id = reply_drafts.site_id
               AND expired.projection_ref = reply_drafts.draft_ref
        )
    );

ALTER TABLE email_gateway.retention_run_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.retention_run_items FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.retention_run_items FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope
    ON email_gateway.retention_run_items;
CREATE POLICY email_gateway_site_scope
    ON email_gateway.retention_run_items
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.retention_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.retention_audit_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.retention_audit_events FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope
    ON email_gateway.retention_audit_events;
CREATE POLICY email_gateway_site_scope
    ON email_gateway.retention_audit_events
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
           AND pending.attempt < pending.max_attempts
           AND (
               (
                   pending.status IN ('queued', 'retry')
                   AND pending.next_attempt_at <= p_now
               )
               OR (
                   pending.status = 'leased'
                   AND pending.lease_expires_at <= p_now
               )
           )
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

REVOKE ALL ON FUNCTION email_gateway.claim_human_retention_run(
    text, text, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.claim_human_retention_run(
    text, text, timestamptz, integer
) FROM gbos_email_gateway_worker;
GRANT EXECUTE ON FUNCTION email_gateway.claim_human_retention_run(
    text, text, timestamptz, integer
) TO gbos_email_gateway_retention_worker;

REVOKE INSERT, UPDATE ON email_gateway.retention_runs
    FROM gbos_email_gateway_worker;
REVOKE INSERT ON email_gateway.content_expiration_receipts
    FROM gbos_email_gateway_worker;
REVOKE INSERT, UPDATE ON email_gateway.worker_heartbeats
    FROM gbos_email_gateway_worker;

GRANT SELECT ON email_gateway.reply_drafts
    TO gbos_email_gateway_retention_worker;
GRANT SELECT, INSERT, UPDATE ON email_gateway.retention_runs
    TO gbos_email_gateway_retention_worker;
GRANT SELECT, INSERT ON email_gateway.retention_run_items
    TO gbos_email_gateway_retention_worker;
GRANT SELECT, INSERT ON email_gateway.content_expiration_receipts
    TO gbos_email_gateway_retention_worker;
GRANT SELECT, INSERT ON email_gateway.retention_audit_events
    TO gbos_email_gateway_retention_worker;
GRANT SELECT, INSERT, UPDATE ON email_gateway.worker_heartbeats
    TO gbos_email_gateway_retention_worker;
GRANT SELECT ON email_gateway.visible_reply_draft_content
    TO gbos_email_gateway_app;

GRANT EXECUTE ON FUNCTION email_gateway.heartbeat_human_retention_run(
    text, text, text, integer, bigint, timestamptz, integer
) TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.record_email_gateway_worker_heartbeat(
    text, text, timestamptz
) TO gbos_email_gateway_retention_worker;
