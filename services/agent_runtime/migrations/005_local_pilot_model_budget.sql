CREATE TABLE IF NOT EXISTS agent_runtime.model_budget_reservations (
    site_id text NOT NULL,
    reservation_id text NOT NULL,
    month_start date NOT NULL,
    state text NOT NULL,
    maximum_amount_usd numeric NOT NULL,
    charged_amount_usd numeric NOT NULL,
    price_catalog_version text NOT NULL,
    token_counter_version text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, reservation_id),
    CHECK (site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$'),
    CHECK (length(reservation_id) BETWEEN 1 AND 256),
    CHECK (extract(day FROM month_start) = 1),
    CHECK (state IN ('reserved', 'settled', 'consumed')),
    CHECK (
        maximum_amount_usd >= 0
        AND maximum_amount_usd <> 'Infinity'::numeric
        AND charged_amount_usd >= 0
        AND charged_amount_usd <= maximum_amount_usd
        AND charged_amount_usd <> 'Infinity'::numeric
    ),
    CHECK (
        (state = 'settled' AND charged_amount_usd <= maximum_amount_usd)
        OR
        (state IN ('reserved', 'consumed') AND charged_amount_usd = maximum_amount_usd)
    ),
    CHECK (length(price_catalog_version) BETWEEN 1 AND 80),
    CHECK (length(token_counter_version) BETWEEN 1 AND 80),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS model_budget_reservations_site_month_idx
    ON agent_runtime.model_budget_reservations (site_id, month_start);

ALTER TABLE agent_runtime.model_budget_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runtime.model_budget_reservations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS model_budget_reservations_site_isolation
    ON agent_runtime.model_budget_reservations;
CREATE POLICY model_budget_reservations_site_isolation
    ON agent_runtime.model_budget_reservations
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, INSERT, DELETE
    ON agent_runtime.model_budget_reservations
    TO gbos_agent_app;
GRANT UPDATE (state, charged_amount_usd, updated_at)
    ON agent_runtime.model_budget_reservations
    TO gbos_agent_app;
