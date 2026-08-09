CREATE TABLE IF NOT EXISTS observer.connector_instances (
    site_id text NOT NULL,
    connector text NOT NULL CHECK (char_length(connector) BETWEEN 1 AND 80),
    connector_instance_id text NOT NULL
        CHECK (char_length(connector_instance_id) BETWEEN 1 AND 256),
    status text NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'paused', 'degraded', 'failed')),
    registered_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, connector, connector_instance_id)
);

CREATE TABLE IF NOT EXISTS observer.inbound_deliveries (
    site_id text NOT NULL,
    connector text NOT NULL,
    connector_instance_id text NOT NULL,
    delivery_id text NOT NULL CHECK (char_length(delivery_id) BETWEEN 1 AND 512),
    exact_body_sha256 char(64) NOT NULL
        CHECK (exact_body_sha256 ~ '^[a-f0-9]{64}$'),
    media_type text NOT NULL CHECK (char_length(media_type) BETWEEN 1 AND 255),
    received_at timestamptz NOT NULL,
    processing_status text NOT NULL DEFAULT 'received'
        CHECK (
            processing_status IN (
                'received', 'authenticated', 'queued', 'processing',
                'succeeded', 'failed', 'quarantined'
            )
        ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    correlation_id text NOT NULL CHECK (char_length(correlation_id) BETWEEN 1 AND 256),
    last_attempt_at timestamptz,
    last_error_code text CHECK (char_length(last_error_code) <= 80),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, connector, connector_instance_id, delivery_id),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        )
);

CREATE TABLE IF NOT EXISTS observer.inbound_delivery_events (
    site_id text NOT NULL,
    connector text NOT NULL,
    connector_instance_id text NOT NULL,
    provider_event_id text NOT NULL
        CHECK (char_length(provider_event_id) BETWEEN 1 AND 512),
    delivery_id text NOT NULL,
    linked_at timestamptz NOT NULL,
    PRIMARY KEY (
        site_id, connector, connector_instance_id, provider_event_id
    ),
    FOREIGN KEY (
        site_id, connector, connector_instance_id, delivery_id
    ) REFERENCES observer.inbound_deliveries (
        site_id, connector, connector_instance_id, delivery_id
    )
);

CREATE TABLE IF NOT EXISTS observer.connector_checkpoints (
    site_id text NOT NULL,
    connector text NOT NULL,
    connector_instance_id text NOT NULL,
    checkpoint_id text NOT NULL CHECK (char_length(checkpoint_id) BETWEEN 1 AND 256),
    cursor_value text CHECK (char_length(cursor_value) <= 4096),
    checkpoint_version bigint NOT NULL DEFAULT 0 CHECK (checkpoint_version >= 0),
    replay_window_seconds integer NOT NULL DEFAULT 0
        CHECK (replay_window_seconds BETWEEN 0 AND 31536000),
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 256),
    lease_expires_at timestamptz,
    last_success_at timestamptz,
    last_error_code text CHECK (char_length(last_error_code) <= 80),
    status text NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'paused', 'degraded', 'failed')),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, connector, connector_instance_id),
    UNIQUE (site_id, checkpoint_id),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        ),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS observer.persistent_nonces (
    site_id text NOT NULL,
    identity_ref text NOT NULL CHECK (char_length(identity_ref) BETWEEN 1 AND 256),
    nonce_sha256 char(64) NOT NULL CHECK (nonce_sha256 ~ '^[a-f0-9]{64}$'),
    consumed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, identity_ref, nonce_sha256),
    CHECK (expires_at > consumed_at)
);

CREATE TABLE IF NOT EXISTS observer.processing_jobs (
    site_id text NOT NULL,
    job_id text NOT NULL CHECK (char_length(job_id) BETWEEN 1 AND 256),
    connector text NOT NULL,
    connector_instance_id text NOT NULL,
    delivery_id text,
    stage text NOT NULL CHECK (char_length(stage) BETWEEN 1 AND 80),
    status text NOT NULL
        CHECK (
            status IN (
                'queued', 'processing', 'retry_wait', 'succeeded',
                'failed', 'quarantined', 'dead_letter'
            )
        ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    next_retry_at timestamptz,
    last_error_code text CHECK (char_length(last_error_code) <= 80),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, job_id),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        ),
    FOREIGN KEY (
        site_id, connector, connector_instance_id, delivery_id
    ) REFERENCES observer.inbound_deliveries (
        site_id, connector, connector_instance_id, delivery_id
    )
);

CREATE TABLE IF NOT EXISTS observer.context_publication_outbox (
    site_id text NOT NULL,
    outbox_id text NOT NULL CHECK (char_length(outbox_id) BETWEEN 1 AND 256),
    observation_event_id text NOT NULL,
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 256),
    payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    status text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'leased', 'retry_wait', 'published', 'dead_letter')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    next_retry_at timestamptz NOT NULL,
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 256),
    lease_expires_at timestamptz,
    last_error_code text CHECK (char_length(last_error_code) <= 80),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, outbox_id),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, observation_event_id)
        REFERENCES observer.observation_events (site_id, event_id),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS observer.local_pilot_quarantine (
    site_id text NOT NULL,
    quarantine_id text NOT NULL CHECK (char_length(quarantine_id) BETWEEN 1 AND 256),
    job_id text,
    delivery_id text,
    reason_code text NOT NULL CHECK (char_length(reason_code) BETWEEN 1 AND 80),
    created_at timestamptz NOT NULL,
    released_at timestamptz,
    PRIMARY KEY (site_id, quarantine_id),
    FOREIGN KEY (site_id, job_id)
        REFERENCES observer.processing_jobs (site_id, job_id)
);

CREATE TABLE IF NOT EXISTS observer.local_pilot_dead_letter (
    site_id text NOT NULL,
    dead_letter_id text NOT NULL CHECK (char_length(dead_letter_id) BETWEEN 1 AND 256),
    job_id text,
    outbox_id text,
    reason_code text NOT NULL CHECK (char_length(reason_code) BETWEEN 1 AND 80),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, dead_letter_id),
    FOREIGN KEY (site_id, job_id)
        REFERENCES observer.processing_jobs (site_id, job_id),
    FOREIGN KEY (site_id, outbox_id)
        REFERENCES observer.context_publication_outbox (site_id, outbox_id)
);

ALTER TABLE observer.observation_events
    ADD COLUMN IF NOT EXISTS connector_instance_id text;
UPDATE observer.observation_events
SET connector_instance_id = 'legacy-manual-import'
WHERE connector_instance_id IS NULL;
ALTER TABLE observer.observation_events
    ALTER COLUMN connector_instance_id SET DEFAULT 'legacy-manual-import',
    ALTER COLUMN connector_instance_id SET NOT NULL;

ALTER TABLE observer.observation_events
    DROP CONSTRAINT IF EXISTS observation_events_site_id_connector_provider_event_id_key;
DROP INDEX IF EXISTS observer.observation_events_fallback_dedup_uq;
CREATE UNIQUE INDEX IF NOT EXISTS observation_events_instance_provider_dedup_uq
    ON observer.observation_events (
        site_id, connector, connector_instance_id, provider_event_id
    )
    WHERE provider_event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS observation_events_instance_fallback_dedup_uq
    ON observer.observation_events (
        site_id, connector, connector_instance_id, raw_sha256, occurred_minute
    )
    WHERE provider_event_id IS NULL;

ALTER TABLE observer.connector_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_instances FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS connector_instances_site_isolation
    ON observer.connector_instances;
CREATE POLICY connector_instances_site_isolation ON observer.connector_instances
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.inbound_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_deliveries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_deliveries_site_isolation
    ON observer.inbound_deliveries;
CREATE POLICY inbound_deliveries_site_isolation ON observer.inbound_deliveries
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.inbound_delivery_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.inbound_delivery_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_delivery_events_site_isolation
    ON observer.inbound_delivery_events;
CREATE POLICY inbound_delivery_events_site_isolation ON observer.inbound_delivery_events
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.connector_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_checkpoints FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS connector_checkpoints_site_isolation
    ON observer.connector_checkpoints;
CREATE POLICY connector_checkpoints_site_isolation ON observer.connector_checkpoints
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.persistent_nonces ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.persistent_nonces FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS persistent_nonces_site_isolation
    ON observer.persistent_nonces;
CREATE POLICY persistent_nonces_site_isolation ON observer.persistent_nonces
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.processing_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.processing_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS processing_jobs_site_isolation
    ON observer.processing_jobs;
CREATE POLICY processing_jobs_site_isolation ON observer.processing_jobs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.context_publication_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS context_publication_outbox_site_isolation
    ON observer.context_publication_outbox;
CREATE POLICY context_publication_outbox_site_isolation
    ON observer.context_publication_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.local_pilot_quarantine ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_quarantine FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS local_pilot_quarantine_site_isolation
    ON observer.local_pilot_quarantine;
CREATE POLICY local_pilot_quarantine_site_isolation
    ON observer.local_pilot_quarantine
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.local_pilot_dead_letter ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.local_pilot_dead_letter FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS local_pilot_dead_letter_site_isolation
    ON observer.local_pilot_dead_letter;
CREATE POLICY local_pilot_dead_letter_site_isolation
    ON observer.local_pilot_dead_letter
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

GRANT SELECT, INSERT, UPDATE ON
    observer.connector_instances,
    observer.inbound_deliveries,
    observer.connector_checkpoints,
    observer.processing_jobs,
    observer.context_publication_outbox,
    observer.local_pilot_quarantine,
    observer.local_pilot_dead_letter
TO gbos_observer_app;
GRANT SELECT, INSERT ON
    observer.inbound_delivery_events,
    observer.persistent_nonces
TO gbos_observer_app;
