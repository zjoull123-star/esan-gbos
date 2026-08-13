DO $$
DECLARE
    legacy_constraint name;
BEGIN
    SELECT constraint_record.conname
      INTO legacy_constraint
      FROM pg_constraint AS constraint_record
      JOIN pg_class AS table_record
        ON table_record.oid = constraint_record.conrelid
      JOIN pg_namespace AS schema_record
        ON schema_record.oid = table_record.relnamespace
     WHERE schema_record.nspname = 'email_gateway'
       AND table_record.relname = 'identity_projection_receipts'
       AND constraint_record.contype = 'u'
       AND pg_get_constraintdef(constraint_record.oid) =
           'UNIQUE (site_id, opaque_address_ref, external_identity_revision)'
     LIMIT 1;
    IF legacy_constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE email_gateway.identity_projection_receipts DROP CONSTRAINT %I',
            legacy_constraint
        );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS identity_projection_receipts_purpose_revision_uq
    ON email_gateway.identity_projection_receipts (
        site_id, processing_purpose, opaque_address_ref,
        external_identity_revision
    );

ALTER TABLE email_gateway.identity_projection_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.identity_projection_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.identity_projection_receipts FROM PUBLIC;

REVOKE ALL ON email_gateway.identity_projection_receipts
    FROM gbos_email_gateway_app;
GRANT SELECT, INSERT ON email_gateway.identity_projection_receipts
    TO gbos_email_gateway_app;
