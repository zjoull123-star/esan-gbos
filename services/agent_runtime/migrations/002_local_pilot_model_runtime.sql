CREATE TABLE IF NOT EXISTS agent_runtime.model_invocations (
    site_id text NOT NULL,
    invocation_id text NOT NULL,
    idempotency_key text NOT NULL,
    provider text NOT NULL,
    requested_model text NOT NULL,
    observed_model text,
    prompt_version text NOT NULL,
    output_schema_version text NOT NULL,
    policy_version text NOT NULL,
    tokenizer_version text NOT NULL,
    request_id text NOT NULL,
    response_id text,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    latency_ms bigint,
    status text NOT NULL,
    token_usage_status text NOT NULL,
    input_tokens bigint,
    output_tokens bigint,
    total_tokens bigint,
    cost_status text NOT NULL,
    cost_amount numeric,
    cost_currency text,
    network_call_count integer NOT NULL,
    tool_call_count integer NOT NULL,
    external_send_count integer NOT NULL,
    observation_event_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    tokenization_receipt_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    attempt integer NOT NULL,
    retry_count integer NOT NULL,
    finish_code text,
    error_code text,
    budget_status text NOT NULL,
    price_catalog_version text,
    output_digest char(64),
    PRIMARY KEY (site_id, invocation_id),
    UNIQUE (site_id, idempotency_key),
    CHECK (site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$'),
    CHECK (length(invocation_id) BETWEEN 1 AND 256),
    CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    CHECK (length(provider) BETWEEN 1 AND 80),
    CHECK (length(requested_model) BETWEEN 1 AND 160),
    CHECK (observed_model IS NULL OR length(observed_model) BETWEEN 1 AND 160),
    CHECK (length(prompt_version) BETWEEN 1 AND 80),
    CHECK (length(output_schema_version) BETWEEN 1 AND 80),
    CHECK (length(policy_version) BETWEEN 1 AND 80),
    CHECK (length(tokenizer_version) BETWEEN 1 AND 80),
    CHECK (length(request_id) BETWEEN 1 AND 256),
    CHECK (response_id IS NULL OR length(response_id) BETWEEN 1 AND 256),
    CHECK (
        (completed_at IS NULL AND latency_ms IS NULL)
        OR
        (
            completed_at IS NOT NULL
            AND completed_at >= started_at
            AND latency_ms >= 0
        )
    ),
    CHECK (status IN ('succeeded', 'failed', 'timed_out', 'cancelled')),
    CHECK (
        (
            token_usage_status = 'unknown'
            AND input_tokens IS NULL
            AND output_tokens IS NULL
            AND total_tokens IS NULL
        )
        OR
        (
            token_usage_status = 'known'
            AND input_tokens >= 0
            AND output_tokens >= 0
            AND total_tokens = input_tokens + output_tokens
        )
    ),
    CHECK (
        (
            cost_status = 'unknown'
            AND cost_amount IS NULL
            AND cost_currency IS NULL
        )
        OR
        (
            cost_status = 'known'
            AND cost_amount >= 0
            AND cost_amount <> 'Infinity'::numeric
            AND cost_currency ~ '^[A-Z]{3}$'
            AND length(price_catalog_version) BETWEEN 1 AND 80
        )
    ),
    CHECK (
        network_call_count >= 0
        AND tool_call_count >= 0
        AND external_send_count >= 0
        AND attempt >= 1
        AND retry_count >= 0
        AND (
            (network_call_count = 0 AND retry_count = 0)
            OR
            (network_call_count > 0 AND retry_count < network_call_count)
        )
    ),
    CHECK (jsonb_typeof(observation_event_refs) = 'array'),
    CHECK (jsonb_typeof(evidence_refs) = 'array'),
    CHECK (jsonb_typeof(tokenization_receipt_refs) = 'array'),
    CHECK (finish_code IS NULL OR finish_code ~ '^[a-z][a-z0-9_]{0,79}$'),
    CHECK (
        error_code IS NULL
        OR error_code IN (
            'network_disabled',
            'budget_hard_stop',
            'input_token_limit',
            'transport_exhausted',
            'retry_exhausted',
            'provider_http_error',
            'response_invalid_json',
            'model_mismatch',
            'response_protocol_error',
            'output_invalid_json',
            'output_schema_invalid',
            'request_binding_failed',
            'unsafe_output',
            'pricing_error',
            'internal_error'
        )
    ),
    CHECK (
        (
            status = 'succeeded'
            AND error_code IS NULL
            AND output_digest ~ '^[a-f0-9]{64}$'
        )
        OR
        (
            status <> 'succeeded'
            AND error_code IS NOT NULL
            AND (output_digest IS NULL OR output_digest ~ '^[a-f0-9]{64}$')
        )
    ),
    CHECK (budget_status IN ('normal', 'warning', 'hard_stop', 'network_disabled', 'unknown'))
);

CREATE INDEX IF NOT EXISTS model_invocations_site_started_idx
    ON agent_runtime.model_invocations (site_id, started_at ASC, invocation_id ASC);

ALTER TABLE agent_runtime.model_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.model_invocations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS model_invocations_site_isolation
    ON agent_runtime.model_invocations;
CREATE POLICY model_invocations_site_isolation
    ON agent_runtime.model_invocations
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, INSERT, UPDATE ON agent_runtime.model_invocations TO gbos_agent_app;
