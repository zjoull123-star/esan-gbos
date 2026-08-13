-- Durable SLA and closed command-authority evidence. Only opaque references,
-- revisions, safe outcomes, and digests belong in these relations.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS email_gateway.inbox_sla_events (
    site_id text NOT NULL,
    sla_event_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    policy_ref text NOT NULL,
    policy_revision bigint NOT NULL,
    event_type text NOT NULL,
    event_at timestamptz NOT NULL,
    outcome text,
    provider_accepted_receipt_ref text,
    audit_revision bigint NOT NULL,
    payload_digest text NOT NULL,
    PRIMARY KEY (site_id, sla_event_ref),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (policy_revision >= 1),
    CHECK (event_type IN ('started', 'preserved', 'completed', 'closed', 'reopened')),
    CHECK (outcome IS NULL OR outcome IN ('met', 'overdue', 'not_applicable')),
    CHECK (audit_revision >= 1),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

CREATE TABLE IF NOT EXISTS email_gateway.inbox_authority_receipts (
    site_id text NOT NULL,
    authority_receipt_ref text NOT NULL,
    inbox_item_ref text NOT NULL,
    command_type text NOT NULL,
    actor_ref_digest text NOT NULL,
    team_ref text NOT NULL,
    target_user_ref_digest text,
    business_ref text,
    authority_revision_digest text NOT NULL,
    expected_inbox_revision bigint NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, authority_receipt_ref),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, inbox_item_ref)
        REFERENCES email_gateway.inbox_items (site_id, inbox_item_ref),
    CHECK (command_type IN ('claim', 'reassign', 'link_business')),
    CHECK (expected_inbox_revision >= 1),
    CHECK (actor_ref_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (target_user_ref_digest IS NULL OR target_user_ref_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (authority_revision_digest ~ '^sha256:[a-f0-9]{64}$'),
    CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$')
);

DROP TRIGGER IF EXISTS inbox_sla_events_immutable ON email_gateway.inbox_sla_events;
CREATE TRIGGER inbox_sla_events_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.inbox_sla_events
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();
DROP TRIGGER IF EXISTS inbox_authority_receipts_immutable
    ON email_gateway.inbox_authority_receipts;
CREATE TRIGGER inbox_authority_receipts_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.inbox_authority_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

ALTER TABLE email_gateway.inbox_sla_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.inbox_sla_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.inbox_sla_events FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.inbox_sla_events;
CREATE POLICY email_gateway_site_scope ON email_gateway.inbox_sla_events
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

ALTER TABLE email_gateway.inbox_authority_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.inbox_authority_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.inbox_authority_receipts FROM PUBLIC;
DROP POLICY IF EXISTS email_gateway_site_scope ON email_gateway.inbox_authority_receipts;
CREATE POLICY email_gateway_site_scope ON email_gateway.inbox_authority_receipts
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));

GRANT SELECT ON email_gateway.inbox_sla_events TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.inbox_authority_receipts TO gbos_email_gateway_app;

CREATE OR REPLACE FUNCTION email_gateway.start_inbox_sla_clock()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_policy record;
    v_status text;
BEGIN
    SELECT policy_ref, revision, first_response_duration_seconds
      INTO v_policy
      FROM email_gateway.mailbox_sla_policies
     WHERE site_id = NEW.site_id
       AND mailbox_ref = NEW.mailbox_ref
       AND effective_at <= NEW.received_at
     ORDER BY effective_at DESC, revision DESC
     LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'SLA policy unavailable at Observer received_at';
    END IF;
    v_status := CASE WHEN NEW.state = 'quarantined' THEN 'not_applicable' ELSE 'running' END;
    INSERT INTO email_gateway.inbox_sla_clocks (
        site_id, inbox_item_ref, policy_ref, policy_revision, started_at, due_at,
        status, audit_revision, updated_at, created_at
    ) VALUES (
        NEW.site_id, NEW.inbox_item_ref, v_policy.policy_ref, v_policy.revision,
        CASE WHEN v_status = 'not_applicable' THEN NULL ELSE NEW.received_at END,
        CASE WHEN v_status = 'not_applicable' THEN NULL ELSE
            NEW.received_at + make_interval(secs => v_policy.first_response_duration_seconds) END,
        v_status, 1, NEW.received_at, NEW.received_at
    );
    INSERT INTO email_gateway.inbox_sla_events (
        site_id, sla_event_ref, inbox_item_ref, policy_ref, policy_revision,
        event_type, event_at, outcome, audit_revision, payload_digest
    ) VALUES (
        NEW.site_id, 'SLE-' || md5(NEW.site_id || ':' || NEW.inbox_item_ref || ':started'),
        NEW.inbox_item_ref, v_policy.policy_ref, v_policy.revision, 'started',
        NEW.received_at, CASE WHEN v_status = 'not_applicable' THEN 'not_applicable' END,
        1, 'sha256:' || pg_catalog.encode(public.digest(
            NEW.site_id || ':' || NEW.inbox_item_ref || ':started', 'sha256'
        ), 'hex')
    );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS inbox_items_start_sla ON email_gateway.inbox_items;
CREATE TRIGGER inbox_items_start_sla
    AFTER INSERT ON email_gateway.inbox_items
    FOR EACH ROW EXECUTE FUNCTION email_gateway.start_inbox_sla_clock();

REVOKE ALL ON FUNCTION email_gateway.start_inbox_sla_clock() FROM PUBLIC;

CREATE OR REPLACE FUNCTION email_gateway.complete_sla_from_provider_receipt()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    clock email_gateway.inbox_sla_clocks%ROWTYPE;
    v_status text;
BEGIN
    IF NEW.outcome NOT IN ('accepted', 'delivered') THEN
        RETURN NEW;
    END IF;
    SELECT sla.* INTO clock
      FROM email_gateway.inbox_sla_clocks AS sla
      JOIN email_gateway.send_outbox AS outbox
        ON outbox.site_id = sla.site_id
       AND outbox.inbox_item_ref = sla.inbox_item_ref
     WHERE outbox.site_id = NEW.site_id
       AND outbox.send_ref = NEW.send_outbox_ref
     FOR UPDATE OF sla;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider-accepted SLA state unavailable';
    END IF;
    IF clock.status = 'not_applicable' THEN
        RETURN NEW;
    END IF;
    IF clock.completed_at IS NOT NULL THEN
        IF NEW.observed_at < clock.completed_at THEN
            RAISE EXCEPTION 'provider-accepted SLA clock regression';
        END IF;
        -- The first provider-accepted receipt is authoritative. Later accepted
        -- or delivered receipts are valid delivery history, but never reset SLA.
        RETURN NEW;
    END IF;
    IF NEW.observed_at < clock.started_at
       OR (clock.closed_at IS NOT NULL AND NEW.observed_at < clock.closed_at) THEN
        RAISE EXCEPTION 'provider-accepted SLA clock regression';
    END IF;
    v_status := CASE
        WHEN clock.closed_at IS NOT NULL THEN clock.status
        WHEN NEW.observed_at <= clock.due_at THEN 'met'
        ELSE 'overdue'
    END;
    UPDATE email_gateway.inbox_sla_clocks
       SET status = v_status,
           completed_at = NEW.observed_at,
           provider_accepted_receipt_ref = NEW.provider_receipt_record_ref,
           audit_revision = clock.audit_revision + 1,
           updated_at = NEW.observed_at
     WHERE site_id = clock.site_id AND inbox_item_ref = clock.inbox_item_ref;
    INSERT INTO email_gateway.inbox_sla_events (
        site_id, sla_event_ref, inbox_item_ref, policy_ref, policy_revision,
        event_type, event_at, outcome, provider_accepted_receipt_ref,
        audit_revision, payload_digest
    ) VALUES (
        clock.site_id,
        'SLE-' || md5(clock.site_id || ':' || NEW.provider_receipt_record_ref),
        clock.inbox_item_ref, clock.policy_ref, clock.policy_revision,
        'completed', NEW.observed_at,
        CASE WHEN NEW.observed_at <= clock.due_at THEN 'met' ELSE 'overdue' END,
        NEW.provider_receipt_record_ref, clock.audit_revision + 1,
        'sha256:' || pg_catalog.encode(public.digest(
            clock.site_id || ':' || NEW.provider_receipt_record_ref || ':completed',
            'sha256'
        ), 'hex')
    );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS provider_receipts_complete_sla ON email_gateway.provider_receipts;
CREATE TRIGGER provider_receipts_complete_sla
    AFTER INSERT ON email_gateway.provider_receipts
    FOR EACH ROW EXECUTE FUNCTION email_gateway.complete_sla_from_provider_receipt();

REVOKE ALL ON FUNCTION email_gateway.complete_sla_from_provider_receipt() FROM PUBLIC;

-- One explicit transaction seam used by the Gateway repository. The function
-- performs Inbox CAS, preserves the original SLA clock, records its event,
-- appends audit, and stores both idempotency and authority receipts atomically.
CREATE OR REPLACE FUNCTION email_gateway.apply_inbox_sla_operation(
    p_site_id text,
    p_processing_purpose text,
    p_inbox_item_ref text,
    p_expected_revision bigint,
    p_revised_state text,
    p_assignee_user_ref text,
    p_business_links text[],
    p_updated_at timestamptz,
    p_policy_ref text,
    p_policy_revision bigint,
    p_started_at timestamptz,
    p_due_at timestamptz,
    p_sla_status text,
    p_completed_at timestamptz,
    p_provider_accepted_receipt_ref text,
    p_closed_at timestamptz,
    p_closed_outcome text,
    p_sla_audit_revision bigint,
    p_operation_type text,
    p_event_type text,
    p_audit_ref text,
    p_actor_ref text,
    p_actor_ref_digest text,
    p_request_id text,
    p_idempotency_key text,
    p_payload_digest text,
    p_authority_receipt_ref text,
    p_authority_revision_digest text,
    p_target_user_ref_digest text,
    p_business_ref text
)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_clock email_gateway.inbox_sla_clocks%ROWTYPE;
    v_result_revision bigint;
    v_replay email_gateway.inbox_operation_requests%ROWTYPE;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_processing_purpose IS DISTINCT FROM current_setting('gbos.processing_purpose', true)
       OR p_expected_revision < 1 THEN
        RAISE EXCEPTION 'SLA operation scope rejected';
    END IF;
    SELECT * INTO v_replay FROM email_gateway.inbox_operation_requests
     WHERE site_id = p_site_id AND idempotency_key = p_idempotency_key FOR UPDATE;
    IF FOUND THEN
        IF v_replay.payload_digest IS DISTINCT FROM p_payload_digest
           OR v_replay.expected_revision IS DISTINCT FROM p_expected_revision THEN
            RAISE EXCEPTION 'SLA replay drift';
        END IF;
        RETURN v_replay.result_revision;
    END IF;
    SELECT * INTO STRICT v_clock FROM email_gateway.inbox_sla_clocks
     WHERE site_id = p_site_id AND inbox_item_ref = p_inbox_item_ref FOR UPDATE;
    IF v_clock.policy_ref IS DISTINCT FROM p_policy_ref
       OR v_clock.policy_revision IS DISTINCT FROM p_policy_revision THEN
        RAISE EXCEPTION 'SLA policy revision drift';
    END IF;
    IF v_clock.started_at IS DISTINCT FROM p_started_at
       OR v_clock.due_at IS DISTINCT FROM p_due_at
       OR p_updated_at < GREATEST(v_clock.completed_at, v_clock.closed_at, v_clock.started_at) THEN
        RAISE EXCEPTION 'SLA clock regression';
    END IF;
    IF p_completed_at IS DISTINCT FROM v_clock.completed_at
       OR p_provider_accepted_receipt_ref IS DISTINCT FROM v_clock.provider_accepted_receipt_ref THEN
        RAISE EXCEPTION 'SLA completion drift';
    END IF;
    UPDATE email_gateway.inbox_items AS inbox
       SET state = p_revised_state, assignee_user_ref = p_assignee_user_ref,
           business_links = p_business_links, revision = p_expected_revision + 1,
           updated_at = p_updated_at
      FROM email_gateway.mailboxes AS mailbox
     WHERE inbox.site_id = p_site_id
       AND inbox.inbox_item_ref = p_inbox_item_ref
       AND inbox.revision = p_expected_revision
       AND mailbox.site_id = inbox.site_id
       AND mailbox.mailbox_ref = inbox.mailbox_ref
       AND mailbox.business_purpose = p_processing_purpose
     RETURNING inbox.revision INTO v_result_revision;
    IF v_result_revision IS NULL THEN
        RAISE EXCEPTION 'Inbox revision conflict';
    END IF;
    UPDATE email_gateway.inbox_sla_clocks
       SET status = p_sla_status, completed_at = p_completed_at,
           provider_accepted_receipt_ref = p_provider_accepted_receipt_ref,
           closed_at = p_closed_at, closed_outcome = p_closed_outcome,
           audit_revision = p_sla_audit_revision, updated_at = p_updated_at
     WHERE site_id = p_site_id AND inbox_item_ref = p_inbox_item_ref;
    INSERT INTO email_gateway.inbox_sla_events (
        site_id, sla_event_ref, inbox_item_ref, policy_ref, policy_revision,
        event_type, event_at, outcome, provider_accepted_receipt_ref,
        audit_revision, payload_digest
    ) VALUES (
        p_site_id, 'SLE-' || md5(p_site_id || ':' || p_idempotency_key),
        p_inbox_item_ref, p_policy_ref, p_policy_revision,
        CASE WHEN p_revised_state = 'closed' THEN 'closed'
             WHEN v_clock.closed_at IS NOT NULL AND p_closed_at IS NULL THEN 'reopened'
             ELSE 'preserved' END,
        p_updated_at, p_closed_outcome, p_provider_accepted_receipt_ref,
        p_sla_audit_revision, p_payload_digest
    );
    INSERT INTO email_gateway.audit_events (
        site_id, audit_ref, actor_ref, event_type, subject_ref, request_id,
        idempotency_key, payload_digest, occurred_at
    ) VALUES (
        p_site_id, p_audit_ref, p_actor_ref, p_event_type,
        p_inbox_item_ref, p_request_id, 'audit:' || p_idempotency_key,
        p_payload_digest, p_updated_at
    );
    INSERT INTO email_gateway.inbox_operation_requests (
        site_id, operation_ref, inbox_item_ref, actor_ref, actor_kind,
        operation_type, expected_revision, result_revision, request_id,
        idempotency_key, payload_digest, occurred_at
    ) VALUES (
        p_site_id, 'OPR-' || md5(p_site_id || ':' || p_idempotency_key),
        p_inbox_item_ref, p_actor_ref, 'human', p_operation_type,
        p_expected_revision, v_result_revision, p_request_id,
        p_idempotency_key, p_payload_digest, p_updated_at
    );
    IF p_authority_receipt_ref IS NOT NULL THEN
        INSERT INTO email_gateway.inbox_authority_receipts (
            site_id, authority_receipt_ref, inbox_item_ref, command_type,
            actor_ref_digest, team_ref, target_user_ref_digest, business_ref,
            authority_revision_digest, expected_inbox_revision, request_id,
            idempotency_key, payload_digest, created_at
        ) SELECT p_site_id, p_authority_receipt_ref, p_inbox_item_ref,
                 p_operation_type, p_actor_ref_digest, inbox.team_ref,
                 p_target_user_ref_digest, p_business_ref, p_authority_revision_digest,
                 p_expected_revision, p_request_id, p_idempotency_key,
                 p_payload_digest, p_updated_at
            FROM email_gateway.inbox_items AS inbox
           WHERE inbox.site_id = p_site_id AND inbox.inbox_item_ref = p_inbox_item_ref;
    END IF;
    RETURN v_result_revision;
END
$$;

REVOKE ALL ON FUNCTION email_gateway.apply_inbox_sla_operation(
    text, text, text, bigint, text, text, text[], timestamptz, text, bigint,
    timestamptz, timestamptz, text, timestamptz, text, timestamptz, text,
    bigint, text, text, text, text, text, text, text, text, text, text, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION email_gateway.apply_inbox_sla_operation(
    text, text, text, bigint, text, text, text[], timestamptz, text, bigint,
    timestamptz, timestamptz, text, timestamptz, text, timestamptz, text,
    bigint, text, text, text, text, text, text, text, text, text, text, text, text
) TO gbos_email_gateway_app;
