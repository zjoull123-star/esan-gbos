ALTER TABLE email_gateway.publication_receipts
    ADD COLUMN IF NOT EXISTS mailbox_config_revision bigint
        CHECK (
            mailbox_config_revision IS NULL
            OR mailbox_config_revision BETWEEN 1 AND 2147483647
        ),
    ADD COLUMN IF NOT EXISTS participant_binding_digest text
        CHECK (
            participant_binding_digest IS NULL
            OR participant_binding_digest ~ '^sha256:[a-f0-9]{64}$'
        ),
    ADD COLUMN IF NOT EXISTS evidence_binding_digest text
        CHECK (
            evidence_binding_digest IS NULL
            OR evidence_binding_digest ~ '^sha256:[a-f0-9]{64}$'
        );
