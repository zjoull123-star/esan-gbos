CREATE TABLE IF NOT EXISTS observer.email_connector_config_projections (
    site_id text NOT NULL,
    mailbox_id text NOT NULL CHECK (char_length(mailbox_id) BETWEEN 1 AND 80),
    mailbox_config_revision bigint NOT NULL CHECK (mailbox_config_revision >= 1),
    connector text NOT NULL DEFAULT 'email' CHECK (connector = 'email'),
    connector_instance_id text NOT NULL,
    provider_kind text NOT NULL
        CHECK (provider_kind IN ('wecom_app_mail', 'imap_smtp')),
    activation_watermark text NOT NULL
        CHECK (char_length(activation_watermark) BETWEEN 1 AND 4096),
    activation_not_before timestamptz NOT NULL,
    projection_revision bigint NOT NULL CHECK (projection_revision >= 1),
    projection_digest char(64) NOT NULL CHECK (projection_digest ~ '^[a-f0-9]{64}$'),
    projected_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, mailbox_id, mailbox_config_revision),
    UNIQUE (site_id, connector, connector_instance_id, mailbox_config_revision),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        )
);

CREATE TABLE IF NOT EXISTS observer.email_poll_batches (
    site_id text NOT NULL,
    batch_id text NOT NULL CHECK (char_length(batch_id) BETWEEN 1 AND 80),
    connector text NOT NULL DEFAULT 'email' CHECK (connector = 'email'),
    connector_instance_id text NOT NULL,
    mailbox_id text NOT NULL,
    mailbox_config_revision bigint NOT NULL,
    expected_checkpoint_version bigint NOT NULL
        CHECK (expected_checkpoint_version >= 0),
    expected_cursor text CHECK (char_length(expected_cursor) <= 4096),
    candidate_cursor text CHECK (char_length(candidate_cursor) <= 4096),
    batch_digest char(64) NOT NULL CHECK (batch_digest ~ '^[a-f0-9]{64}$'),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'finalized')),
    created_at timestamptz NOT NULL,
    finalized_at timestamptz,
    PRIMARY KEY (site_id, batch_id),
    UNIQUE (
        site_id, connector, connector_instance_id,
        expected_checkpoint_version, batch_digest
    ),
    FOREIGN KEY (site_id, connector, connector_instance_id)
        REFERENCES observer.connector_instances (
            site_id, connector, connector_instance_id
        ),
    FOREIGN KEY (site_id, mailbox_id, mailbox_config_revision)
        REFERENCES observer.email_connector_config_projections (
            site_id, mailbox_id, mailbox_config_revision
        ),
    CHECK (
        (status = 'open' AND finalized_at IS NULL)
        OR (status = 'finalized' AND finalized_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS observer.email_poll_batch_deliveries (
    site_id text NOT NULL,
    batch_id text NOT NULL,
    connector text NOT NULL DEFAULT 'email' CHECK (connector = 'email'),
    connector_instance_id text NOT NULL,
    delivery_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 999),
    terminal_kind text CHECK (terminal_kind IN ('published', 'quarantined')),
    terminal_ref_digest char(64)
        CHECK (terminal_ref_digest ~ '^[a-f0-9]{64}$'),
    terminal_at timestamptz,
    PRIMARY KEY (site_id, batch_id, delivery_id),
    UNIQUE (site_id, batch_id, ordinal),
    FOREIGN KEY (site_id, batch_id)
        REFERENCES observer.email_poll_batches (site_id, batch_id),
    CHECK (
        (terminal_kind IS NULL AND terminal_ref_digest IS NULL AND terminal_at IS NULL)
        OR (
            terminal_kind IS NOT NULL
            AND terminal_ref_digest IS NOT NULL
            AND terminal_at IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS observer.email_message_publication_outbox (
    site_id text NOT NULL,
    publication_id text NOT NULL CHECK (char_length(publication_id) BETWEEN 1 AND 80),
    mailbox_id text NOT NULL,
    mailbox_config_revision bigint NOT NULL,
    connector text NOT NULL DEFAULT 'email' CHECK (connector = 'email'),
    connector_instance_id text NOT NULL,
    observer_delivery_ref text NOT NULL
        CHECK (char_length(observer_delivery_ref) BETWEEN 1 AND 512),
    publication_revision bigint NOT NULL CHECK (publication_revision >= 1),
    idempotency_key text NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 256),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
    relay_status text NOT NULL DEFAULT 'queued'
        CHECK (relay_status IN (
            'queued', 'leased', 'retry_wait', 'delivered', 'dead_letter'
        )),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 5),
    max_attempts smallint NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 5),
    next_attempt_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 128),
    lease_expires_at timestamptz,
    relay_generation bigint NOT NULL DEFAULT 0 CHECK (relay_generation >= 0),
    last_error_code text CHECK (char_length(last_error_code) BETWEEN 1 AND 128),
    delivery_receipt jsonb CHECK (jsonb_typeof(delivery_receipt) = 'object'),
    delivery_receipt_digest char(64)
        CHECK (delivery_receipt_digest ~ '^[a-f0-9]{64}$'),
    delivered_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (site_id, publication_id),
    UNIQUE (site_id, mailbox_id, observer_delivery_ref),
    UNIQUE (site_id, idempotency_key),
    FOREIGN KEY (site_id, mailbox_id, mailbox_config_revision)
        REFERENCES observer.email_connector_config_projections (
            site_id, mailbox_id, mailbox_config_revision
        ),
    FOREIGN KEY (
        site_id, connector, connector_instance_id, observer_delivery_ref
    ) REFERENCES observer.inbound_deliveries (
        site_id, connector, connector_instance_id, delivery_id
    ),
    CHECK (attempt_count <= max_attempts),
    CHECK (
        (relay_status = 'leased' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (relay_status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (relay_status = 'delivered'
            AND delivery_receipt IS NOT NULL
            AND delivery_receipt_digest IS NOT NULL
            AND delivered_at IS NOT NULL)
        OR (relay_status <> 'delivered'
            AND delivery_receipt IS NULL
            AND delivery_receipt_digest IS NULL
            AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS email_message_publication_outbox_relay_claim_idx
    ON observer.email_message_publication_outbox (
        site_id, next_attempt_at, publication_id
    )
    WHERE relay_status IN ('queued', 'retry_wait', 'leased');

CREATE OR REPLACE FUNCTION observer.reject_email_publication_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR (
        NEW.site_id IS DISTINCT FROM OLD.site_id
        OR NEW.publication_id IS DISTINCT FROM OLD.publication_id
        OR NEW.mailbox_id IS DISTINCT FROM OLD.mailbox_id
        OR NEW.mailbox_config_revision IS DISTINCT FROM OLD.mailbox_config_revision
        OR NEW.connector IS DISTINCT FROM OLD.connector
        OR NEW.connector_instance_id IS DISTINCT FROM OLD.connector_instance_id
        OR NEW.observer_delivery_ref IS DISTINCT FROM OLD.observer_delivery_ref
        OR NEW.publication_revision IS DISTINCT FROM OLD.publication_revision
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
        OR NEW.payload IS DISTINCT FROM OLD.payload
        OR NEW.payload_digest IS DISTINCT FROM OLD.payload_digest
        OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'email_message_publication_outbox_immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_publication_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_message_publication_outbox_immutable
    ON observer.email_message_publication_outbox;
CREATE TRIGGER email_message_publication_outbox_immutable
    BEFORE UPDATE OR DELETE ON observer.email_message_publication_outbox
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_publication_mutation();

CREATE OR REPLACE FUNCTION observer.reject_email_terminal_rewrite()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF OLD.terminal_kind IS NOT NULL AND ROW(OLD.*) IS DISTINCT FROM ROW(NEW.*) THEN
        RAISE EXCEPTION 'email_poll_batch_delivery_terminal_immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_terminal_rewrite() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_poll_batch_delivery_terminal_immutable
    ON observer.email_poll_batch_deliveries;
CREATE TRIGGER email_poll_batch_delivery_terminal_immutable
    BEFORE UPDATE ON observer.email_poll_batch_deliveries
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_terminal_rewrite();

ALTER TABLE observer.email_connector_config_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_connector_config_projections FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_poll_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_poll_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_poll_batch_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_poll_batch_deliveries FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_message_publication_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_message_publication_outbox FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_connector_config_projections_site_isolation
    ON observer.email_connector_config_projections;
CREATE POLICY email_connector_config_projections_site_isolation
    ON observer.email_connector_config_projections
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_poll_batches_site_isolation
    ON observer.email_poll_batches;
CREATE POLICY email_poll_batches_site_isolation
    ON observer.email_poll_batches
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_poll_batch_deliveries_site_isolation
    ON observer.email_poll_batch_deliveries;
CREATE POLICY email_poll_batch_deliveries_site_isolation
    ON observer.email_poll_batch_deliveries
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_message_publication_outbox_site_isolation
    ON observer.email_message_publication_outbox;
CREATE POLICY email_message_publication_outbox_site_isolation
    ON observer.email_message_publication_outbox
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_connector_config_projections FROM PUBLIC;
REVOKE ALL ON observer.email_poll_batches FROM PUBLIC;
REVOKE ALL ON observer.email_poll_batch_deliveries FROM PUBLIC;
REVOKE ALL ON observer.email_message_publication_outbox FROM PUBLIC;
REVOKE ALL ON observer.email_connector_config_projections FROM gbos_observer_app;
REVOKE ALL ON observer.email_poll_batches FROM gbos_observer_app;
REVOKE ALL ON observer.email_poll_batch_deliveries FROM gbos_observer_app;
REVOKE ALL ON observer.email_message_publication_outbox FROM gbos_observer_app;

GRANT SELECT, INSERT ON observer.email_connector_config_projections
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.email_poll_batches
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.email_poll_batch_deliveries
    TO gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_message_publication_outbox
    TO gbos_observer_app;
GRANT UPDATE (
    relay_status, attempt_count, next_attempt_at, lease_owner,
    lease_expires_at, relay_generation, last_error_code,
    delivery_receipt, delivery_receipt_digest, delivered_at, updated_at
) ON observer.email_message_publication_outbox TO gbos_observer_app;
