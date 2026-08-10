CREATE TABLE IF NOT EXISTS observer.model_fatal_latches (
    site_id text NOT NULL
        CHECK (char_length(site_id) BETWEEN 1 AND 140),
    processing_purpose text NOT NULL
        CHECK (char_length(processing_purpose) BETWEEN 1 AND 80),
    error_code text NOT NULL CHECK (
        error_code IN (
            'budget_hard_stop',
            'input_token_limit',
            'internal_error',
            'invalid_model_output',
            'model_binding_mismatch',
            'model_mismatch',
            'model_provider_failed',
            'output_invalid_json',
            'output_schema_invalid',
            'pricing_error',
            'provider_http_error',
            'request_binding_failed',
            'response_invalid_json',
            'response_protocol_error',
            'unsafe_output'
        )
    ),
    latched_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, processing_purpose)
);

CREATE OR REPLACE FUNCTION observer.reject_model_fatal_latch_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'model fatal latch is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_model_fatal_latch_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS model_fatal_latches_immutable
    ON observer.model_fatal_latches;
CREATE TRIGGER model_fatal_latches_immutable
    BEFORE UPDATE OR DELETE ON observer.model_fatal_latches
    FOR EACH ROW
    EXECUTE FUNCTION observer.reject_model_fatal_latch_mutation();

ALTER TABLE observer.model_fatal_latches ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.model_fatal_latches FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS model_fatal_latches_scope_isolation
    ON observer.model_fatal_latches;
CREATE POLICY model_fatal_latches_scope_isolation
    ON observer.model_fatal_latches
    USING (
        site_id = current_setting('app.site_id', true)
        AND processing_purpose =
            current_setting('app.processing_purpose', true)
    )
    WITH CHECK (
        site_id = current_setting('app.site_id', true)
        AND processing_purpose =
            current_setting('app.processing_purpose', true)
    );

REVOKE ALL ON observer.model_fatal_latches FROM PUBLIC;
REVOKE ALL ON observer.model_fatal_latches FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.model_fatal_latches TO gbos_observer_app;
