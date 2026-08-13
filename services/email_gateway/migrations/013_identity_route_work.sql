CREATE TABLE IF NOT EXISTS email_gateway.identity_route_work (
    site_id text NOT NULL,
    processing_purpose text NOT NULL,
    work_ref text NOT NULL,
    opaque_address_ref text NOT NULL,
    mapping_ref text NOT NULL,
    mapping_revision bigint NOT NULL,
    expected_team_ref text NOT NULL,
    projection_receipt_ref text NOT NULL,
    projection_payload_digest text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    attempt integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0,
    fence_token text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    safe_error_code text,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, processing_purpose, work_ref),
    UNIQUE (site_id, processing_purpose, opaque_address_ref, mapping_revision),
    FOREIGN KEY (site_id, projection_receipt_ref)
        REFERENCES email_gateway.identity_projection_receipts (
            site_id, projection_receipt_ref
        ),
    CHECK (work_ref ~ '^IRW-[0-9A-HJKMNP-TV-Z]{26}$'),
    CHECK (opaque_address_ref ~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'),
    CHECK (mapping_revision >= 1),
    CHECK (projection_payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (status IN (
        'queued', 'leased', 'retry', 'completed', 'superseded', 'dead_letter'
    )),
    CHECK (attempt BETWEEN 0 AND 5),
    CHECK (max_attempts = 5),
    CHECK (lease_generation >= 0),
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL AND fence_token IS NOT NULL)
        OR (status <> 'leased' AND lease_owner IS NULL
            AND lease_expires_at IS NULL AND fence_token IS NULL)
    ),
    CHECK (
        (status IN ('completed', 'superseded', 'dead_letter') AND completed_at IS NOT NULL)
        OR (status NOT IN ('completed', 'superseded', 'dead_letter')
            AND completed_at IS NULL)
    ),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS identity_route_work_claim_idx
    ON email_gateway.identity_route_work (
        site_id, processing_purpose, next_attempt_at, created_at, work_ref
    )
    WHERE status IN ('queued', 'retry', 'leased');

CREATE OR REPLACE FUNCTION email_gateway.reject_identity_route_work_pin_change()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       OR NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.processing_purpose IS DISTINCT FROM OLD.processing_purpose
       OR NEW.work_ref IS DISTINCT FROM OLD.work_ref
       OR NEW.opaque_address_ref IS DISTINCT FROM OLD.opaque_address_ref
       OR NEW.mapping_ref IS DISTINCT FROM OLD.mapping_ref
       OR NEW.mapping_revision IS DISTINCT FROM OLD.mapping_revision
       OR NEW.expected_team_ref IS DISTINCT FROM OLD.expected_team_ref
       OR NEW.projection_receipt_ref IS DISTINCT FROM OLD.projection_receipt_ref
       OR NEW.projection_payload_digest IS DISTINCT FROM OLD.projection_payload_digest
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'immutable identity route work pins'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION email_gateway.reject_identity_route_work_pin_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS identity_route_work_immutable_pins
    ON email_gateway.identity_route_work;
CREATE TRIGGER identity_route_work_immutable_pins
    BEFORE UPDATE OR DELETE ON email_gateway.identity_route_work
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_identity_route_work_pin_change();

ALTER TABLE email_gateway.identity_route_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.identity_route_work FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.identity_route_work FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_identity_route_work_scope
    ON email_gateway.identity_route_work;
CREATE POLICY email_gateway_identity_route_work_scope
    ON email_gateway.identity_route_work
    USING (
        site_id = current_setting('gbos.site_id', true)
        AND processing_purpose = current_setting('gbos.processing_purpose', true)
    )
    WITH CHECK (
        site_id = current_setting('gbos.site_id', true)
        AND processing_purpose = current_setting('gbos.processing_purpose', true)
    );

CREATE OR REPLACE FUNCTION email_gateway.requeue_identity_route_work_for_inbox()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_processing_purpose text;
    v_sender_ref text;
    v_projection email_gateway.identity_projection_receipts%ROWTYPE;
    v_work_ref text;
BEGIN
    IF NEW.state <> 'identity_pending'
       OR NEW.assignee_user_ref IS NOT NULL THEN
        RETURN NEW;
    END IF;
    SELECT mailbox.business_purpose
      INTO v_processing_purpose
      FROM email_gateway.mailboxes AS mailbox
     WHERE mailbox.site_id = NEW.site_id
       AND mailbox.mailbox_ref = NEW.mailbox_ref
       AND mailbox.business_purpose = current_setting(
           'gbos.processing_purpose', true
       )
       AND mailbox.default_team_ref = NEW.team_ref;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;
    SELECT min(participant.identity_ref)
      INTO v_sender_ref
      FROM email_gateway.message_participants AS participant
     WHERE participant.site_id = NEW.site_id
       AND participant.message_ref = NEW.message_ref
       AND participant.role = 'from'
    HAVING count(*) = 1;
    IF v_sender_ref IS NULL THEN
        RETURN NEW;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.site_id || chr(31) || v_processing_purpose || chr(31)
            || v_sender_ref,
            0
        )
    );
    SELECT projection.* INTO v_projection
      FROM email_gateway.identity_projection_receipts AS projection
     WHERE projection.site_id = NEW.site_id
       AND projection.processing_purpose = v_processing_purpose
       AND projection.opaque_address_ref = v_sender_ref
     ORDER BY projection.external_identity_revision DESC, projection.created_at DESC
     LIMIT 1;
    IF NOT FOUND
       OR v_projection.identity_type <> 'Party'
       OR v_projection.status <> 'confirmed'
       OR v_projection.team_ref <> NEW.team_ref THEN
        RETURN NEW;
    END IF;
    v_work_ref := 'IRW-' || upper(substr(md5(
        NEW.site_id || chr(31) || v_processing_purpose || chr(31)
        || v_sender_ref || chr(31)
        || v_projection.external_identity_revision::text
    ), 1, 26));

    INSERT INTO email_gateway.identity_route_work AS work (
        site_id, processing_purpose, work_ref, opaque_address_ref,
        mapping_ref, mapping_revision, expected_team_ref,
        projection_receipt_ref, projection_payload_digest,
        status, attempt, max_attempts, lease_generation,
        next_attempt_at, created_at, updated_at
    ) VALUES (
        NEW.site_id, v_processing_purpose, v_work_ref, v_sender_ref,
        v_projection.external_identity_ref,
        v_projection.external_identity_revision, v_projection.team_ref,
        v_projection.projection_receipt_ref, v_projection.payload_digest,
        'queued', 0, 5, 0, clock_timestamp(), clock_timestamp(), clock_timestamp()
    )
    ON CONFLICT (
        site_id, processing_purpose, opaque_address_ref, mapping_revision
    ) DO UPDATE
       SET status = 'queued', attempt = 0, lease_owner = NULL,
           lease_expires_at = NULL,
           lease_generation = work.lease_generation + 1,
           fence_token = NULL, next_attempt_at = clock_timestamp(),
           safe_error_code = NULL, completed_at = NULL,
           updated_at = greatest(clock_timestamp(), work.created_at)
     WHERE work.status IN ('completed', 'leased');
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION email_gateway.requeue_identity_route_work_for_inbox()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS inbox_items_requeue_identity_route_work
    ON email_gateway.inbox_items;
CREATE TRIGGER inbox_items_requeue_identity_route_work
    AFTER INSERT ON email_gateway.inbox_items
    FOR EACH ROW
    EXECUTE FUNCTION email_gateway.requeue_identity_route_work_for_inbox();

-- Backfill only current confirmed Party projections that already have an
-- eligible pending Inbox when this migration is installed. This remains
-- opaque and purpose/team scoped.
INSERT INTO email_gateway.identity_route_work (
    site_id, processing_purpose, work_ref, opaque_address_ref,
    mapping_ref, mapping_revision, expected_team_ref,
    projection_receipt_ref, projection_payload_digest,
    status, attempt, max_attempts, lease_generation,
    next_attempt_at, created_at, updated_at
)
SELECT DISTINCT ON (
       projection.site_id, projection.processing_purpose,
       projection.opaque_address_ref, projection.external_identity_revision
       )
       projection.site_id, projection.processing_purpose,
       'IRW-' || upper(substr(md5(
           projection.site_id || chr(31) || projection.processing_purpose
           || chr(31) || projection.opaque_address_ref || chr(31)
           || projection.external_identity_revision::text
       ), 1, 26)),
       projection.opaque_address_ref, projection.external_identity_ref,
       projection.external_identity_revision, projection.team_ref,
       projection.projection_receipt_ref, projection.payload_digest,
       'queued', 0, 5, 0, clock_timestamp(), clock_timestamp(), clock_timestamp()
  FROM email_gateway.identity_projection_receipts AS projection
  JOIN email_gateway.message_participants AS participant
    ON participant.site_id = projection.site_id
   AND participant.role = 'from'
   AND participant.identity_ref = projection.opaque_address_ref
  JOIN email_gateway.inbox_items AS inbox
    ON inbox.site_id = participant.site_id
   AND inbox.message_ref = participant.message_ref
   AND inbox.state = 'identity_pending'
   AND inbox.assignee_user_ref IS NULL
   AND inbox.team_ref = projection.team_ref
  JOIN email_gateway.mailboxes AS mailbox
    ON mailbox.site_id = inbox.site_id
   AND mailbox.mailbox_ref = inbox.mailbox_ref
   AND mailbox.business_purpose = projection.processing_purpose
   AND mailbox.default_team_ref = projection.team_ref
 WHERE projection.identity_type = 'Party'
   AND projection.status = 'confirmed'
   AND NOT EXISTS (
       SELECT 1
         FROM email_gateway.identity_projection_receipts AS newer
        WHERE newer.site_id = projection.site_id
          AND newer.processing_purpose = projection.processing_purpose
          AND newer.opaque_address_ref = projection.opaque_address_ref
          AND newer.external_identity_revision
              > projection.external_identity_revision
   )
ON CONFLICT (
    site_id, processing_purpose, opaque_address_ref, mapping_revision
) DO NOTHING;

-- The worker has no direct Inbox UPDATE grant. This function is the only
-- mutation boundary and rechecks the exact lease, immutable projection pins,
-- current mapping, purpose-scoped mailbox, sender participant, team, state,
-- and Inbox revision in one transaction before applying the route.
CREATE OR REPLACE FUNCTION email_gateway.apply_identity_route_fenced(
    p_site_id text,
    p_processing_purpose text,
    p_work_ref text,
    p_worker_id text,
    p_attempt integer,
    p_generation bigint,
    p_fence_token text,
    p_inbox_item_ref text,
    p_expected_revision bigint,
    p_target_state text,
    p_assignee_user_ref text,
    p_now timestamptz,
    p_operation_ref text,
    p_request_id text,
    p_idempotency_key text,
    p_payload_digest text,
    p_audit_ref text,
    p_audit_idempotency_key text
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_work email_gateway.identity_route_work%ROWTYPE;
    v_opaque_address_ref text;
    v_mailbox_ref text;
    v_inbox_revision bigint;
    v_projection_status text;
    v_projection_type text;
    v_projection_mapping_ref text;
    v_projection_revision bigint;
    v_projection_team_ref text;
    v_projection_receipt_ref text;
    v_projection_digest text;
    v_replay_revision bigint;
    v_replay_digest text;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_processing_purpose IS DISTINCT FROM current_setting(
           'gbos.processing_purpose', true
       ) THEN
        RAISE EXCEPTION 'identity route scope rejected';
    END IF;
    IF p_target_state = 'assigned' THEN
        IF p_assignee_user_ref IS NULL THEN
            RAISE EXCEPTION 'identity route assignment rejected';
        END IF;
    ELSIF p_target_state = 'unassigned' THEN
        IF p_assignee_user_ref IS NOT NULL THEN
            RAISE EXCEPTION 'identity route unassigned payload rejected';
        END IF;
    ELSE
        RAISE EXCEPTION 'identity route state rejected';
    END IF;

    SELECT request.result_revision, request.payload_digest
      INTO v_replay_revision, v_replay_digest
      FROM email_gateway.inbox_operation_requests AS request
     WHERE request.site_id = p_site_id
       AND request.idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_replay_digest IS DISTINCT FROM p_payload_digest THEN
            RAISE EXCEPTION 'identity route replay drift';
        END IF;
        RETURN v_replay_revision;
    END IF;

    SELECT work.opaque_address_ref INTO v_opaque_address_ref
      FROM email_gateway.identity_route_work AS work
     WHERE work.site_id = p_site_id
       AND work.processing_purpose = p_processing_purpose
       AND work.work_ref = p_work_ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity route lease fence rejected';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            p_site_id || chr(31) || p_processing_purpose || chr(31)
            || v_opaque_address_ref,
            0
        )
    );
    SELECT work.* INTO v_work
      FROM email_gateway.identity_route_work AS work
     WHERE work.site_id = p_site_id
       AND work.processing_purpose = p_processing_purpose
       AND work.work_ref = p_work_ref
     FOR UPDATE;
    IF NOT FOUND
       OR v_work.status <> 'leased'
       OR v_work.lease_owner IS DISTINCT FROM p_worker_id
       OR v_work.attempt IS DISTINCT FROM p_attempt
       OR v_work.lease_generation IS DISTINCT FROM p_generation
       OR v_work.fence_token IS DISTINCT FROM p_fence_token
       OR v_work.lease_expires_at < p_now THEN
        RAISE EXCEPTION 'identity route lease fence rejected';
    END IF;

    SELECT projection.status, projection.identity_type,
           projection.external_identity_ref,
           projection.external_identity_revision, projection.team_ref,
           projection.projection_receipt_ref, projection.payload_digest
      INTO v_projection_status, v_projection_type, v_projection_mapping_ref,
           v_projection_revision, v_projection_team_ref,
           v_projection_receipt_ref, v_projection_digest
      FROM email_gateway.identity_projection_receipts AS projection
     WHERE projection.site_id = p_site_id
       AND projection.processing_purpose = p_processing_purpose
       AND projection.opaque_address_ref = v_work.opaque_address_ref
     ORDER BY projection.external_identity_revision DESC, projection.created_at DESC
     LIMIT 1;
    IF NOT FOUND
       OR v_projection_status <> 'confirmed'
       OR v_projection_type <> 'Party'
       OR v_projection_mapping_ref IS DISTINCT FROM v_work.mapping_ref
       OR v_projection_revision IS DISTINCT FROM v_work.mapping_revision
       OR v_projection_team_ref IS DISTINCT FROM v_work.expected_team_ref
       OR v_projection_receipt_ref IS DISTINCT FROM v_work.projection_receipt_ref
       OR v_projection_digest IS DISTINCT FROM v_work.projection_payload_digest THEN
        RAISE EXCEPTION 'identity route projection fence rejected';
    END IF;

    SELECT inbox.mailbox_ref INTO v_mailbox_ref
      FROM email_gateway.inbox_items AS inbox
     WHERE inbox.site_id = p_site_id
       AND inbox.inbox_item_ref = p_inbox_item_ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity route Inbox unavailable';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            p_site_id || chr(31) || p_processing_purpose || chr(31)
            || v_mailbox_ref,
            0
        )
    );

    SELECT inbox.revision INTO v_inbox_revision
      FROM email_gateway.inbox_items AS inbox
      JOIN email_gateway.mailboxes AS mailbox
        ON mailbox.site_id = inbox.site_id
       AND mailbox.mailbox_ref = inbox.mailbox_ref
     WHERE inbox.site_id = p_site_id
       AND inbox.inbox_item_ref = p_inbox_item_ref
       AND inbox.revision = p_expected_revision
       AND inbox.state = 'identity_pending'
       AND inbox.assignee_user_ref IS NULL
       AND inbox.team_ref = v_work.expected_team_ref
       AND mailbox.business_purpose = p_processing_purpose
       AND mailbox.default_team_ref = v_work.expected_team_ref
       AND EXISTS (
           SELECT 1
             FROM email_gateway.message_participants AS participant
            WHERE participant.site_id = inbox.site_id
              AND participant.message_ref = inbox.message_ref
              AND participant.role = 'from'
              AND participant.identity_ref = v_work.opaque_address_ref
       )
     FOR UPDATE OF inbox;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity route current Inbox rejected';
    END IF;

    UPDATE email_gateway.inbox_items AS inbox
       SET state = p_target_state,
           assignee_user_ref = p_assignee_user_ref,
           revision = inbox.revision + 1,
           updated_at = p_now
     WHERE inbox.site_id = p_site_id
       AND inbox.inbox_item_ref = p_inbox_item_ref
       AND inbox.revision = v_inbox_revision;

    INSERT INTO email_gateway.audit_events (
        site_id, audit_ref, actor_ref, event_type, subject_ref,
        request_id, idempotency_key, payload_digest, occurred_at
    ) VALUES (
        p_site_id, p_audit_ref, 'email-gateway-routing_worker',
        'inbox_identity_routed', p_inbox_item_ref, p_request_id,
        p_audit_idempotency_key, p_payload_digest, p_now
    );
    INSERT INTO email_gateway.inbox_operation_requests (
        site_id, operation_ref, inbox_item_ref, actor_ref, actor_kind,
        operation_type, expected_revision, result_revision, request_id,
        idempotency_key, payload_digest, occurred_at
    ) VALUES (
        p_site_id, p_operation_ref, p_inbox_item_ref,
        'email-gateway-routing_worker', 'routing_worker', 'identity_route',
        p_expected_revision, p_expected_revision + 1, p_request_id,
        p_idempotency_key, p_payload_digest, p_now
    );
    RETURN p_expected_revision + 1;
END
$$;
REVOKE ALL ON FUNCTION email_gateway.apply_identity_route_fenced(
    text, text, text, text, integer, bigint, text, text, bigint, text,
    text, timestamptz, text, text, text, text, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION email_gateway.apply_identity_route_fenced(
    text, text, text, text, integer, bigint, text, text, bigint, text,
    text, timestamptz, text, text, text, text, text, text
) TO gbos_email_gateway_worker;

REVOKE ALL ON email_gateway.identity_route_work FROM gbos_email_gateway_app;
GRANT SELECT, INSERT, UPDATE ON email_gateway.identity_route_work
    TO gbos_email_gateway_app;
REVOKE ALL ON email_gateway.identity_route_work FROM gbos_email_gateway_worker;
GRANT SELECT, UPDATE ON email_gateway.identity_route_work
    TO gbos_email_gateway_worker;
GRANT SELECT ON email_gateway.identity_projection_receipts,
    email_gateway.inbox_items, email_gateway.message_participants,
    email_gateway.mailboxes, email_gateway.inbox_operation_requests
    TO gbos_email_gateway_worker;
