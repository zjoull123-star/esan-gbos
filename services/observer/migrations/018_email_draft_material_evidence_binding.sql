CREATE OR REPLACE FUNCTION observer.valid_email_draft_material_response(
    operation_value text,
    response_value jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $function$
    SELECT CASE operation_value
        WHEN 'save' THEN
            jsonb_typeof(response_value) = 'object'
            AND response_value - ARRAY['evidence_ref', 'digest', 'revision'] = '{}'::jsonb
            AND response_value->>'evidence_ref' ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'
            AND response_value->>'digest' ~ '^sha256:[a-f0-9]{64}$'
            AND jsonb_typeof(response_value->'revision') = 'number'
            AND (response_value->>'revision')::bigint BETWEEN 1 AND 2147483647
        WHEN 'finalize' THEN
            jsonb_typeof(response_value) = 'object'
            AND response_value - ARRAY[
                'evidence_ref', 'digest', 'role_binding', 'participants'
            ] = '{}'::jsonb
            AND response_value->>'evidence_ref' ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'
            AND response_value->>'digest' ~ '^sha256:[a-f0-9]{64}$'
            AND response_value->>'role_binding' ~ '^sha256:[a-f0-9]{64}$'
            AND jsonb_typeof(response_value->'participants') = 'array'
            AND jsonb_array_length(response_value->'participants') BETWEEN 2 AND 256
            AND NOT EXISTS (
                SELECT 1
                  FROM jsonb_array_elements(response_value->'participants') AS participant
                 WHERE jsonb_typeof(participant) <> 'object'
                    OR participant - ARRAY['address_role', 'opaque_address_ref'] <> '{}'::jsonb
                    OR participant->>'address_role' NOT IN ('sender', 'to', 'cc')
                    OR participant->>'opaque_address_ref'
                        !~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'
            )
        ELSE false
    END
$function$;

REVOKE ALL ON FUNCTION observer.valid_email_draft_material_response(text, jsonb)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION observer.valid_email_draft_material_response(text, jsonb)
    TO gbos_observer_app;

CREATE TABLE IF NOT EXISTS observer.email_draft_material_receipts (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    operation text NOT NULL CHECK (operation IN ('save', 'finalize')),
    idempotency_key text NOT NULL
        CHECK (length(idempotency_key) BETWEEN 8 AND 256),
    request_digest char(71) NOT NULL
        CHECK (request_digest ~ '^sha256:[a-f0-9]{64}$'),
    response jsonb NOT NULL
        CHECK (observer.valid_email_draft_material_response(operation, response)),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS observer.email_draft_evidence_bindings (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    inbox_item_ref text NOT NULL
        CHECK (inbox_item_ref ~ '^INB-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_ref text NOT NULL
        CHECK (draft_ref ~ '^DRF-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_revision bigint NOT NULL CHECK (draft_revision BETWEEN 1 AND 2147483647),
    evidence_ref text NOT NULL
        CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    object_ref text NOT NULL
        CHECK (object_ref ~ '^obs:v1:[a-f0-9]{32}:sha256:[a-f0-9]{64}$'),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    media_type text NOT NULL CHECK (media_type = 'text/plain; charset=utf-8'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 131072),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, inbox_item_ref, draft_ref, draft_revision)
);

CREATE TABLE IF NOT EXISTS observer.email_final_mime_evidence_bindings (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    inbox_item_ref text NOT NULL
        CHECK (inbox_item_ref ~ '^INB-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_ref text NOT NULL
        CHECK (draft_ref ~ '^DRF-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_revision bigint NOT NULL CHECK (draft_revision BETWEEN 1 AND 2147483647),
    evidence_ref text NOT NULL
        CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    object_ref text NOT NULL
        CHECK (object_ref ~ '^obs:v1:[a-f0-9]{32}:sha256:[a-f0-9]{64}$'),
    digest char(71) NOT NULL CHECK (digest ~ '^sha256:[a-f0-9]{64}$'),
    media_type text NOT NULL CHECK (media_type = 'message/rfc822'),
    byte_size bigint NOT NULL CHECK (byte_size BETWEEN 1 AND 262144),
    authorization_receipt_ref text NOT NULL
        CHECK (authorization_receipt_ref ~ '^DAR-[0-9A-HJKMNP-TV-Z]{26}$'),
    gateway_receipt_ref text NOT NULL
        CHECK (gateway_receipt_ref ~ '^EGR-[0-9A-HJKMNP-TV-Z]{26}$'),
    publication_ref text NOT NULL
        CHECK (publication_ref ~ '^PUB-[0-9A-HJKMNP-TV-Z]{26}$'),
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
    participant_roles_digest char(71) NOT NULL
        CHECK (participant_roles_digest ~ '^sha256:[a-f0-9]{64}$'),
    role_binding_digest char(71) NOT NULL
        CHECK (role_binding_digest ~ '^sha256:[a-f0-9]{64}$'),
    source_draft_evidence_ref text NOT NULL
        CHECK (source_draft_evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    source_draft_digest char(71) NOT NULL
        CHECK (source_draft_digest ~ '^sha256:[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, inbox_item_ref, draft_ref, draft_revision),
    FOREIGN KEY (site_id, purpose, source_draft_evidence_ref)
        REFERENCES observer.email_draft_evidence_bindings (site_id, purpose, evidence_ref)
);

CREATE OR REPLACE FUNCTION observer.reject_email_draft_material_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'email_draft_material_immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_email_draft_material_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_draft_material_receipts_immutable
    ON observer.email_draft_material_receipts;
CREATE TRIGGER email_draft_material_receipts_immutable
    BEFORE UPDATE OR DELETE ON observer.email_draft_material_receipts
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_draft_material_mutation();

DROP TRIGGER IF EXISTS email_draft_evidence_bindings_immutable
    ON observer.email_draft_evidence_bindings;
CREATE TRIGGER email_draft_evidence_bindings_immutable
    BEFORE UPDATE OR DELETE ON observer.email_draft_evidence_bindings
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_draft_material_mutation();

DROP TRIGGER IF EXISTS email_final_mime_evidence_bindings_immutable
    ON observer.email_final_mime_evidence_bindings;
CREATE TRIGGER email_final_mime_evidence_bindings_immutable
    BEFORE UPDATE OR DELETE ON observer.email_final_mime_evidence_bindings
    FOR EACH ROW EXECUTE FUNCTION observer.reject_email_draft_material_mutation();

ALTER TABLE observer.email_draft_material_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_draft_material_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_draft_evidence_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_draft_evidence_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.email_final_mime_evidence_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.email_final_mime_evidence_bindings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_draft_material_receipts_site_isolation
    ON observer.email_draft_material_receipts;
CREATE POLICY email_draft_material_receipts_site_isolation
    ON observer.email_draft_material_receipts
    TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_draft_evidence_bindings_site_isolation
    ON observer.email_draft_evidence_bindings;
CREATE POLICY email_draft_evidence_bindings_site_isolation
    ON observer.email_draft_evidence_bindings
    TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

DROP POLICY IF EXISTS email_final_mime_evidence_bindings_site_isolation
    ON observer.email_final_mime_evidence_bindings;
CREATE POLICY email_final_mime_evidence_bindings_site_isolation
    ON observer.email_final_mime_evidence_bindings
    TO gbos_observer_app
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.email_draft_material_receipts FROM PUBLIC;
REVOKE ALL ON observer.email_draft_material_receipts FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_draft_material_receipts TO gbos_observer_app;

REVOKE ALL ON observer.email_draft_evidence_bindings FROM PUBLIC;
REVOKE ALL ON observer.email_draft_evidence_bindings FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_draft_evidence_bindings TO gbos_observer_app;

REVOKE ALL ON observer.email_final_mime_evidence_bindings FROM PUBLIC;
REVOKE ALL ON observer.email_final_mime_evidence_bindings FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.email_final_mime_evidence_bindings TO gbos_observer_app;
