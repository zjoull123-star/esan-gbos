ALTER TABLE observer.email_connector_config_projections
    ADD COLUMN IF NOT EXISTS mailbox_address_identity_ref text
        CHECK (
            mailbox_address_identity_ref IS NULL
            OR mailbox_address_identity_ref ~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'
        );

CREATE TABLE IF NOT EXISTS observer.email_gateway_inbox_bindings (
    site_id text NOT NULL,
    gateway_receipt_ref text NOT NULL
        CHECK (gateway_receipt_ref ~ '^EGR-[0-9A-HJKMNP-TV-Z]{26}$'),
    publication_ref text NOT NULL
        CHECK (publication_ref ~ '^PUB-[0-9A-HJKMNP-TV-Z]{26}$'),
    inbox_item_ref text NOT NULL
        CHECK (inbox_item_ref ~ '^INB-[0-9A-HJKMNP-TV-Z]{26}$'),
    message_ref text NOT NULL
        CHECK (message_ref ~ '^MSG-[0-9A-HJKMNP-TV-Z]{26}$'),
    mailbox_ref text NOT NULL
        CHECK (mailbox_ref ~ '^MBX-[0-9A-HJKMNP-TV-Z]{26}$'),
    mailbox_config_revision bigint NOT NULL
        CHECK (mailbox_config_revision BETWEEN 1 AND 2147483647),
    observer_delivery_ref text NOT NULL
        CHECK (observer_delivery_ref ~ '^DLV-[0-9A-HJKMNP-TV-Z]{26}$'),
    payload_digest char(71) NOT NULL
        CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    participant_binding_digest char(71) NOT NULL
        CHECK (participant_binding_digest ~ '^sha256:[a-f0-9]{64}$'),
    evidence_binding_digest char(71) NOT NULL
        CHECK (evidence_binding_digest ~ '^sha256:[a-f0-9]{64}$'),
    acknowledged_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, gateway_receipt_ref),
    UNIQUE (site_id, inbox_item_ref),
    UNIQUE (site_id, publication_ref),
    FOREIGN KEY (site_id, publication_ref)
        REFERENCES observer.email_message_publication_outbox (site_id, publication_id)
);

CREATE OR REPLACE FUNCTION observer.reject_email_gateway_inbox_binding_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'email_gateway_inbox_binding_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_gateway_inbox_binding_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_gateway_inbox_binding_immutable
    ON observer.email_gateway_inbox_bindings;
CREATE TRIGGER email_gateway_inbox_binding_immutable
    BEFORE UPDATE OR DELETE ON observer.email_gateway_inbox_bindings
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_gateway_inbox_binding_mutation();

ALTER TABLE observer.email_gateway_inbox_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_gateway_inbox_bindings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_gateway_inbox_bindings_publisher_insert
    ON observer.email_gateway_inbox_bindings;
CREATE POLICY email_gateway_inbox_bindings_publisher_insert
    ON observer.email_gateway_inbox_bindings
    FOR INSERT TO gbos_observer_publisher
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_gateway_inbox_bindings_observer_read
    ON observer.email_gateway_inbox_bindings;
CREATE POLICY email_gateway_inbox_bindings_observer_read
    ON observer.email_gateway_inbox_bindings
    FOR SELECT TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_gateway_inbox_bindings FROM PUBLIC;
REVOKE ALL ON observer.email_gateway_inbox_bindings FROM gbos_observer_publisher;
REVOKE ALL ON observer.email_gateway_inbox_bindings FROM gbos_observer_app;
GRANT INSERT ON observer.email_gateway_inbox_bindings
    TO gbos_observer_publisher;
GRANT SELECT ON observer.email_gateway_inbox_bindings
    TO gbos_observer_app;
