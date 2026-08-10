DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        WHERE con.contype = 'c'
          AND con.conrelid = 'observer.participant_identity_resolutions'::regclass
          AND pg_get_constraintdef(con.oid) LIKE '%external_subject_ref%'
    LOOP
        EXECUTE format(
            'ALTER TABLE observer.participant_identity_resolutions DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;

    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        WHERE con.contype = 'c'
          AND con.conrelid = 'observer.identity_resolution_work'::regclass
          AND pg_get_constraintdef(con.oid) LIKE '%identity_ref%'
    LOOP
        EXECUTE format(
            'ALTER TABLE observer.identity_resolution_work DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;

    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        WHERE con.contype = 'c'
          AND con.conrelid = 'observer.identity_authority_denials'::regclass
          AND pg_get_constraintdef(con.oid) LIKE '%identity_ref%'
    LOOP
        EXECUTE format(
            'ALTER TABLE observer.identity_authority_denials DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'participant_identity_resolutions_digest_ref_ck'
          AND conrelid = 'observer.participant_identity_resolutions'::regclass
    ) THEN
        ALTER TABLE observer.participant_identity_resolutions
            ADD CONSTRAINT participant_identity_resolutions_digest_ref_ck
            CHECK (
                external_subject_ref ~ (
                    '^extid:v1:' || identity_provider
                    || ':[A-Za-z0-9_-]{43}$'
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'identity_resolution_work_digest_ref_ck'
          AND conrelid = 'observer.identity_resolution_work'::regclass
    ) THEN
        ALTER TABLE observer.identity_resolution_work
            ADD CONSTRAINT identity_resolution_work_digest_ref_ck
            CHECK (
                identity_ref ~ (
                    '^extid:v1:' || identity_provider
                    || ':[A-Za-z0-9_-]{43}$'
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'identity_authority_denials_digest_ref_ck'
          AND conrelid = 'observer.identity_authority_denials'::regclass
    ) THEN
        ALTER TABLE observer.identity_authority_denials
            ADD CONSTRAINT identity_authority_denials_digest_ref_ck
            CHECK (
                identity_ref ~ (
                    '^extid:v1:' || identity_provider
                    || ':[A-Za-z0-9_-]{43}$'
                )
            ) NOT VALID;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'participants_external_identity_digest_ref_ck'
          AND conrelid = 'observer.participants'::regclass
    ) THEN
        ALTER TABLE observer.participants
            ADD CONSTRAINT participants_external_identity_digest_ref_ck
            CHECK (
                identity_ref !~ '^extid:v1:'
                OR identity_ref ~ (
                    '^extid:v1:(email|wecom|whatsapp|phone|manual_import):'
                    || '[A-Za-z0-9_-]{43}$'
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.participant_identity_resolutions
    VALIDATE CONSTRAINT participant_identity_resolutions_digest_ref_ck;
ALTER TABLE observer.identity_resolution_work
    VALIDATE CONSTRAINT identity_resolution_work_digest_ref_ck;
ALTER TABLE observer.identity_authority_denials
    VALIDATE CONSTRAINT identity_authority_denials_digest_ref_ck;
ALTER TABLE observer.participants
    VALIDATE CONSTRAINT participants_external_identity_digest_ref_ck;

CREATE OR REPLACE FUNCTION observer.enforce_external_identity_digest_boundary()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
DECLARE
    provider text;
    identity text;
BEGIN
    IF TG_TABLE_NAME = 'participant_identity_resolutions' THEN
        provider := to_jsonb(NEW)->>'identity_provider';
        identity := to_jsonb(NEW)->>'external_subject_ref';
    ELSIF TG_TABLE_NAME IN (
        'identity_resolution_work', 'identity_authority_denials'
    ) THEN
        provider := to_jsonb(NEW)->>'identity_provider';
        identity := to_jsonb(NEW)->>'identity_ref';
    ELSE
        identity := to_jsonb(NEW)->>'identity_ref';
    END IF;

    IF identity LIKE 'extid:v1:%' AND identity !~ (
        '^extid:v1:(email|wecom|whatsapp|phone|manual_import):'
        || '[A-Za-z0-9_-]{43}$'
    ) THEN
        RAISE EXCEPTION 'external identity digest boundary rejected'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF provider IS NOT NULL AND identity !~ (
        '^extid:v1:' || provider || ':[A-Za-z0-9_-]{43}$'
    ) THEN
        RAISE EXCEPTION 'external identity provider binding rejected'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

REVOKE ALL ON FUNCTION observer.enforce_external_identity_digest_boundary()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS participant_identity_digest_boundary
    ON observer.participants;
CREATE TRIGGER participant_identity_digest_boundary
    BEFORE INSERT OR UPDATE ON observer.participants
    FOR EACH ROW EXECUTE FUNCTION observer.enforce_external_identity_digest_boundary();

DROP TRIGGER IF EXISTS participant_resolution_digest_boundary
    ON observer.participant_identity_resolutions;
CREATE TRIGGER participant_resolution_digest_boundary
    BEFORE INSERT OR UPDATE ON observer.participant_identity_resolutions
    FOR EACH ROW EXECUTE FUNCTION observer.enforce_external_identity_digest_boundary();

DROP TRIGGER IF EXISTS identity_resolution_work_digest_boundary
    ON observer.identity_resolution_work;
CREATE TRIGGER identity_resolution_work_digest_boundary
    BEFORE INSERT OR UPDATE ON observer.identity_resolution_work
    FOR EACH ROW EXECUTE FUNCTION observer.enforce_external_identity_digest_boundary();

DROP TRIGGER IF EXISTS identity_authority_denial_digest_boundary
    ON observer.identity_authority_denials;
CREATE TRIGGER identity_authority_denial_digest_boundary
    BEFORE INSERT OR UPDATE ON observer.identity_authority_denials
    FOR EACH ROW EXECUTE FUNCTION observer.enforce_external_identity_digest_boundary();

ALTER TABLE observer.participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.participants FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.participant_identity_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.participant_identity_resolutions FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_resolution_work ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_resolution_work FORCE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_authority_denials ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_authority_denials FORCE ROW LEVEL SECURITY;

REVOKE ALL ON observer.participants FROM PUBLIC;
REVOKE ALL ON observer.participant_identity_resolutions FROM PUBLIC;
REVOKE ALL ON observer.identity_resolution_work FROM PUBLIC;
REVOKE ALL ON observer.identity_authority_denials FROM PUBLIC;

REVOKE ALL ON observer.participants FROM gbos_observer_app;
REVOKE ALL ON observer.participant_identity_resolutions FROM gbos_observer_app;
REVOKE ALL ON observer.identity_resolution_work FROM gbos_observer_app;
REVOKE ALL ON observer.identity_authority_denials FROM gbos_observer_app;

GRANT SELECT, INSERT ON observer.participants TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.participant_identity_resolutions
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE ON observer.identity_resolution_work
    TO gbos_observer_app;
GRANT SELECT, INSERT ON observer.identity_authority_denials
    TO gbos_observer_app;
