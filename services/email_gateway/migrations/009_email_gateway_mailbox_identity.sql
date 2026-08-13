ALTER TABLE email_gateway.mailboxes
    ADD COLUMN IF NOT EXISTS mailbox_address_identity_ref text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'email_gateway.mailboxes'::regclass
           AND conname = 'mailboxes_mailbox_address_identity_ref_check'
    ) THEN
        ALTER TABLE email_gateway.mailboxes
            ADD CONSTRAINT mailboxes_mailbox_address_identity_ref_check
            CHECK (
                mailbox_address_identity_ref IS NULL
                OR mailbox_address_identity_ref
                    ~ '^extid:v1:email:[A-Za-z0-9_-]{43}$'
            );
    END IF;
END
$$;

ALTER TABLE email_gateway.mailboxes ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.mailboxes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON email_gateway.mailboxes FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON email_gateway.mailboxes TO gbos_email_gateway_app;
GRANT SELECT ON email_gateway.mailboxes TO gbos_email_gateway_worker;
