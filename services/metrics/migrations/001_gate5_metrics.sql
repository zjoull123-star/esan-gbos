CREATE SCHEMA IF NOT EXISTS metrics;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gbos_metrics_app') THEN
        CREATE ROLE gbos_metrics_app
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE gbos_metrics_app
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS metrics.projection_batches (
    site_id text NOT NULL,
    batch_id text NOT NULL,
    source_mode text NOT NULL CHECK (source_mode IN ('synthetic', 'live')),
    checkpoint text NOT NULL,
    source_system text NOT NULL,
    transformation_version text NOT NULL,
    retrieved_at timestamptz NOT NULL,
    payload_digest char(64) NOT NULL,
    PRIMARY KEY (site_id, batch_id),
    UNIQUE (site_id, source_mode, checkpoint)
);

CREATE TABLE IF NOT EXISTS metrics.projection_rows (
    site_id text NOT NULL,
    row_id text NOT NULL,
    batch_id text NOT NULL,
    metric_key text NOT NULL,
    definition_version text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    as_of timestamptz NOT NULL,
    value numeric NOT NULL,
    included_count bigint NOT NULL CHECK (included_count >= 0),
    total_count bigint NOT NULL CHECK (total_count >= included_count),
    reconciliation_reference text NOT NULL,
    reconciliation_variance numeric NOT NULL,
    reconciliation_checked_at timestamptz NOT NULL,
    source_record_refs jsonb NOT NULL,
    governed boolean NOT NULL,
    source_lineage jsonb NOT NULL,
    freshness jsonb NOT NULL,
    coverage jsonb NOT NULL,
    reconciliation jsonb NOT NULL,
    payload_digest char(64) NOT NULL,
    PRIMARY KEY (site_id, row_id),
    UNIQUE (site_id, batch_id, row_id),
    FOREIGN KEY (site_id, batch_id)
        REFERENCES metrics.projection_batches (site_id, batch_id) ON DELETE RESTRICT,
    CHECK (window_end >= window_start),
    CHECK (jsonb_typeof(source_record_refs) = 'array'),
    CHECK (jsonb_array_length(source_record_refs) > 0)
);

CREATE TABLE IF NOT EXISTS metrics.checkpoints (
    site_id text NOT NULL,
    source_mode text NOT NULL CHECK (source_mode IN ('synthetic', 'live')),
    checkpoint text NOT NULL,
    batch_id text NOT NULL,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, source_mode, checkpoint),
    FOREIGN KEY (site_id, batch_id)
        REFERENCES metrics.projection_batches (site_id, batch_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS metrics.query_audit (
    site_id text NOT NULL,
    audit_id text NOT NULL,
    request_id text NOT NULL,
    metric_key text NOT NULL,
    source_mode text NOT NULL CHECK (source_mode IN ('synthetic', 'live')),
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    queried_at timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('available', 'unavailable')),
    reason text,
    batch_id text,
    row_id text,
    PRIMARY KEY (site_id, audit_id),
    FOREIGN KEY (site_id, batch_id)
        REFERENCES metrics.projection_batches (site_id, batch_id) ON DELETE RESTRICT,
    FOREIGN KEY (site_id, batch_id, row_id)
        REFERENCES metrics.projection_rows (site_id, batch_id, row_id) ON DELETE RESTRICT,
    CHECK ((batch_id IS NULL AND row_id IS NULL) OR (batch_id IS NOT NULL AND row_id IS NOT NULL)),
    CHECK (window_end >= window_start),
    CHECK (
        reason IS NULL OR reason IN (
            'stale',
            'insufficient_coverage',
            'reconciliation_failed',
            'source_unavailable',
            'definition_unavailable',
            'ungoverned_source'
        )
    ),
    CHECK ((outcome = 'available' AND reason IS NULL) OR (outcome = 'unavailable' AND reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS projection_rows_exact_lookup_idx
    ON metrics.projection_rows (site_id, metric_key, window_start, window_end, batch_id);
CREATE INDEX IF NOT EXISTS checkpoints_latest_idx
    ON metrics.checkpoints (site_id, source_mode, recorded_at DESC, checkpoint DESC);
CREATE INDEX IF NOT EXISTS query_audit_request_idx
    ON metrics.query_audit (site_id, request_id, queried_at DESC);

CREATE OR REPLACE FUNCTION metrics.enforce_single_source_mode()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.site_id, 0));
    IF EXISTS (
        SELECT 1 FROM metrics.projection_batches
        WHERE site_id = NEW.site_id AND source_mode <> NEW.source_mode
    ) THEN
        RAISE EXCEPTION 'synthetic and live projection modes are mutually exclusive';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS projection_batches_single_source_mode ON metrics.projection_batches;
CREATE TRIGGER projection_batches_single_source_mode
    BEFORE INSERT ON metrics.projection_batches
    FOR EACH ROW EXECUTE FUNCTION metrics.enforce_single_source_mode();

CREATE OR REPLACE FUNCTION metrics.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'metrics records are append-only';
END
$$;

DROP TRIGGER IF EXISTS projection_batches_append_only ON metrics.projection_batches;
CREATE TRIGGER projection_batches_append_only
    BEFORE UPDATE OR DELETE ON metrics.projection_batches
    FOR EACH ROW EXECUTE FUNCTION metrics.reject_append_only_mutation();
DROP TRIGGER IF EXISTS projection_rows_append_only ON metrics.projection_rows;
CREATE TRIGGER projection_rows_append_only
    BEFORE UPDATE OR DELETE ON metrics.projection_rows
    FOR EACH ROW EXECUTE FUNCTION metrics.reject_append_only_mutation();
DROP TRIGGER IF EXISTS query_audit_append_only ON metrics.query_audit;
CREATE TRIGGER query_audit_append_only
    BEFORE UPDATE OR DELETE ON metrics.query_audit
    FOR EACH ROW EXECUTE FUNCTION metrics.reject_append_only_mutation();
DROP TRIGGER IF EXISTS checkpoints_append_only ON metrics.checkpoints;
CREATE TRIGGER checkpoints_append_only
    BEFORE UPDATE OR DELETE ON metrics.checkpoints
    FOR EACH ROW EXECUTE FUNCTION metrics.reject_append_only_mutation();

ALTER TABLE metrics.projection_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics.projection_batches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS projection_batches_site_isolation ON metrics.projection_batches;
CREATE POLICY projection_batches_site_isolation ON metrics.projection_batches
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE metrics.projection_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics.projection_rows FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS projection_rows_site_isolation ON metrics.projection_rows;
CREATE POLICY projection_rows_site_isolation ON metrics.projection_rows
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE metrics.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics.checkpoints FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS checkpoints_site_isolation ON metrics.checkpoints;
CREATE POLICY checkpoints_site_isolation ON metrics.checkpoints
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE metrics.query_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics.query_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_audit_site_isolation ON metrics.query_audit;
CREATE POLICY query_audit_site_isolation ON metrics.query_audit
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT USAGE ON SCHEMA metrics TO gbos_metrics_app;
GRANT SELECT, INSERT ON
    metrics.projection_batches,
    metrics.projection_rows,
    metrics.checkpoints,
    metrics.query_audit
TO gbos_metrics_app;
