CREATE TABLE IF NOT EXISTS observer.email_material_retention_requests (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    request_ref text NOT NULL CHECK (request_ref ~ '^EMR-[0-9A-HJKMNP-TV-Z]{26}$'),
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    material_kind text NOT NULL CHECK (material_kind IN ('draft', 'final_mime')),
    draft_ref text NOT NULL CHECK (draft_ref ~ '^DRF-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_revision bigint NOT NULL CHECK (draft_revision BETWEEN 1 AND 2147483647),
    object_ref text NOT NULL
        CHECK (object_ref ~ '^obs:v1:[a-f0-9]{32}:sha256:[a-f0-9]{64}$'),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    terminal_state text NOT NULL CHECK (terminal_state IN ('sent', 'discarded')),
    terminal_at timestamptz NOT NULL,
    not_before timestamptz NOT NULL,
    authority_receipt_ref text NOT NULL
        CHECK (authority_receipt_ref ~ '^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$'),
    registered_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, request_ref),
    UNIQUE (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, authority_receipt_ref),
    CHECK (not_before = terminal_at + interval '30 days'),
    CHECK (right(object_ref, 71) = digest)
);

CREATE TABLE IF NOT EXISTS observer.email_material_retention_work (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    request_ref text NOT NULL CHECK (request_ref ~ '^EMR-[0-9A-HJKMNP-TV-Z]{26}$'),
    status text NOT NULL CHECK (status IN ('pending', 'leased', 'completed')),
    worker_id text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    UNIQUE (site_id, purpose, request_ref),
    FOREIGN KEY (site_id, purpose, request_ref)
        REFERENCES observer.email_material_retention_requests (site_id, purpose, request_ref),
    CHECK (
        (status = 'pending' AND worker_id IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NULL)
        OR (status = 'leased' AND worker_id IS NOT NULL AND lease_generation >= 1
            AND lease_expires_at IS NOT NULL AND completed_at IS NULL)
        OR (status = 'completed' AND worker_id IS NULL AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS email_material_retention_work_claim_idx
    ON observer.email_material_retention_work (site_id, purpose, status, lease_expires_at)
    WHERE status IN ('pending', 'leased');

CREATE TABLE IF NOT EXISTS observer.email_material_tombstone_receipts (
    tombstone_receipt_ref text NOT NULL
        CHECK (tombstone_receipt_ref ~ '^TMB-[0-9A-HJKMNP-TV-Z]{26}$'),
    request_ref text NOT NULL CHECK (request_ref ~ '^EMR-[0-9A-HJKMNP-TV-Z]{26}$'),
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    material_kind text NOT NULL CHECK (material_kind IN ('draft', 'final_mime')),
    draft_ref text NOT NULL CHECK (draft_ref ~ '^DRF-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_revision bigint NOT NULL CHECK (draft_revision BETWEEN 1 AND 2147483647),
    object_ref text NOT NULL
        CHECK (object_ref ~ '^obs:v1:[a-f0-9]{32}:sha256:[a-f0-9]{64}$'),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    terminal_state text NOT NULL CHECK (terminal_state IN ('sent', 'discarded')),
    terminal_at timestamptz NOT NULL,
    not_before timestamptz NOT NULL,
    authority_receipt_ref text NOT NULL
        CHECK (authority_receipt_ref ~ '^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$'),
    deleted_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, tombstone_receipt_ref),
    UNIQUE (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, request_ref),
    FOREIGN KEY (site_id, purpose, request_ref)
        REFERENCES observer.email_material_retention_requests (site_id, purpose, request_ref),
    CHECK (not_before = terminal_at + interval '30 days'),
    CHECK (deleted_at >= not_before),
    CHECK (right(object_ref, 71) = digest)
);

CREATE TABLE IF NOT EXISTS observer.email_material_legal_hold_events (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    hold_ref text NOT NULL CHECK (hold_ref ~ '^HLD-[0-9A-HJKMNP-TV-Z]{26}$'),
    hold_revision bigint NOT NULL CHECK (hold_revision >= 1),
    action text NOT NULL CHECK (action IN ('placed', 'released')),
    event_at timestamptz NOT NULL,
    reason_code text NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_.-]{0,79}$'),
    PRIMARY KEY (site_id, purpose, hold_ref, hold_revision),
    UNIQUE (site_id, purpose, evidence_ref, hold_revision)
);

CREATE OR REPLACE FUNCTION observer.reject_email_material_retention_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'email_material_retention_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_material_retention_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_material_retention_requests_immutable
    ON observer.email_material_retention_requests;
CREATE TRIGGER email_material_retention_requests_immutable
    BEFORE UPDATE OR DELETE ON observer.email_material_retention_requests
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_material_retention_mutation();

DROP TRIGGER IF EXISTS email_material_tombstone_receipts_immutable
    ON observer.email_material_tombstone_receipts;
CREATE TRIGGER email_material_tombstone_receipts_immutable
    BEFORE UPDATE OR DELETE ON observer.email_material_tombstone_receipts
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_material_retention_mutation();

DROP TRIGGER IF EXISTS email_material_legal_hold_events_immutable
    ON observer.email_material_legal_hold_events;
CREATE TRIGGER email_material_legal_hold_events_immutable
    BEFORE UPDATE OR DELETE ON observer.email_material_legal_hold_events
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_material_retention_mutation();

CREATE OR REPLACE FUNCTION observer.email_material_has_legal_hold(
    p_site_id text,
    p_evidence_ref text,
    p_checked_at timestamptz
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
    SELECT
        p_site_id = current_setting('app.site_id', true)
        AND (
            EXISTS (
                SELECT 1
                  FROM observer.email_material_legal_hold_events AS event
                 WHERE event.site_id = p_site_id
                   AND event.purpose = 'email_draft_material'
                   AND event.evidence_ref = p_evidence_ref
                   AND event.event_at <= p_checked_at
                   AND event.action = 'placed'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM observer.email_material_legal_hold_events AS later
                        WHERE later.site_id = event.site_id
                          AND later.purpose = event.purpose
                          AND later.evidence_ref = event.evidence_ref
                          AND later.event_at <= p_checked_at
                          AND later.hold_revision > event.hold_revision
                   )
            )
            OR EXISTS (
                SELECT 1
                  FROM observer.legal_holds AS hold
                 WHERE hold.site_id = p_site_id
                   AND hold.evidence_id = p_evidence_ref
                   AND hold.started_at <= p_checked_at
                   AND (hold.released_at IS NULL OR hold.released_at > p_checked_at)
            )
        )
$function$;

CREATE OR REPLACE FUNCTION observer.register_email_material_retention(
    p_site_id text,
    p_purpose text,
    p_evidence_ref text,
    p_terminal_state text,
    p_terminal_at timestamptz,
    p_not_before timestamptz,
    p_authority_receipt_ref text,
    p_draft_ref text,
    p_draft_revision bigint
)
RETURNS TABLE (
    request_ref text, site_id text, purpose text, evidence_ref text,
    material_kind text, draft_ref text, draft_revision bigint,
    object_ref text, digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz, authority_receipt_ref text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
DECLARE
    v_binding record;
    v_existing observer.email_material_retention_requests%ROWTYPE;
    v_request_ref text;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_purpose <> 'email_draft_material'
       OR p_terminal_state NOT IN ('sent', 'discarded')
       OR p_not_before IS DISTINCT FROM p_terminal_at + interval '30 days'
       OR observer.email_material_has_legal_hold(
              p_site_id, p_evidence_ref, statement_timestamp()
          ) THEN
        RAISE EXCEPTION 'email material retention registration rejected'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_site_id || chr(31) || p_evidence_ref, 0)
    );

    SELECT binding.* INTO STRICT v_binding
      FROM (
          SELECT 'draft'::text AS material_kind, draft.draft_ref,
                 draft.draft_revision, draft.object_ref, draft.digest::text
            FROM observer.email_draft_evidence_bindings AS draft
           WHERE draft.site_id = p_site_id
             AND draft.purpose = p_purpose
             AND draft.evidence_ref = p_evidence_ref
             AND draft.draft_ref = p_draft_ref
             AND draft.draft_revision = p_draft_revision
          UNION ALL
          SELECT 'final_mime'::text, final.draft_ref,
                 final.draft_revision, final.object_ref, final.digest::text
            FROM observer.email_final_mime_evidence_bindings AS final
           WHERE final.site_id = p_site_id
             AND final.purpose = p_purpose
             AND final.evidence_ref = p_evidence_ref
             AND final.draft_ref = p_draft_ref
             AND final.draft_revision = p_draft_revision
      ) AS binding;
    SELECT * INTO v_existing
      FROM observer.email_material_retention_requests AS request
     WHERE request.site_id = p_site_id
       AND request.purpose = p_purpose
       AND request.evidence_ref = p_evidence_ref;
    IF FOUND THEN
        IF v_existing.draft_ref <> p_draft_ref
           OR v_existing.draft_revision <> p_draft_revision
           OR v_existing.object_ref <> v_binding.object_ref
           OR v_existing.digest::text <> v_binding.digest
           OR v_existing.terminal_state <> p_terminal_state
           OR v_existing.terminal_at <> p_terminal_at
           OR v_existing.not_before <> p_not_before
           OR v_existing.authority_receipt_ref <> p_authority_receipt_ref THEN
            RAISE EXCEPTION 'email material retention replay drift'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        v_request_ref := 'EMR-' || upper(substr(md5(
            p_site_id || chr(31) || p_evidence_ref || chr(31) ||
            p_authority_receipt_ref || chr(31) || p_terminal_at::text
        ), 1, 26));
        INSERT INTO observer.email_material_retention_requests (
            site_id, purpose, request_ref, evidence_ref, material_kind,
            draft_ref, draft_revision, object_ref, digest, terminal_state,
            terminal_at, not_before, authority_receipt_ref, registered_at
        ) VALUES (
            p_site_id, p_purpose, v_request_ref, p_evidence_ref,
            v_binding.material_kind, p_draft_ref, p_draft_revision,
            v_binding.object_ref, v_binding.digest, p_terminal_state,
            p_terminal_at, p_not_before, p_authority_receipt_ref,
            statement_timestamp()
        ) RETURNING * INTO v_existing;
        INSERT INTO observer.email_material_retention_work (
            site_id, purpose, request_ref, status, updated_at
        ) VALUES (
            p_site_id, p_purpose, v_request_ref, 'pending', statement_timestamp()
        );
    END IF;

    RETURN QUERY SELECT
        v_existing.request_ref, v_existing.site_id, v_existing.purpose,
        v_existing.evidence_ref, v_existing.material_kind, v_existing.draft_ref,
        v_existing.draft_revision, v_existing.object_ref, v_existing.digest::text,
        v_existing.terminal_state, v_existing.terminal_at, v_existing.not_before,
        v_existing.authority_receipt_ref;
END
$function$;

CREATE OR REPLACE FUNCTION observer.claim_email_material_retention(
    p_site_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_until timestamptz,
    p_limit integer
)
RETURNS TABLE (
    request_ref text, site_id text, purpose text, evidence_ref text,
    material_kind text, draft_ref text, draft_revision bigint,
    object_ref text, digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz, authority_receipt_ref text,
    worker_id text, lease_generation bigint, lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
       OR p_limit NOT BETWEEN 1 AND 100
       OR p_lease_until <= p_now
       OR p_lease_until > p_now + interval '1 hour' THEN
        RAISE EXCEPTION 'email material retention claim rejected'
            USING ERRCODE = 'serialization_failure';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT work.site_id, work.purpose, work.request_ref
          FROM observer.email_material_retention_work AS work
          JOIN observer.email_material_retention_requests AS request
            USING (site_id, purpose, request_ref)
         WHERE work.site_id = p_site_id
           AND request.not_before <= p_now
           AND (
               work.status = 'pending'
               OR (work.status = 'leased' AND work.lease_expires_at <= p_now)
           )
           AND NOT observer.email_material_has_legal_hold(
               request.site_id, request.evidence_ref, p_now
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM (
                     SELECT binding.site_id, binding.purpose,
                            binding.evidence_ref, binding.object_ref
                       FROM observer.email_draft_evidence_bindings AS binding
                     UNION ALL
                     SELECT binding.site_id, binding.purpose,
                            binding.evidence_ref, binding.object_ref
                       FROM observer.email_final_mime_evidence_bindings AS binding
                 ) AS shared
                WHERE shared.site_id = request.site_id
                  AND shared.purpose = request.purpose
                  AND shared.object_ref = request.object_ref
                  AND NOT EXISTS (
                      SELECT 1
                        FROM observer.email_material_retention_requests AS terminal
                       WHERE terminal.site_id = shared.site_id
                         AND terminal.purpose = shared.purpose
                         AND terminal.evidence_ref = shared.evidence_ref
                         AND terminal.not_before <= p_now
                  )
           )
         ORDER BY request.not_before, request.request_ref
         LIMIT p_limit
         FOR UPDATE OF work SKIP LOCKED
    ), leased AS (
        UPDATE observer.email_material_retention_work AS work
           SET status = 'leased',
               worker_id = p_worker_id,
               lease_generation = work.lease_generation + 1,
               lease_expires_at = p_lease_until,
               updated_at = p_now
          FROM candidates
         WHERE work.site_id = candidates.site_id
           AND work.purpose = candidates.purpose
           AND work.request_ref = candidates.request_ref
        RETURNING work.*
    )
    SELECT request.request_ref, request.site_id, request.purpose,
           request.evidence_ref, request.material_kind, request.draft_ref,
           request.draft_revision, request.object_ref, request.digest::text,
           request.terminal_state, request.terminal_at, request.not_before,
           request.authority_receipt_ref, leased.worker_id,
           leased.lease_generation, leased.lease_expires_at
      FROM leased
      JOIN observer.email_material_retention_requests AS request
        USING (site_id, purpose, request_ref)
     ORDER BY request.not_before, request.request_ref;
END
$function$;

CREATE OR REPLACE FUNCTION observer.complete_email_material_retention(
    p_site_id text,
    p_request_ref text,
    p_worker_id text,
    p_lease_generation bigint,
    p_tombstone_receipt_ref text,
    p_deleted_at timestamptz
)
RETURNS TABLE (
    tombstone_receipt_ref text,
    request_ref text, site_id text, purpose text, evidence_ref text,
    material_kind text, draft_ref text, draft_revision bigint,
    object_ref text, digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz, authority_receipt_ref text,
    deleted_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
DECLARE
    v_request observer.email_material_retention_requests%ROWTYPE;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true) THEN
        RETURN;
    END IF;
    SELECT request.* INTO v_request
      FROM observer.email_material_retention_work AS work
      JOIN observer.email_material_retention_requests AS request
        USING (site_id, purpose, request_ref)
     WHERE work.site_id = p_site_id
       AND work.request_ref = p_request_ref
       AND work.status = 'leased'
       AND work.worker_id = p_worker_id
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at > p_deleted_at
       AND request.not_before <= p_deleted_at
       AND NOT observer.email_material_has_legal_hold(
           request.site_id, request.evidence_ref, p_deleted_at
       )
     FOR UPDATE OF work;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    INSERT INTO observer.email_material_tombstone_receipts (
        tombstone_receipt_ref, request_ref, site_id, purpose, evidence_ref,
        material_kind, draft_ref, draft_revision, object_ref, digest,
        terminal_state, terminal_at, not_before, authority_receipt_ref, deleted_at
    ) VALUES (
        p_tombstone_receipt_ref, v_request.request_ref, v_request.site_id,
        v_request.purpose, v_request.evidence_ref, v_request.material_kind,
        v_request.draft_ref, v_request.draft_revision, v_request.object_ref,
        v_request.digest, v_request.terminal_state, v_request.terminal_at,
        v_request.not_before, v_request.authority_receipt_ref, p_deleted_at
    );
    UPDATE observer.email_material_retention_work AS work
       SET status = 'completed', worker_id = NULL, lease_expires_at = NULL,
           updated_at = p_deleted_at, completed_at = p_deleted_at
     WHERE work.site_id = v_request.site_id
       AND work.purpose = v_request.purpose
       AND work.request_ref = v_request.request_ref;

    RETURN QUERY SELECT
        p_tombstone_receipt_ref, v_request.request_ref, v_request.site_id,
        v_request.purpose, v_request.evidence_ref, v_request.material_kind,
        v_request.draft_ref, v_request.draft_revision, v_request.object_ref,
        v_request.digest::text, v_request.terminal_state, v_request.terminal_at,
        v_request.not_before, v_request.authority_receipt_ref, p_deleted_at;
END
$function$;

CREATE OR REPLACE FUNCTION observer.resolve_email_material_tombstone(
    p_site_id text,
    p_evidence_ref text,
    p_tombstone_receipt_ref text
)
RETURNS TABLE (
    tombstone_receipt_ref text,
    request_ref text, site_id text, purpose text, evidence_ref text,
    material_kind text, draft_ref text, draft_revision bigint,
    object_ref text, digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz, authority_receipt_ref text,
    deleted_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
    SELECT receipt.tombstone_receipt_ref, receipt.request_ref,
           receipt.site_id, receipt.purpose, receipt.evidence_ref,
           receipt.material_kind, receipt.draft_ref, receipt.draft_revision,
           receipt.object_ref, receipt.digest::text, receipt.terminal_state,
           receipt.terminal_at, receipt.not_before, receipt.authority_receipt_ref,
           receipt.deleted_at
      FROM observer.email_material_tombstone_receipts AS receipt
      JOIN observer.email_material_retention_requests AS request
        USING (site_id, purpose, request_ref, evidence_ref, material_kind,
               draft_ref, draft_revision, object_ref, digest, terminal_state,
               terminal_at, not_before, authority_receipt_ref)
     WHERE p_site_id = current_setting('app.site_id', true)
       AND receipt.site_id = p_site_id
       AND receipt.purpose = 'email_draft_material'
       AND receipt.evidence_ref = p_evidence_ref
       AND receipt.tombstone_receipt_ref = p_tombstone_receipt_ref
       AND (
           EXISTS (
               SELECT 1 FROM observer.email_draft_evidence_bindings AS binding
                WHERE receipt.material_kind = 'draft'
                  AND binding.site_id = receipt.site_id
                  AND binding.purpose = receipt.purpose
                  AND binding.evidence_ref = receipt.evidence_ref
                  AND binding.draft_ref = receipt.draft_ref
                  AND binding.draft_revision = receipt.draft_revision
                  AND binding.object_ref = receipt.object_ref
                  AND binding.digest = receipt.digest
           )
           OR EXISTS (
               SELECT 1 FROM observer.email_final_mime_evidence_bindings AS binding
                WHERE receipt.material_kind = 'final_mime'
                  AND binding.site_id = receipt.site_id
                  AND binding.purpose = receipt.purpose
                  AND binding.evidence_ref = receipt.evidence_ref
                  AND binding.draft_ref = receipt.draft_ref
                  AND binding.draft_revision = receipt.draft_revision
                  AND binding.object_ref = receipt.object_ref
                  AND binding.digest = receipt.digest
           )
       )
$function$;

CREATE OR REPLACE FUNCTION observer.record_email_material_legal_hold_event(
    p_site_id text,
    p_evidence_ref text,
    p_hold_ref text,
    p_hold_revision bigint,
    p_action text,
    p_event_at timestamptz,
    p_reason_code text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
DECLARE
    v_latest record;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_action NOT IN ('placed', 'released') OR p_hold_revision < 1 THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_site_id || chr(31) || p_evidence_ref, 0)
    );
    SELECT event.hold_revision, event.action INTO v_latest
      FROM observer.email_material_legal_hold_events AS event
     WHERE event.site_id = p_site_id
       AND event.purpose = 'email_draft_material'
       AND event.evidence_ref = p_evidence_ref
     ORDER BY event.hold_revision DESC
     LIMIT 1;
    IF (v_latest IS NULL AND (p_hold_revision <> 1 OR p_action <> 'placed'))
       OR (v_latest IS NOT NULL AND p_hold_revision <> v_latest.hold_revision + 1)
       OR (v_latest IS NOT NULL AND p_action = v_latest.action)
       OR (
           p_action = 'placed' AND EXISTS (
               SELECT 1
                 FROM observer.email_material_retention_work AS work
                 JOIN observer.email_material_retention_requests AS request
                   USING (site_id, purpose, request_ref)
                WHERE request.site_id = p_site_id
                  AND request.evidence_ref = p_evidence_ref
                  AND work.status = 'leased'
                  AND work.lease_expires_at > p_event_at
           )
       ) THEN
        RETURN false;
    END IF;
    INSERT INTO observer.email_material_legal_hold_events (
        site_id, purpose, evidence_ref, hold_ref, hold_revision,
        action, event_at, reason_code
    ) VALUES (
        p_site_id, 'email_draft_material', p_evidence_ref, p_hold_ref,
        p_hold_revision, p_action, p_event_at, p_reason_code
    );
    RETURN true;
END
$function$;

ALTER TABLE observer.email_material_retention_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_work FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_tombstone_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_tombstone_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_legal_hold_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_legal_hold_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_material_retention_requests_site_isolation
    ON observer.email_material_retention_requests;
CREATE POLICY email_material_retention_requests_site_isolation
    ON observer.email_material_retention_requests TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));
DROP POLICY IF EXISTS email_material_retention_work_site_isolation
    ON observer.email_material_retention_work;
CREATE POLICY email_material_retention_work_site_isolation
    ON observer.email_material_retention_work TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));
DROP POLICY IF EXISTS email_material_tombstone_receipts_site_isolation
    ON observer.email_material_tombstone_receipts;
CREATE POLICY email_material_tombstone_receipts_site_isolation
    ON observer.email_material_tombstone_receipts TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));
DROP POLICY IF EXISTS email_material_legal_hold_events_site_isolation
    ON observer.email_material_legal_hold_events;
CREATE POLICY email_material_legal_hold_events_site_isolation
    ON observer.email_material_legal_hold_events TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_material_retention_requests FROM PUBLIC;
REVOKE ALL ON observer.email_material_retention_work FROM PUBLIC;
REVOKE ALL ON observer.email_material_tombstone_receipts FROM PUBLIC;
REVOKE ALL ON observer.email_material_legal_hold_events FROM PUBLIC;
REVOKE ALL ON observer.email_material_retention_requests FROM gbos_observer_app;
REVOKE ALL ON observer.email_material_retention_work FROM gbos_observer_app;
REVOKE ALL ON observer.email_material_tombstone_receipts FROM gbos_observer_app;
REVOKE ALL ON observer.email_material_legal_hold_events FROM gbos_observer_app;
GRANT SELECT ON observer.email_material_retention_requests TO gbos_observer_app;
GRANT SELECT ON observer.email_material_retention_work TO gbos_observer_app;
GRANT SELECT ON observer.email_material_tombstone_receipts TO gbos_observer_app;
GRANT SELECT ON observer.email_material_legal_hold_events TO gbos_observer_app;

REVOKE ALL ON FUNCTION observer.email_material_has_legal_hold(text, text, timestamptz)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.register_email_material_retention(
    text, text, text, text, timestamptz, timestamptz, text, text, bigint
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.claim_email_material_retention(
    text, text, timestamptz, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.complete_email_material_retention(
    text, text, text, bigint, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.resolve_email_material_tombstone(text, text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.record_email_material_legal_hold_event(
    text, text, text, bigint, text, timestamptz, text
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION observer.email_material_has_legal_hold(text, text, timestamptz)
    TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.register_email_material_retention(
    text, text, text, text, timestamptz, timestamptz, text, text, bigint
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.claim_email_material_retention(
    text, text, timestamptz, timestamptz, integer
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.complete_email_material_retention(
    text, text, text, bigint, text, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.resolve_email_material_tombstone(text, text, text)
    TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.record_email_material_legal_hold_event(
    text, text, text, bigint, text, timestamptz, text
) TO gbos_observer_app;
