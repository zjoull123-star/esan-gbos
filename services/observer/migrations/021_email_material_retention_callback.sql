-- Durable callback delivery for terminal email-material tombstones.  The
-- callback payload intentionally carries no CAS locator or material bytes.

CREATE TABLE IF NOT EXISTS observer.email_material_retention_callbacks (
    callback_ref text NOT NULL CHECK (callback_ref ~ '^EMC-[0-9A-HJKMNP-TV-Z]{26}$'),
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    authority_receipt_ref text NOT NULL
        CHECK (authority_receipt_ref ~ '^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$'),
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    observer_request_ref text NOT NULL
        CHECK (observer_request_ref ~ '^EMR-[0-9A-HJKMNP-TV-Z]{26}$'),
    tombstone_receipt_ref text NOT NULL
        CHECK (tombstone_receipt_ref ~ '^TMB-[0-9A-HJKMNP-TV-Z]{26}$'),
    deleted_at timestamptz NOT NULL,
    evidence_digest char(71) NOT NULL CHECK (evidence_digest ~ '^sha256:[a-f0-9]{64}$'),
    callback_payload_digest char(71) NOT NULL
        CHECK (callback_payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, callback_ref),
    UNIQUE (site_id, purpose, authority_receipt_ref),
    UNIQUE (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, observer_request_ref),
    UNIQUE (site_id, purpose, tombstone_receipt_ref)
);

CREATE TABLE IF NOT EXISTS observer.email_material_retention_callback_work (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    callback_ref text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'retry', 'delivered', 'dead_letter')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 5),
    worker_id text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at timestamptz,
    next_attempt_at timestamptz NOT NULL,
    safe_error_code text,
    gateway_callback_receipt_ref text,
    delivered_at timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, callback_ref),
    FOREIGN KEY (site_id, purpose, callback_ref)
        REFERENCES observer.email_material_retention_callbacks
            (site_id, purpose, callback_ref),
    CHECK (
        (status = 'leased' AND worker_id IS NOT NULL AND lease_expires_at IS NOT NULL
            AND attempt >= 1 AND lease_generation >= 1)
        OR
        (status <> 'leased' AND worker_id IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND gateway_callback_receipt_ref IS NOT NULL
            AND delivered_at IS NOT NULL)
        OR
        (status <> 'delivered' AND gateway_callback_receipt_ref IS NULL
            AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS email_material_retention_callback_claim_idx
    ON observer.email_material_retention_callback_work
        (site_id, purpose, status, next_attempt_at, lease_expires_at)
    WHERE status IN ('pending', 'retry', 'leased');

DROP TRIGGER IF EXISTS email_material_retention_callbacks_immutable
    ON observer.email_material_retention_callbacks;
CREATE TRIGGER email_material_retention_callbacks_immutable
    BEFORE UPDATE OR DELETE ON observer.email_material_retention_callbacks
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_material_retention_mutation();

CREATE OR REPLACE FUNCTION observer.email_material_retention_callback_payload_digest(
    p_site_id text,
    p_authority_receipt_ref text,
    p_evidence_ref text,
    p_observer_request_ref text,
    p_tombstone_receipt_ref text,
    p_deleted_at timestamptz,
    p_evidence_digest text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public
AS $function$
    SELECT 'sha256:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        p_site_id || chr(31) || 'email_draft_material' || chr(31) ||
        p_authority_receipt_ref || chr(31) || p_evidence_ref || chr(31) ||
        p_observer_request_ref || chr(31) || p_tombstone_receipt_ref || chr(31) ||
        pg_catalog.to_char(
            p_deleted_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ) || chr(31) || p_evidence_digest,
        'UTF8'
    )), 'hex')
$function$;

-- Migration 019 remains checksum-stable.  This replacement preserves its
-- fence and hold checks while adding callback rows in the same transaction.
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
SET search_path = pg_catalog, observer, public
AS $function$
DECLARE
    v_request observer.email_material_retention_requests%ROWTYPE;
    v_callback_ref text;
    v_callback_payload_digest text;
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

    v_callback_ref := 'EMC-' || upper(substr(md5(
        v_request.site_id || chr(31) || v_request.authority_receipt_ref || chr(31) ||
        p_tombstone_receipt_ref
    ), 1, 26));
    v_callback_payload_digest :=
        observer.email_material_retention_callback_payload_digest(
            v_request.site_id, v_request.authority_receipt_ref,
            v_request.evidence_ref, v_request.request_ref,
            p_tombstone_receipt_ref, p_deleted_at, v_request.digest::text
        );
    INSERT INTO observer.email_material_retention_callbacks (
        callback_ref, site_id, purpose, authority_receipt_ref, evidence_ref,
        observer_request_ref, tombstone_receipt_ref, deleted_at,
        evidence_digest, callback_payload_digest, created_at
    ) VALUES (
        v_callback_ref, v_request.site_id, v_request.purpose,
        v_request.authority_receipt_ref, v_request.evidence_ref,
        v_request.request_ref, p_tombstone_receipt_ref, p_deleted_at,
        v_request.digest, v_callback_payload_digest, p_deleted_at
    );
    INSERT INTO observer.email_material_retention_callback_work (
        site_id, purpose, callback_ref, status, next_attempt_at, updated_at
    ) VALUES (
        v_request.site_id, v_request.purpose, v_callback_ref,
        'pending', p_deleted_at, p_deleted_at
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

CREATE OR REPLACE FUNCTION observer.claim_email_material_retention_callback(
    p_site_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_until timestamptz
)
RETURNS TABLE (
    callback_ref text, site_id text, purpose text,
    authority_receipt_ref text, evidence_ref text, observer_request_ref text,
    tombstone_receipt_ref text, deleted_at timestamptz,
    evidence_digest text, callback_payload_digest text,
    worker_id text, attempt integer, lease_generation bigint,
    lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
       OR p_worker_id LIKE '%@%'
       OR p_lease_until <= p_now OR p_lease_until > p_now + interval '5 minutes' THEN
        RAISE EXCEPTION 'email material callback claim rejected';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT work.site_id, work.purpose, work.callback_ref
          FROM observer.email_material_retention_callback_work AS work
         WHERE work.site_id = p_site_id
           AND work.attempt < 5
           AND work.next_attempt_at <= p_now
           AND (
               work.status IN ('pending', 'retry')
               OR (work.status = 'leased' AND work.lease_expires_at <= p_now)
           )
         ORDER BY work.next_attempt_at, work.callback_ref
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    ), leased AS (
        UPDATE observer.email_material_retention_callback_work AS work
           SET status = 'leased', attempt = work.attempt + 1,
               worker_id = p_worker_id, lease_generation = work.lease_generation + 1,
               lease_expires_at = p_lease_until, safe_error_code = NULL,
               updated_at = p_now
          FROM candidate
         WHERE work.site_id = candidate.site_id
           AND work.purpose = candidate.purpose
           AND work.callback_ref = candidate.callback_ref
        RETURNING work.*
    )
    SELECT callback.callback_ref, callback.site_id, callback.purpose,
           callback.authority_receipt_ref, callback.evidence_ref,
           callback.observer_request_ref, callback.tombstone_receipt_ref,
           callback.deleted_at, callback.evidence_digest::text,
           callback.callback_payload_digest::text, leased.worker_id,
           leased.attempt, leased.lease_generation, leased.lease_expires_at
      FROM leased
      JOIN observer.email_material_retention_callbacks AS callback
        USING (site_id, purpose, callback_ref);
END
$function$;

CREATE OR REPLACE FUNCTION observer.heartbeat_email_material_retention_callback(
    p_site_id text, p_callback_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint,
    p_now timestamptz, p_lease_until timestamptz
)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
DECLARE v_expiry timestamptz;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_lease_until <= p_now OR p_lease_until > p_now + interval '5 minutes' THEN
        RAISE EXCEPTION 'email material callback heartbeat rejected';
    END IF;
    UPDATE observer.email_material_retention_callback_work AS work
       SET lease_expires_at = p_lease_until, updated_at = p_now
     WHERE work.site_id = p_site_id
       AND work.callback_ref = p_callback_ref
       AND work.status = 'leased'
       AND work.worker_id = p_worker_id
       AND work.attempt = p_attempt
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now
    RETURNING work.lease_expires_at INTO v_expiry;
    IF v_expiry IS NULL THEN
        RAISE EXCEPTION 'email material callback lease fence conflict';
    END IF;
    RETURN v_expiry;
END
$function$;

CREATE OR REPLACE FUNCTION observer.ack_email_material_retention_callback(
    p_site_id text, p_callback_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint,
    p_gateway_callback_receipt_ref text, p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_gateway_callback_receipt_ref !~ '^GTC-[0-9A-HJKMNP-TV-Z]{26}$' THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM observer.email_material_retention_callback_work AS work
         WHERE work.site_id = p_site_id
           AND work.callback_ref = p_callback_ref
           AND work.status = 'delivered'
           AND work.gateway_callback_receipt_ref = p_gateway_callback_receipt_ref
    ) THEN
        RETURN true;
    END IF;
    UPDATE observer.email_material_retention_callback_work AS work
       SET status = 'delivered', worker_id = NULL, lease_expires_at = NULL,
           gateway_callback_receipt_ref = p_gateway_callback_receipt_ref,
           delivered_at = p_now, safe_error_code = NULL, updated_at = p_now
     WHERE work.site_id = p_site_id
       AND work.callback_ref = p_callback_ref
       AND work.status = 'leased'
       AND work.worker_id = p_worker_id
       AND work.attempt = p_attempt
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION observer.fail_email_material_retention_callback(
    p_site_id text, p_callback_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint, p_safe_error_code text,
    p_next_attempt_at timestamptz, p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_safe_error_code !~ '^[a-z][a-z0-9_]{0,79}$'
       OR p_next_attempt_at < p_now
       OR p_next_attempt_at > p_now + interval '5 minutes' THEN
        RETURN false;
    END IF;
    UPDATE observer.email_material_retention_callback_work AS work
       SET status = CASE WHEN work.attempt >= 5 THEN 'dead_letter' ELSE 'retry' END,
           worker_id = NULL, lease_expires_at = NULL,
           next_attempt_at = p_next_attempt_at,
           safe_error_code = p_safe_error_code, updated_at = p_now
     WHERE work.site_id = p_site_id
       AND work.callback_ref = p_callback_ref
       AND work.status = 'leased'
       AND work.worker_id = p_worker_id
       AND work.attempt = p_attempt
       AND work.lease_generation = p_lease_generation
       AND work.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

ALTER TABLE observer.email_material_retention_callbacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_callbacks FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_callback_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_material_retention_callback_work FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_material_retention_callbacks_site_isolation
    ON observer.email_material_retention_callbacks;
CREATE POLICY email_material_retention_callbacks_site_isolation
    ON observer.email_material_retention_callbacks TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));
DROP POLICY IF EXISTS email_material_retention_callback_work_site_isolation
    ON observer.email_material_retention_callback_work;
CREATE POLICY email_material_retention_callback_work_site_isolation
    ON observer.email_material_retention_callback_work TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_material_retention_callbacks FROM PUBLIC;
REVOKE ALL ON observer.email_material_retention_callback_work FROM PUBLIC;
REVOKE ALL ON observer.email_material_retention_callbacks FROM gbos_observer_app;
REVOKE ALL ON observer.email_material_retention_callback_work FROM gbos_observer_app;
GRANT SELECT ON observer.email_material_retention_callbacks TO gbos_observer_app;
GRANT SELECT ON observer.email_material_retention_callback_work TO gbos_observer_app;

REVOKE ALL ON FUNCTION observer.email_material_retention_callback_payload_digest(
    text, text, text, text, text, timestamptz, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.complete_email_material_retention(
    text, text, text, bigint, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.claim_email_material_retention_callback(
    text, text, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.heartbeat_email_material_retention_callback(
    text, text, text, integer, bigint, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.ack_email_material_retention_callback(
    text, text, text, integer, bigint, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.fail_email_material_retention_callback(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION observer.complete_email_material_retention(
    text, text, text, bigint, text, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.claim_email_material_retention_callback(
    text, text, timestamptz, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.heartbeat_email_material_retention_callback(
    text, text, text, integer, bigint, timestamptz, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.ack_email_material_retention_callback(
    text, text, text, integer, bigint, text, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.fail_email_material_retention_callback(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) TO gbos_observer_app;
