CREATE TABLE IF NOT EXISTS observer.identity_authority_denials (
    site_id text NOT NULL
        CHECK (
            char_length(site_id) BETWEEN 1 AND 140
            AND site_id ~ '^[A-Za-z0-9][A-Za-z0-9.-]*$'
        ),
    notice_id text NOT NULL
        CHECK (notice_id ~ '^IAD-[0-9a-f]{64}$'),
    identity_provider text NOT NULL
        CHECK (
            identity_provider IN (
                'email', 'wecom', 'whatsapp', 'phone', 'manual_import'
            )
        ),
    identity_ref text NOT NULL
        CHECK (
            char_length(identity_ref) BETWEEN 1 AND 160
            AND identity_ref ~ (
                '^extid:v1:' || identity_provider
                || ':[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$'
            )
            AND identity_ref !~ (
                '^extid:v1:' || identity_provider
                || ':[0-9][0-9 ()-]{7,}[0-9]$'
            )
        ),
    mapping_ref text NOT NULL
        CHECK (mapping_ref ~ '^EID-[0-9A-HJKMNP-TV-Z]{26}$'),
    team_ref text NOT NULL
        CHECK (
            char_length(team_ref) BETWEEN 1 AND 256
            AND team_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
        ),
    deny_through_revision integer NOT NULL
        CHECK (deny_through_revision BETWEEN 1 AND 2147483647),
    reason text NOT NULL
        CHECK (reason IN ('revoked', 'superseded', 'target_ineligible')),
    denied_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, notice_id)
);

CREATE INDEX IF NOT EXISTS identity_authority_denials_lookup_idx
    ON observer.identity_authority_denials (
        site_id, identity_provider, identity_ref, team_ref,
        mapping_ref, deny_through_revision DESC
    );

CREATE OR REPLACE FUNCTION observer.reject_identity_authority_denial_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    RAISE EXCEPTION 'identity authority denial history is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

REVOKE ALL ON FUNCTION observer.reject_identity_authority_denial_mutation()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS identity_authority_denials_immutable
    ON observer.identity_authority_denials;
CREATE TRIGGER identity_authority_denials_immutable
    BEFORE UPDATE OR DELETE ON observer.identity_authority_denials
    FOR EACH ROW
    EXECUTE FUNCTION observer.reject_identity_authority_denial_mutation();

ALTER TABLE observer.identity_authority_denials ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.identity_authority_denials FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS identity_authority_denials_site_isolation
    ON observer.identity_authority_denials;
CREATE POLICY identity_authority_denials_site_isolation
    ON observer.identity_authority_denials
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.identity_authority_denials FROM PUBLIC;
REVOKE ALL ON observer.identity_authority_denials FROM gbos_observer_app;
GRANT SELECT, INSERT ON observer.identity_authority_denials TO gbos_observer_app;
