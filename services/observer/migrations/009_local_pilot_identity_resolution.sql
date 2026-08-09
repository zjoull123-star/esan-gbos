ALTER TABLE observer.connector_instances
    ADD COLUMN IF NOT EXISTS account_user_ref text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'connector_instances_account_user_ref_safe_ck'
          AND conrelid = 'observer.connector_instances'::regclass
    ) THEN
        ALTER TABLE observer.connector_instances
            ADD CONSTRAINT connector_instances_account_user_ref_safe_ck
            CHECK (
                account_user_ref IS NULL
                OR (
                    char_length(account_user_ref) BETWEEN 1 AND 256
                    AND account_user_ref = btrim(account_user_ref)
                    AND account_user_ref !~ '[[:cntrl:]]'
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.connector_instances
    VALIDATE CONSTRAINT connector_instances_account_user_ref_safe_ck;

CREATE TABLE IF NOT EXISTS observer.participant_identity_resolutions (
    site_id text NOT NULL
        CHECK (
            char_length(site_id) BETWEEN 1 AND 140
            AND site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]*$'
        ),
    identity_provider text NOT NULL
        CHECK (
            identity_provider IN (
                'email', 'wecom', 'whatsapp', 'phone', 'manual_import'
            )
        ),
    external_subject_ref text NOT NULL
        CHECK (
            char_length(external_subject_ref) BETWEEN 1 AND 160
            AND external_subject_ref ~ (
                '^extid:v1:' || identity_provider
                || ':[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$'
            )
        ),
    mapping_ref text NOT NULL
        CHECK (mapping_ref ~ '^EID-[0-9A-HJKMNP-TV-Z]{26}$'),
    mapping_revision integer NOT NULL
        CHECK (mapping_revision BETWEEN 1 AND 2147483647),
    team_ref text NOT NULL
        CHECK (
            char_length(team_ref) BETWEEN 1 AND 256
            AND team_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
        ),
    target_type text NOT NULL CHECK (target_type IN ('User', 'Party')),
    target_ref text NOT NULL
        CHECK (
            char_length(target_ref) BETWEEN 1 AND 256
            AND target_ref = btrim(target_ref)
            AND target_ref !~ '[[:cntrl:]]'
        ),
    status text NOT NULL CHECK (status IN ('confirmed', 'revoked')),
    resolved_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (
        site_id, identity_provider, external_subject_ref, mapping_revision
    ),
    CHECK (recorded_at >= resolved_at)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'participant_identity_resolutions_opaque_subject_ck'
          AND conrelid = 'observer.participant_identity_resolutions'::regclass
    ) THEN
        ALTER TABLE observer.participant_identity_resolutions
            ADD CONSTRAINT participant_identity_resolutions_opaque_subject_ck
            CHECK (
                external_subject_ref !~ (
                    '^extid:v1:' || identity_provider
                    || ':[0-9][0-9 ()-]{7,}[0-9]$'
                )
            ) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.participant_identity_resolutions
    VALIDATE CONSTRAINT participant_identity_resolutions_opaque_subject_ck;

CREATE INDEX IF NOT EXISTS participant_identity_resolutions_latest_idx
    ON observer.participant_identity_resolutions (
        site_id, identity_provider, external_subject_ref,
        mapping_revision DESC
    );

CREATE OR REPLACE FUNCTION observer.enforce_identity_resolution_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    existing observer.participant_identity_resolutions%ROWTYPE;
    latest observer.participant_identity_resolutions%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.site_id || chr(31) || NEW.identity_provider
            || chr(31) || NEW.external_subject_ref,
            0
        )
    );

    SELECT resolution.*
    INTO existing
    FROM observer.participant_identity_resolutions AS resolution
    WHERE resolution.site_id = NEW.site_id
      AND resolution.identity_provider = NEW.identity_provider
      AND resolution.external_subject_ref = NEW.external_subject_ref
      AND resolution.mapping_revision = NEW.mapping_revision;

    IF FOUND THEN
        IF existing.mapping_ref IS NOT DISTINCT FROM NEW.mapping_ref
           AND existing.team_ref IS NOT DISTINCT FROM NEW.team_ref
           AND existing.target_type IS NOT DISTINCT FROM NEW.target_type
           AND existing.target_ref IS NOT DISTINCT FROM NEW.target_ref
           AND existing.status IS NOT DISTINCT FROM NEW.status
           AND existing.resolved_at IS NOT DISTINCT FROM NEW.resolved_at THEN
            RETURN NULL;
        END IF;
        RAISE EXCEPTION 'identity resolution revision conflict'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT resolution.*
    INTO latest
    FROM observer.participant_identity_resolutions AS resolution
    WHERE resolution.site_id = NEW.site_id
      AND resolution.identity_provider = NEW.identity_provider
      AND resolution.external_subject_ref = NEW.external_subject_ref
    ORDER BY resolution.mapping_revision DESC
    LIMIT 1;

    IF FOUND THEN
        IF NEW.mapping_revision < latest.mapping_revision THEN
            RAISE EXCEPTION 'stale identity resolution revision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.mapping_ref IS DISTINCT FROM latest.mapping_ref
           OR NEW.team_ref IS DISTINCT FROM latest.team_ref
           OR NEW.target_type IS DISTINCT FROM latest.target_type
           OR NEW.target_ref IS DISTINCT FROM latest.target_ref THEN
            RAISE EXCEPTION 'identity resolution mapping conflict'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.resolved_at < latest.resolved_at
           OR NEW.recorded_at < latest.recorded_at THEN
            RAISE EXCEPTION 'stale identity resolution timestamp'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF latest.status = 'revoked' AND NEW.status = 'confirmed' THEN
            RAISE EXCEPTION 'identity resolution transition rejected'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;

    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS participant_identity_resolutions_insert_fence
    ON observer.participant_identity_resolutions;
CREATE TRIGGER participant_identity_resolutions_insert_fence
    BEFORE INSERT ON observer.participant_identity_resolutions
    FOR EACH ROW EXECUTE FUNCTION observer.enforce_identity_resolution_insert();

CREATE OR REPLACE FUNCTION observer.reject_identity_resolution_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW IS NOT DISTINCT FROM OLD THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'identity resolution history is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$$;

DROP TRIGGER IF EXISTS participant_identity_resolutions_immutable
    ON observer.participant_identity_resolutions;
CREATE TRIGGER participant_identity_resolutions_immutable
    BEFORE UPDATE ON observer.participant_identity_resolutions
    FOR EACH ROW EXECUTE FUNCTION observer.reject_identity_resolution_update();

REVOKE ALL ON FUNCTION observer.enforce_identity_resolution_insert() FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.reject_identity_resolution_update() FROM PUBLIC;

ALTER TABLE observer.participant_identity_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.participant_identity_resolutions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS participant_identity_resolutions_site_isolation
    ON observer.participant_identity_resolutions;
CREATE POLICY participant_identity_resolutions_site_isolation
    ON observer.participant_identity_resolutions
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.connector_instances ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.connector_instances FORCE ROW LEVEL SECURITY;

REVOKE ALL ON observer.connector_instances FROM PUBLIC;
REVOKE ALL ON observer.participant_identity_resolutions FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE
    ON observer.connector_instances
    TO gbos_observer_app;
GRANT SELECT, INSERT, UPDATE
    ON observer.participant_identity_resolutions
    TO gbos_observer_app;
