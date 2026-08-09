ALTER TABLE observer.connector_instances
    ADD COLUMN IF NOT EXISTS control_revision bigint NOT NULL DEFAULT 0
        CHECK (control_revision >= 0);

CREATE TABLE IF NOT EXISTS observer.connector_control_commands (
    site_id text NOT NULL,
    idempotency_key text NOT NULL
        CHECK (char_length(idempotency_key) BETWEEN 8 AND 256),
    request_digest char(64) NOT NULL
        CHECK (request_digest ~ '^[a-f0-9]{64}$'),
    connector text NOT NULL CHECK (char_length(connector) BETWEEN 1 AND 80),
    connector_instance_id text NOT NULL
        CHECK (char_length(connector_instance_id) BETWEEN 1 AND 256),
    operation text NOT NULL CHECK (operation IN ('pause', 'resume', 'replay')),
    expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
    result_revision bigint NOT NULL CHECK (result_revision >= 0),
    replayed_count integer NOT NULL DEFAULT 0
        CHECK (replayed_count BETWEEN 0 AND 100),
    response_status jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, idempotency_key),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        )
);

CREATE INDEX IF NOT EXISTS inbound_deliveries_replay_eligible_idx
    ON observer.inbound_deliveries (
        site_id, connector, connector_instance_id, received_at, delivery_id
    )
    WHERE processing_status = 'failed'
      AND object_ref IS NOT NULL
      AND byte_size IS NOT NULL;

CREATE INDEX IF NOT EXISTS observation_events_communication_page_idx
    ON observer.observation_events (
        site_id, occurred_at DESC, event_id DESC
    );

CREATE TABLE IF NOT EXISTS observer.communication_projections (
    site_id text NOT NULL,
    observation_event_id text NOT NULL,
    summary_zh text NOT NULL CHECK (char_length(summary_zh) BETWEEN 1 AND 2000),
    original_language text NOT NULL
        CHECK (char_length(original_language) BETWEEN 1 AND 80),
    review_status text NOT NULL
        CHECK (char_length(review_status) BETWEEN 1 AND 80),
    model_name text NOT NULL CHECK (model_name = 'deepseek-v4-flash'),
    model_version text NOT NULL
        CHECK (char_length(model_version) BETWEEN 1 AND 160),
    fact_proposals jsonb NOT NULL DEFAULT '[]'::jsonb,
    association_suggestions jsonb NOT NULL DEFAULT '[]'::jsonb,
    projected_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, observation_event_id),
    FOREIGN KEY (site_id, observation_event_id)
        REFERENCES observer.observation_events (site_id, event_id)
        ON DELETE CASCADE,
    CHECK (
        jsonb_typeof(fact_proposals) = 'array'
        AND jsonb_array_length(fact_proposals) <= 100
    ),
    CHECK (
        jsonb_typeof(association_suggestions) = 'array'
        AND jsonb_array_length(association_suggestions) <= 100
    )
);

ALTER TABLE observer.connector_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_instances FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_control_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_control_commands FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.communication_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.communication_projections FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS connector_control_commands_site_isolation
    ON observer.connector_control_commands;
CREATE POLICY connector_control_commands_site_isolation
    ON observer.connector_control_commands
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS communication_projections_site_isolation
    ON observer.communication_projections;
CREATE POLICY communication_projections_site_isolation
    ON observer.communication_projections
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, UPDATE ON observer.connector_instances TO gbos_observer_app;
GRANT SELECT, INSERT ON observer.connector_control_commands TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON
    observer.communication_projections
TO gbos_observer_app;
