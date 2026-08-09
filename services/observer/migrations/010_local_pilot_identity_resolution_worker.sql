CREATE TABLE IF NOT EXISTS observer.identity_resolution_work (
    site_id text NOT NULL
        CHECK (
            char_length(site_id) BETWEEN 1 AND 140
            AND site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]*$'
        ),
    work_id text NOT NULL
        CHECK (work_id ~ '^IRW-[0-9a-f]{64}$'),
    identity_provider text NOT NULL
        CHECK (
            identity_provider IN (
                'email', 'wecom', 'whatsapp', 'phone', 'manual_import'
            )
        ),
    identity_ref text NOT NULL
        CHECK (
            char_length(identity_ref) BETWEEN 1 AND 160
            AND identity_ref ~ (
                '^extid:v1:' || identity_provider
                || ':[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$'
            )
            AND identity_ref !~ (
                '^extid:v1:' || identity_provider
                || ':[0-9][0-9 ()-]{7,}[0-9]$'
            )
        ),
    team_ref text NOT NULL
        CHECK (
            char_length(team_ref) BETWEEN 1 AND 256
            AND team_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
        ),
    status text NOT NULL
        CHECK (
            status IN (
                'queued', 'leased', 'retry_wait', 'unresolved',
                'confirmed', 'revoked', 'conflict', 'dead_letter'
            )
        ),
    attempt_count integer NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 100),
    max_attempts integer NOT NULL DEFAULT 5
        CHECK (max_attempts BETWEEN 1 AND 100),
    next_attempt_at timestamptz NOT NULL,
    lease_owner text
        CHECK (
            lease_owner IS NULL
            OR (
                char_length(lease_owner) BETWEEN 1 AND 256
                AND lease_owner ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
            )
        ),
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0
        CHECK (lease_generation >= 0),
    last_error_code text
        CHECK (
            last_error_code IS NULL
            OR last_error_code IN (
                'authentication_failed', 'invalid_resolver_response',
                'permission_denied', 'resolver_timeout',
                'resolver_unavailable', 'team_mismatch'
            )
        ),
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, work_id),
    UNIQUE (site_id, identity_provider, identity_ref, team_ref),
    CHECK (attempt_count <= max_attempts),
    CHECK (last_seen_at >= first_seen_at),
    CHECK (created_at <= updated_at),
    CHECK (
        (
            status = 'leased'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status <> 'leased'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS identity_resolution_work_claim_idx
    ON observer.identity_resolution_work (
        site_id, next_attempt_at, first_seen_at, work_id
    )
    WHERE status IN (
        'queued', 'leased', 'retry_wait', 'unresolved', 'confirmed', 'revoked'
    );

CREATE INDEX IF NOT EXISTS identity_resolution_work_status_idx
    ON observer.identity_resolution_work (site_id, status, first_seen_at);

CREATE TABLE IF NOT EXISTS observer.identity_resolution_worker_metrics (
    site_id text PRIMARY KEY
        CHECK (
            char_length(site_id) BETWEEN 1 AND 140
            AND site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]*$'
        ),
    worker_last_heartbeat_at timestamptz,
    request_confirmed_count bigint NOT NULL DEFAULT 0
        CHECK (request_confirmed_count >= 0),
    request_unresolved_count bigint NOT NULL DEFAULT 0
        CHECK (request_unresolved_count >= 0),
    request_revoked_count bigint NOT NULL DEFAULT 0
        CHECK (request_revoked_count >= 0),
    request_conflict_count bigint NOT NULL DEFAULT 0
        CHECK (request_conflict_count >= 0),
    request_error_count bigint NOT NULL DEFAULT 0
        CHECK (request_error_count >= 0),
    latency_le_100_ms_count bigint NOT NULL DEFAULT 0
        CHECK (latency_le_100_ms_count >= 0),
    latency_le_500_ms_count bigint NOT NULL DEFAULT 0
        CHECK (latency_le_500_ms_count >= 0),
    latency_le_2000_ms_count bigint NOT NULL DEFAULT 0
        CHECK (latency_le_2000_ms_count >= 0),
    latency_gt_2000_ms_count bigint NOT NULL DEFAULT 0
        CHECK (latency_gt_2000_ms_count >= 0),
    updated_at timestamptz NOT NULL
);

CREATE OR REPLACE FUNCTION observer.prevent_identity_resolution_work_scope_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.site_id IS DISTINCT FROM OLD.site_id
       OR NEW.work_id IS DISTINCT FROM OLD.work_id
       OR NEW.identity_provider IS DISTINCT FROM OLD.identity_provider
       OR NEW.identity_ref IS DISTINCT FROM OLD.identity_ref
       OR NEW.team_ref IS DISTINCT FROM OLD.team_ref
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.first_seen_at IS DISTINCT FROM OLD.first_seen_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'identity resolution work scope is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('conflict', 'dead_letter')
       AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'terminal identity resolution work cannot be reopened'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS identity_resolution_work_scope_immutable
    ON observer.identity_resolution_work;
CREATE TRIGGER identity_resolution_work_scope_immutable
    BEFORE UPDATE ON observer.identity_resolution_work
    FOR EACH ROW
    EXECUTE FUNCTION observer.prevent_identity_resolution_work_scope_mutation();

REVOKE ALL ON FUNCTION observer.prevent_identity_resolution_work_scope_mutation()
    FROM PUBLIC;

ALTER TABLE observer.identity_resolution_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_resolution_work FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS identity_resolution_work_site_isolation
    ON observer.identity_resolution_work;
CREATE POLICY identity_resolution_work_site_isolation
    ON observer.identity_resolution_work
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.identity_resolution_worker_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_resolution_worker_metrics FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS identity_resolution_worker_metrics_site_isolation
    ON observer.identity_resolution_worker_metrics;
CREATE POLICY identity_resolution_worker_metrics_site_isolation
    ON observer.identity_resolution_worker_metrics
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.identity_resolution_work FROM PUBLIC;
REVOKE ALL ON observer.identity_resolution_worker_metrics FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE
    ON observer.identity_resolution_work
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE
    ON observer.identity_resolution_worker_metrics
    TO gbos_observer_app;
