-- Durable provider-neutral terminal authority for draft and final-MIME material.
-- This schema contains only opaque references, digests, state, and timestamps.

CREATE TABLE IF NOT EXISTS email_gateway.email_material_terminal_authorities (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    authority_receipt_ref text NOT NULL
        CHECK (authority_receipt_ref ~ '^ETA-[0-9A-HJKMNP-TV-Z]{26}$'),
    source_authority_receipt_ref text NOT NULL
        CHECK (source_authority_receipt_ref ~ '^[A-Z]{3}-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_ref text NOT NULL CHECK (draft_ref ~ '^DRF-[0-9A-HJKMNP-TV-Z]{26}$'),
    draft_revision bigint NOT NULL CHECK (draft_revision BETWEEN 1 AND 2147483647),
    source_draft_evidence_ref text NOT NULL
        CHECK (source_draft_evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    source_draft_digest char(71) NOT NULL
        CHECK (source_draft_digest ~ '^sha256:[a-f0-9]{64}$'),
    material_kind text NOT NULL CHECK (material_kind IN ('draft', 'final_mime')),
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    evidence_digest char(71) NOT NULL CHECK (evidence_digest ~ '^sha256:[a-f0-9]{64}$'),
    terminal_state text NOT NULL CHECK (terminal_state IN ('sent', 'discarded')),
    terminal_at timestamptz NOT NULL,
    not_before timestamptz NOT NULL,
    payload_digest char(71) NOT NULL CHECK (payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    registration_request_ref text NOT NULL
        CHECK (registration_request_ref ~ '^ETR-[0-9A-HJKMNP-TV-Z]{26}$'),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, authority_receipt_ref),
    UNIQUE (site_id, purpose, evidence_ref),
    UNIQUE (site_id, purpose, registration_request_ref),
    CHECK (not_before = terminal_at + interval '30 days'),
    CHECK (terminal_state <> 'discarded' OR material_kind = 'draft'),
    CHECK (
        material_kind <> 'draft'
        OR (
            evidence_ref = source_draft_evidence_ref
            AND evidence_digest = source_draft_digest
        )
    )
);

CREATE TABLE IF NOT EXISTS email_gateway.email_material_terminal_authority_state (
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    authority_receipt_ref text NOT NULL,
    registration_request_ref text NOT NULL,
    registration_status text NOT NULL DEFAULT 'pending'
        CHECK (registration_status IN ('pending', 'leased', 'retry', 'registered', 'dead_letter')),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt BETWEEN 0 AND 5),
    lease_owner text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_expires_at timestamptz,
    next_attempt_at timestamptz NOT NULL,
    safe_error_code text,
    observer_request_ref text,
    registered_at timestamptz,
    tombstone_status text NOT NULL DEFAULT 'pending'
        CHECK (tombstone_status IN ('pending', 'completed')),
    tombstone_receipt_ref text,
    deleted_at timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, authority_receipt_ref),
    UNIQUE (site_id, purpose, registration_request_ref),
    FOREIGN KEY (site_id, purpose, authority_receipt_ref)
        REFERENCES email_gateway.email_material_terminal_authorities
            (site_id, purpose, authority_receipt_ref),
    FOREIGN KEY (site_id, purpose, registration_request_ref)
        REFERENCES email_gateway.email_material_terminal_authorities
            (site_id, purpose, registration_request_ref),
    CHECK (
        (registration_status = 'leased' AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL AND attempt >= 1 AND lease_generation >= 1)
        OR
        (registration_status <> 'leased' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        (registration_status = 'registered' AND observer_request_ref IS NOT NULL
            AND registered_at IS NOT NULL)
        OR
        (registration_status <> 'registered' AND observer_request_ref IS NULL
            AND registered_at IS NULL)
    ),
    CHECK (
        (tombstone_status = 'pending' AND tombstone_receipt_ref IS NULL AND deleted_at IS NULL)
        OR
        (tombstone_status = 'completed' AND tombstone_receipt_ref IS NOT NULL
            AND deleted_at IS NOT NULL AND registration_status = 'registered')
    )
);

CREATE INDEX IF NOT EXISTS email_material_terminal_authority_claim_idx
    ON email_gateway.email_material_terminal_authority_state
        (site_id, purpose, registration_status, next_attempt_at, lease_expires_at)
    WHERE registration_status IN ('pending', 'retry', 'leased');

CREATE TABLE IF NOT EXISTS email_gateway.email_material_tombstone_callbacks (
    callback_receipt_ref text NOT NULL
        CHECK (callback_receipt_ref ~ '^GTC-[0-9A-HJKMNP-TV-Z]{26}$'),
    site_id text NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'email_draft_material'),
    authority_receipt_ref text NOT NULL,
    evidence_ref text NOT NULL CHECK (evidence_ref ~ '^EVR-[0-9A-HJKMNP-TV-Z]{26}$'),
    observer_request_ref text NOT NULL
        CHECK (observer_request_ref ~ '^EMR-[0-9A-HJKMNP-TV-Z]{26}$'),
    tombstone_receipt_ref text NOT NULL
        CHECK (tombstone_receipt_ref ~ '^TMB-[0-9A-HJKMNP-TV-Z]{26}$'),
    deleted_at timestamptz NOT NULL,
    evidence_digest char(71) NOT NULL CHECK (evidence_digest ~ '^sha256:[a-f0-9]{64}$'),
    callback_payload_digest char(71) NOT NULL
        CHECK (callback_payload_digest ~ '^sha256:[a-f0-9]{64}$'),
    received_at timestamptz NOT NULL,
    PRIMARY KEY (site_id, purpose, callback_receipt_ref),
    UNIQUE (site_id, purpose, authority_receipt_ref),
    UNIQUE (site_id, purpose, tombstone_receipt_ref),
    FOREIGN KEY (site_id, purpose, authority_receipt_ref)
        REFERENCES email_gateway.email_material_terminal_authorities
            (site_id, purpose, authority_receipt_ref)
);

DROP TRIGGER IF EXISTS email_material_terminal_authorities_immutable
    ON email_gateway.email_material_terminal_authorities;
CREATE TRIGGER email_material_terminal_authorities_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.email_material_terminal_authorities
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

DROP TRIGGER IF EXISTS email_material_tombstone_callbacks_immutable
    ON email_gateway.email_material_tombstone_callbacks;
CREATE TRIGGER email_material_tombstone_callbacks_immutable
    BEFORE UPDATE OR DELETE ON email_gateway.email_material_tombstone_callbacks
    FOR EACH ROW EXECUTE FUNCTION email_gateway.reject_immutable_change();

CREATE OR REPLACE FUNCTION email_gateway.email_material_authority_payload_digest(
    p_site_id text,
    p_authority_receipt_ref text,
    p_source_authority_receipt_ref text,
    p_draft_ref text,
    p_draft_revision bigint,
    p_material_kind text,
    p_evidence_ref text,
    p_evidence_digest text,
    p_terminal_state text,
    p_terminal_at timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public
AS $function$
    SELECT 'sha256:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        p_site_id || chr(31) || 'email_draft_material' || chr(31) ||
        p_authority_receipt_ref || chr(31) || p_source_authority_receipt_ref || chr(31) ||
        p_draft_ref || chr(31) || p_draft_revision::text || chr(31) ||
        p_material_kind || chr(31) || p_evidence_ref || chr(31) ||
        p_evidence_digest || chr(31) || p_terminal_state || chr(31) ||
        pg_catalog.to_char(
            p_terminal_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        'UTF8'
    )), 'hex')
$function$;

CREATE OR REPLACE FUNCTION email_gateway.email_material_callback_payload_digest(
    p_site_id text,
    p_authority_receipt_ref text,
    p_evidence_ref text,
    p_observer_request_ref text,
    p_tombstone_receipt_ref text,
    p_deleted_at timestamptz,
    p_evidence_digest text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public
AS $function$
    SELECT 'sha256:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        p_site_id || chr(31) || 'email_draft_material' || chr(31) ||
        p_authority_receipt_ref || chr(31) || p_evidence_ref || chr(31) ||
        p_observer_request_ref || chr(31) || p_tombstone_receipt_ref || chr(31) ||
        pg_catalog.to_char(
            p_deleted_at AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ) || chr(31) || p_evidence_digest,
        'UTF8'
    )), 'hex')
$function$;

CREATE OR REPLACE FUNCTION email_gateway.create_sent_email_material_authorities(
    p_site_id text,
    p_provider_receipt_record_ref text
)
RETURNS TABLE (
    authority_receipt_ref text, site_id text, purpose text,
    draft_ref text, draft_revision bigint, material_kind text,
    evidence_ref text, evidence_digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz,
    source_authority_receipt_ref text, payload_digest text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway, public
AS $function$
DECLARE
    v_target record;
    v_source record;
    v_material record;
    v_existing email_gateway.email_material_terminal_authorities%ROWTYPE;
    v_authority_ref text;
    v_registration_ref text;
    v_payload_digest text;
    v_updated text;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true) THEN
        RAISE EXCEPTION 'sent email material authority scope rejected'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT receipt.send_outbox_ref INTO v_target
      FROM email_gateway.provider_receipts AS receipt
     WHERE receipt.site_id = p_site_id
       AND receipt.provider_receipt_record_ref = p_provider_receipt_record_ref
       AND receipt.outcome IN ('accepted', 'delivered');
    IF NOT FOUND THEN
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_site_id || chr(31) || v_target.send_outbox_ref, 0)
    );
    SELECT receipt.provider_receipt_record_ref,
           receipt.observed_at,
           outbox.draft_ref,
           (outbox.approved_envelope->>'reply_draft_revision')::bigint AS draft_revision,
           draft.content_evidence_ref AS draft_evidence_ref,
           draft.content_digest AS draft_digest,
           outbox.final_mime_evidence_ref,
           outbox.final_mime_digest
      INTO v_source
      FROM email_gateway.provider_receipts AS receipt
      JOIN email_gateway.send_outbox AS outbox
        ON outbox.site_id = receipt.site_id
       AND outbox.send_ref = receipt.send_outbox_ref
      JOIN email_gateway.send_outbox_state AS outbox_state
        ON outbox_state.site_id = outbox.site_id
       AND outbox_state.send_outbox_ref = outbox.send_ref
      JOIN email_gateway.command_inbox AS command
        ON command.site_id = outbox.site_id
       AND command.command_receipt_ref = outbox.command_receipt_ref
      JOIN email_gateway.reply_drafts AS draft
        ON draft.site_id = outbox.site_id
       AND draft.draft_ref = outbox.draft_ref
     WHERE receipt.site_id = p_site_id
       AND receipt.send_outbox_ref = v_target.send_outbox_ref
       AND receipt.outcome IN ('accepted', 'delivered')
       AND outbox_state.state IN ('provider_accepted', 'delivered')
       AND outbox.approved_envelope = command.approved_envelope
       AND outbox.approved_payload_digest = 'sha256:' || command.payload_digest
       AND outbox.draft_ref = outbox.approved_envelope->>'reply_draft_ref'
       AND draft.revision = (outbox.approved_envelope->>'reply_draft_revision')::bigint
       AND draft.content_digest = outbox.approved_envelope->>'reply_draft_digest'
       AND outbox.final_mime_evidence_ref =
           outbox.approved_envelope->>'final_mime_evidence_ref'
       AND outbox.final_mime_digest = outbox.approved_envelope->>'final_mime_digest'
       AND draft.content_evidence_ref <> outbox.final_mime_evidence_ref
       AND pg_catalog.jsonb_typeof(outbox.approved_envelope->'evidence_refs') = 'array'
       AND outbox.approved_envelope->'evidence_refs'
           @> pg_catalog.jsonb_build_array(draft.content_evidence_ref)
       AND outbox.approved_envelope->'evidence_refs'
           @> pg_catalog.jsonb_build_array(outbox.final_mime_evidence_ref)
       AND draft.state IN ('editable', 'terminal')
     ORDER BY receipt.observed_at, receipt.provider_receipt_record_ref
     LIMIT 1
     FOR UPDATE OF draft;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'sent email material authority pins rejected'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    FOR v_material IN
        SELECT * FROM (
            VALUES
                ('draft'::text, v_source.draft_evidence_ref, v_source.draft_digest),
                ('final_mime'::text, v_source.final_mime_evidence_ref,
                    v_source.final_mime_digest)
        ) AS material(material_kind, evidence_ref, evidence_digest)
    LOOP
        v_authority_ref := 'ETA-' || upper(substr(md5(
            p_site_id || chr(31) || v_source.provider_receipt_record_ref || chr(31) ||
            v_material.material_kind || chr(31) || v_material.evidence_ref
        ), 1, 26));
        v_registration_ref := 'ETR-' || upper(substr(md5(
            v_authority_ref || chr(31) || 'observer-registration'
        ), 1, 26));
        v_payload_digest := email_gateway.email_material_authority_payload_digest(
            p_site_id, v_authority_ref, v_source.provider_receipt_record_ref,
            v_source.draft_ref, v_source.draft_revision, v_material.material_kind,
            v_material.evidence_ref, v_material.evidence_digest, 'sent',
            v_source.observed_at
        );
        SELECT authority.* INTO v_existing
          FROM email_gateway.email_material_terminal_authorities AS authority
         WHERE authority.site_id = p_site_id
           AND authority.purpose = 'email_draft_material'
           AND authority.evidence_ref = v_material.evidence_ref;
        IF FOUND THEN
            IF v_existing.authority_receipt_ref <> v_authority_ref
               OR v_existing.source_authority_receipt_ref <>
                    v_source.provider_receipt_record_ref
               OR v_existing.draft_ref <> v_source.draft_ref
               OR v_existing.draft_revision <> v_source.draft_revision
               OR v_existing.source_draft_evidence_ref <> v_source.draft_evidence_ref
               OR v_existing.source_draft_digest <> v_source.draft_digest
               OR v_existing.material_kind <> v_material.material_kind
               OR v_existing.evidence_digest <> v_material.evidence_digest
               OR v_existing.terminal_state <> 'sent'
               OR v_existing.terminal_at <> v_source.observed_at
               OR v_existing.not_before <> v_source.observed_at + interval '30 days'
               OR v_existing.payload_digest <> v_payload_digest
               OR v_existing.registration_request_ref <> v_registration_ref THEN
                RAISE EXCEPTION 'sent email material authority replay drift'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            INSERT INTO email_gateway.email_material_terminal_authorities (
                site_id, purpose, authority_receipt_ref,
                source_authority_receipt_ref, draft_ref, draft_revision,
                source_draft_evidence_ref, source_draft_digest, material_kind,
                evidence_ref, evidence_digest, terminal_state, terminal_at,
                not_before, payload_digest, registration_request_ref, created_at
            ) VALUES (
                p_site_id, 'email_draft_material', v_authority_ref,
                v_source.provider_receipt_record_ref, v_source.draft_ref,
                v_source.draft_revision, v_source.draft_evidence_ref,
                v_source.draft_digest, v_material.material_kind,
                v_material.evidence_ref, v_material.evidence_digest, 'sent',
                v_source.observed_at, v_source.observed_at + interval '30 days',
                v_payload_digest, v_registration_ref, statement_timestamp()
            );
            INSERT INTO email_gateway.email_material_terminal_authority_state (
                site_id, purpose, authority_receipt_ref, registration_request_ref,
                registration_status, next_attempt_at, tombstone_status, updated_at
            ) VALUES (
                p_site_id, 'email_draft_material', v_authority_ref,
                v_registration_ref, 'pending', statement_timestamp(), 'pending',
                statement_timestamp()
            );
        END IF;
    END LOOP;

    UPDATE email_gateway.reply_drafts AS draft
       SET state = 'terminal',
           terminal_at = v_source.observed_at,
           content_expires_at = v_source.observed_at + interval '30 days',
           updated_at = GREATEST(draft.updated_at, v_source.observed_at)
     WHERE draft.site_id = p_site_id
       AND draft.draft_ref = v_source.draft_ref
       AND draft.revision = v_source.draft_revision
       AND draft.content_evidence_ref = v_source.draft_evidence_ref
       AND draft.content_digest = v_source.draft_digest
       AND draft.state = 'editable'
    RETURNING draft.draft_ref INTO v_updated;
    IF v_updated IS NULL AND NOT EXISTS (
        SELECT 1 FROM email_gateway.reply_drafts AS draft
         WHERE draft.site_id = p_site_id
           AND draft.draft_ref = v_source.draft_ref
           AND draft.revision = v_source.draft_revision
           AND draft.content_evidence_ref = v_source.draft_evidence_ref
           AND draft.content_digest = v_source.draft_digest
           AND draft.state = 'terminal'
           AND draft.terminal_at = v_source.observed_at
           AND draft.content_expires_at = v_source.observed_at + interval '30 days'
    ) THEN
        RAISE EXCEPTION 'sent email material draft transition conflict'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN QUERY
    SELECT authority.authority_receipt_ref, authority.site_id, authority.purpose,
           authority.draft_ref, authority.draft_revision, authority.material_kind,
           authority.evidence_ref, authority.evidence_digest::text,
           authority.terminal_state, authority.terminal_at, authority.not_before,
           authority.source_authority_receipt_ref, authority.payload_digest::text
      FROM email_gateway.email_material_terminal_authorities AS authority
     WHERE authority.site_id = p_site_id
       AND authority.purpose = 'email_draft_material'
       AND authority.source_authority_receipt_ref = v_source.provider_receipt_record_ref
     ORDER BY CASE authority.material_kind WHEN 'draft' THEN 0 ELSE 1 END;
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.create_discarded_email_material_authority(
    p_site_id text,
    p_human_authority_receipt_ref text,
    p_draft_ref text,
    p_draft_revision bigint,
    p_evidence_ref text,
    p_evidence_digest text,
    p_terminal_at timestamptz,
    p_receipt_payload_digest text
)
RETURNS TABLE (
    authority_receipt_ref text, site_id text, purpose text,
    draft_ref text, draft_revision bigint, material_kind text,
    evidence_ref text, evidence_digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz,
    source_authority_receipt_ref text, payload_digest text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
DECLARE
    v_draft email_gateway.reply_drafts%ROWTYPE;
    v_existing email_gateway.email_material_terminal_authorities%ROWTYPE;
    v_authority_ref text;
    v_registration_ref text;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_receipt_payload_digest !~ '^sha256:[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'discard email material authority rejected'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_site_id || chr(31) || p_draft_ref, 0)
    );
    SELECT draft.* INTO v_draft
      FROM email_gateway.reply_drafts AS draft
     WHERE draft.site_id = p_site_id
       AND draft.draft_ref = p_draft_ref
       AND draft.revision = p_draft_revision
       AND draft.content_evidence_ref = p_evidence_ref
       AND draft.content_digest = p_evidence_digest
       AND draft.state IN ('editable', 'discarded')
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'discard email material authority pins rejected'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_authority_ref := 'ETA-' || upper(substr(md5(
        p_site_id || chr(31) || p_human_authority_receipt_ref || chr(31) ||
        p_draft_ref || chr(31) || p_draft_revision::text
    ), 1, 26));
    v_registration_ref := 'ETR-' || upper(substr(md5(
        v_authority_ref || chr(31) || 'observer-registration'
    ), 1, 26));
    SELECT authority.* INTO v_existing
      FROM email_gateway.email_material_terminal_authorities AS authority
     WHERE authority.site_id = p_site_id
       AND authority.purpose = 'email_draft_material'
       AND authority.evidence_ref = p_evidence_ref;
    IF FOUND THEN
        IF v_existing.authority_receipt_ref <> v_authority_ref
           OR v_existing.source_authority_receipt_ref <> p_human_authority_receipt_ref
           OR v_existing.draft_ref <> p_draft_ref
           OR v_existing.draft_revision <> p_draft_revision
           OR v_existing.material_kind <> 'draft'
           OR v_existing.evidence_digest <> p_evidence_digest
           OR v_existing.terminal_state <> 'discarded'
           OR v_existing.terminal_at <> p_terminal_at
           OR v_existing.payload_digest <> p_receipt_payload_digest
           OR v_existing.registration_request_ref <> v_registration_ref THEN
            RAISE EXCEPTION 'discard email material authority replay drift'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        INSERT INTO email_gateway.email_material_terminal_authorities (
            site_id, purpose, authority_receipt_ref,
            source_authority_receipt_ref, draft_ref, draft_revision,
            source_draft_evidence_ref, source_draft_digest, material_kind,
            evidence_ref, evidence_digest, terminal_state, terminal_at,
            not_before, payload_digest, registration_request_ref, created_at
        ) VALUES (
            p_site_id, 'email_draft_material', v_authority_ref,
            p_human_authority_receipt_ref, p_draft_ref, p_draft_revision,
            p_evidence_ref, p_evidence_digest, 'draft', p_evidence_ref,
            p_evidence_digest, 'discarded', p_terminal_at,
            p_terminal_at + interval '30 days', p_receipt_payload_digest,
            v_registration_ref, statement_timestamp()
        );
        INSERT INTO email_gateway.email_material_terminal_authority_state (
            site_id, purpose, authority_receipt_ref, registration_request_ref,
            registration_status, next_attempt_at, tombstone_status, updated_at
        ) VALUES (
            p_site_id, 'email_draft_material', v_authority_ref,
            v_registration_ref, 'pending', statement_timestamp(), 'pending',
            statement_timestamp()
        );
    END IF;
    IF v_draft.state = 'editable' THEN
        UPDATE email_gateway.reply_drafts AS draft
           SET state = 'discarded', terminal_at = p_terminal_at,
               content_expires_at = p_terminal_at + interval '30 days',
               updated_at = GREATEST(draft.updated_at, p_terminal_at)
         WHERE draft.site_id = p_site_id
           AND draft.draft_ref = p_draft_ref
           AND draft.revision = p_draft_revision
           AND draft.content_evidence_ref = p_evidence_ref
           AND draft.content_digest = p_evidence_digest
           AND draft.state = 'editable';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'discard email material draft transition conflict'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF v_draft.terminal_at <> p_terminal_at
       OR v_draft.content_expires_at <> p_terminal_at + interval '30 days' THEN
        RAISE EXCEPTION 'discard email material draft replay drift'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN QUERY
    SELECT authority.authority_receipt_ref, authority.site_id, authority.purpose,
           authority.draft_ref, authority.draft_revision, authority.material_kind,
           authority.evidence_ref, authority.evidence_digest::text,
           authority.terminal_state, authority.terminal_at, authority.not_before,
           authority.source_authority_receipt_ref, authority.payload_digest::text
      FROM email_gateway.email_material_terminal_authorities AS authority
     WHERE authority.site_id = p_site_id
       AND authority.purpose = 'email_draft_material'
       AND authority.authority_receipt_ref = v_authority_ref;
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.resolve_email_material_terminal_authority(
    p_site_id text,
    p_authority_receipt_ref text
)
RETURNS TABLE (
    authority_receipt_ref text, site_id text, purpose text,
    draft_ref text, draft_revision bigint, material_kind text,
    evidence_ref text, evidence_digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz,
    source_authority_receipt_ref text, payload_digest text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
    SELECT authority.authority_receipt_ref, authority.site_id, authority.purpose,
           authority.draft_ref, authority.draft_revision, authority.material_kind,
           authority.evidence_ref, authority.evidence_digest::text,
           authority.terminal_state, authority.terminal_at, authority.not_before,
           authority.source_authority_receipt_ref, authority.payload_digest::text
      FROM email_gateway.email_material_terminal_authorities AS authority
      JOIN email_gateway.reply_drafts AS draft
        ON draft.site_id = authority.site_id
       AND draft.draft_ref = authority.draft_ref
       AND draft.revision = authority.draft_revision
       AND draft.content_evidence_ref = authority.source_draft_evidence_ref
       AND draft.content_digest = authority.source_draft_digest
       AND draft.state = CASE authority.terminal_state
           WHEN 'sent' THEN 'terminal' ELSE 'discarded' END
       AND draft.terminal_at = authority.terminal_at
       AND draft.content_expires_at = authority.not_before
     WHERE p_site_id = current_setting('gbos.site_id', true)
       AND authority.site_id = p_site_id
       AND authority.purpose = 'email_draft_material'
       AND authority.authority_receipt_ref = p_authority_receipt_ref
$function$;

CREATE OR REPLACE FUNCTION email_gateway.claim_email_material_authority_registration(
    p_site_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_until timestamptz
)
RETURNS TABLE (
    authority_receipt_ref text, site_id text, purpose text,
    draft_ref text, draft_revision bigint, material_kind text,
    evidence_ref text, evidence_digest text, terminal_state text,
    terminal_at timestamptz, not_before timestamptz,
    source_authority_receipt_ref text, payload_digest text,
    registration_request_ref text, worker_id text, attempt integer,
    lease_generation bigint, lease_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
       OR p_worker_id LIKE '%@%'
       OR p_lease_until <= p_now OR p_lease_until > p_now + interval '5 minutes' THEN
        RAISE EXCEPTION 'email material registration claim rejected';
    END IF;
    RETURN QUERY
    WITH candidate AS (
        SELECT state.site_id, state.purpose, state.authority_receipt_ref
          FROM email_gateway.email_material_terminal_authority_state AS state
         WHERE state.site_id = p_site_id
           AND state.attempt < 5
           AND state.next_attempt_at <= p_now
           AND (
               state.registration_status IN ('pending', 'retry')
               OR (
                   state.registration_status = 'leased'
                   AND state.lease_expires_at <= p_now
               )
           )
         ORDER BY state.next_attempt_at, state.authority_receipt_ref
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    ), leased AS (
        UPDATE email_gateway.email_material_terminal_authority_state AS state
           SET registration_status = 'leased', attempt = state.attempt + 1,
               lease_owner = p_worker_id, lease_generation = state.lease_generation + 1,
               lease_expires_at = p_lease_until, safe_error_code = NULL,
               updated_at = p_now
          FROM candidate
         WHERE state.site_id = candidate.site_id
           AND state.purpose = candidate.purpose
           AND state.authority_receipt_ref = candidate.authority_receipt_ref
        RETURNING state.*
    )
    SELECT authority.authority_receipt_ref, authority.site_id, authority.purpose,
           authority.draft_ref, authority.draft_revision, authority.material_kind,
           authority.evidence_ref, authority.evidence_digest::text,
           authority.terminal_state, authority.terminal_at, authority.not_before,
           authority.source_authority_receipt_ref, authority.payload_digest::text,
           leased.registration_request_ref, leased.lease_owner,
           leased.attempt, leased.lease_generation, leased.lease_expires_at
      FROM leased
      JOIN email_gateway.email_material_terminal_authorities AS authority
        USING (site_id, purpose, authority_receipt_ref);
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.heartbeat_email_material_authority_registration(
    p_site_id text, p_authority_receipt_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint,
    p_now timestamptz, p_lease_until timestamptz
)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
DECLARE v_expiry timestamptz;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_lease_until <= p_now OR p_lease_until > p_now + interval '5 minutes' THEN
        RAISE EXCEPTION 'email material registration heartbeat rejected';
    END IF;
    UPDATE email_gateway.email_material_terminal_authority_state AS state
       SET lease_expires_at = p_lease_until, updated_at = p_now
     WHERE state.site_id = p_site_id
       AND state.authority_receipt_ref = p_authority_receipt_ref
       AND state.registration_status = 'leased'
       AND state.lease_owner = p_worker_id
       AND state.attempt = p_attempt
       AND state.lease_generation = p_lease_generation
       AND state.lease_expires_at >= p_now
    RETURNING state.lease_expires_at INTO v_expiry;
    IF v_expiry IS NULL THEN
        RAISE EXCEPTION 'email material registration lease fence conflict';
    END IF;
    RETURN v_expiry;
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.ack_email_material_authority_registration(
    p_site_id text, p_authority_receipt_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint, p_observer_request_ref text,
    p_evidence_ref text, p_not_before timestamptz, p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true) THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1
          FROM email_gateway.email_material_terminal_authority_state AS state
          JOIN email_gateway.email_material_terminal_authorities AS authority
            USING (site_id, purpose, authority_receipt_ref)
         WHERE state.site_id = p_site_id
           AND state.authority_receipt_ref = p_authority_receipt_ref
           AND state.registration_status = 'registered'
           AND state.observer_request_ref = p_observer_request_ref
           AND authority.evidence_ref = p_evidence_ref
           AND authority.not_before = p_not_before
    ) THEN
        RETURN true;
    END IF;
    UPDATE email_gateway.email_material_terminal_authority_state AS state
       SET registration_status = 'registered', lease_owner = NULL,
           lease_expires_at = NULL, observer_request_ref = p_observer_request_ref,
           registered_at = p_now, safe_error_code = NULL, updated_at = p_now
      FROM email_gateway.email_material_terminal_authorities AS authority
     WHERE state.site_id = p_site_id
       AND state.authority_receipt_ref = p_authority_receipt_ref
       AND state.registration_status = 'leased'
       AND state.lease_owner = p_worker_id
       AND state.attempt = p_attempt
       AND state.lease_generation = p_lease_generation
       AND state.lease_expires_at >= p_now
       AND authority.site_id = state.site_id
       AND authority.purpose = state.purpose
       AND authority.authority_receipt_ref = state.authority_receipt_ref
       AND authority.evidence_ref = p_evidence_ref
       AND authority.not_before = p_not_before;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.fail_email_material_authority_registration(
    p_site_id text, p_authority_receipt_ref text, p_worker_id text,
    p_attempt integer, p_lease_generation bigint, p_safe_error_code text,
    p_next_attempt_at timestamptz, p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_safe_error_code !~ '^[a-z][a-z0-9_]{0,79}$'
       OR p_next_attempt_at < p_now
       OR p_next_attempt_at > p_now + interval '5 minutes' THEN
        RETURN false;
    END IF;
    UPDATE email_gateway.email_material_terminal_authority_state AS state
       SET registration_status = CASE WHEN state.attempt >= 5
               THEN 'dead_letter' ELSE 'retry' END,
           lease_owner = NULL, lease_expires_at = NULL,
           next_attempt_at = p_next_attempt_at, safe_error_code = p_safe_error_code,
           updated_at = p_now
     WHERE state.site_id = p_site_id
       AND state.authority_receipt_ref = p_authority_receipt_ref
       AND state.registration_status = 'leased'
       AND state.lease_owner = p_worker_id
       AND state.attempt = p_attempt
       AND state.lease_generation = p_lease_generation
       AND state.lease_expires_at >= p_now;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION email_gateway.accept_email_material_tombstone_callback(
    p_site_id text, p_authority_receipt_ref text, p_evidence_ref text,
    p_observer_request_ref text, p_tombstone_receipt_ref text,
    p_deleted_at timestamptz, p_evidence_digest text,
    p_callback_payload_digest text, p_received_at timestamptz
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, email_gateway
AS $function$
DECLARE
    v_authority email_gateway.email_material_terminal_authorities%ROWTYPE;
    v_existing email_gateway.email_material_tombstone_callbacks%ROWTYPE;
    v_callback_ref text;
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('gbos.site_id', true)
       OR p_callback_payload_digest IS DISTINCT FROM
          email_gateway.email_material_callback_payload_digest(
              p_site_id, p_authority_receipt_ref, p_evidence_ref,
              p_observer_request_ref, p_tombstone_receipt_ref, p_deleted_at,
              p_evidence_digest
          ) THEN
        RAISE EXCEPTION 'email material tombstone callback rejected'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_site_id || chr(31) || p_authority_receipt_ref, 0)
    );
    SELECT authority.* INTO v_authority
      FROM email_gateway.email_material_terminal_authorities AS authority
      JOIN email_gateway.email_material_terminal_authority_state AS state
        USING (site_id, purpose, authority_receipt_ref)
     WHERE authority.site_id = p_site_id
       AND authority.purpose = 'email_draft_material'
       AND authority.authority_receipt_ref = p_authority_receipt_ref
       AND authority.evidence_ref = p_evidence_ref
       AND authority.evidence_digest = p_evidence_digest
       AND authority.not_before <= p_deleted_at
       AND state.registration_status = 'registered'
       AND state.observer_request_ref = p_observer_request_ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'email material tombstone callback binding rejected'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_callback_ref := 'GTC-' || upper(substr(md5(
        p_site_id || chr(31) || p_authority_receipt_ref || chr(31) ||
        p_tombstone_receipt_ref
    ), 1, 26));
    SELECT callback.* INTO v_existing
      FROM email_gateway.email_material_tombstone_callbacks AS callback
     WHERE callback.site_id = p_site_id
       AND callback.purpose = 'email_draft_material'
       AND callback.authority_receipt_ref = p_authority_receipt_ref;
    IF FOUND THEN
        IF v_existing.callback_receipt_ref <> v_callback_ref
           OR v_existing.evidence_ref <> p_evidence_ref
           OR v_existing.observer_request_ref <> p_observer_request_ref
           OR v_existing.tombstone_receipt_ref <> p_tombstone_receipt_ref
           OR v_existing.deleted_at <> p_deleted_at
           OR v_existing.evidence_digest <> p_evidence_digest
           OR v_existing.callback_payload_digest <> p_callback_payload_digest THEN
            RAISE EXCEPTION 'email material tombstone callback replay drift'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN v_existing.callback_receipt_ref;
    END IF;
    INSERT INTO email_gateway.email_material_tombstone_callbacks (
        callback_receipt_ref, site_id, purpose, authority_receipt_ref,
        evidence_ref, observer_request_ref, tombstone_receipt_ref, deleted_at,
        evidence_digest, callback_payload_digest, received_at
    ) VALUES (
        v_callback_ref, p_site_id, 'email_draft_material', p_authority_receipt_ref,
        p_evidence_ref, p_observer_request_ref, p_tombstone_receipt_ref,
        p_deleted_at, p_evidence_digest, p_callback_payload_digest, p_received_at
    );
    UPDATE email_gateway.email_material_terminal_authority_state AS state
       SET tombstone_status = 'completed',
           tombstone_receipt_ref = p_tombstone_receipt_ref,
           deleted_at = p_deleted_at, updated_at = p_received_at
     WHERE state.site_id = p_site_id
       AND state.authority_receipt_ref = p_authority_receipt_ref
       AND state.registration_status = 'registered'
       AND state.observer_request_ref = p_observer_request_ref
       AND state.tombstone_status = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'email material tombstone callback state conflict'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF v_authority.material_kind = 'draft' THEN
        UPDATE email_gateway.reply_drafts AS draft
           SET observer_tombstone_receipt_ref = p_tombstone_receipt_ref,
               updated_at = GREATEST(draft.updated_at, p_received_at)
         WHERE draft.site_id = p_site_id
           AND draft.draft_ref = v_authority.draft_ref
           AND draft.revision = v_authority.draft_revision
           AND draft.content_evidence_ref = v_authority.source_draft_evidence_ref
           AND draft.content_digest = v_authority.source_draft_digest
           AND draft.state = CASE v_authority.terminal_state
               WHEN 'sent' THEN 'terminal' ELSE 'discarded' END
           AND draft.terminal_at = v_authority.terminal_at
           AND draft.content_expires_at = v_authority.not_before
           AND draft.observer_tombstone_receipt_ref IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'email material draft tombstone writeback conflict'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF v_authority.material_kind = 'final_mime' THEN
        -- The final-MIME callback remains on its own immutable authority state.
        NULL;
    END IF;
    RETURN v_callback_ref;
END
$function$;

ALTER TABLE email_gateway.email_material_terminal_authorities ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.email_material_terminal_authorities FORCE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.email_material_terminal_authority_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.email_material_terminal_authority_state FORCE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.email_material_tombstone_callbacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_gateway.email_material_tombstone_callbacks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS email_gateway_site_scope
    ON email_gateway.email_material_terminal_authorities;
CREATE POLICY email_gateway_site_scope
    ON email_gateway.email_material_terminal_authorities
    USING (site_id = current_setting('gbos.site_id', true));
DROP POLICY IF EXISTS email_gateway_site_scope
    ON email_gateway.email_material_terminal_authority_state;
CREATE POLICY email_gateway_site_scope
    ON email_gateway.email_material_terminal_authority_state
    USING (site_id = current_setting('gbos.site_id', true))
    WITH CHECK (site_id = current_setting('gbos.site_id', true));
DROP POLICY IF EXISTS email_gateway_site_scope
    ON email_gateway.email_material_tombstone_callbacks;
CREATE POLICY email_gateway_site_scope
    ON email_gateway.email_material_tombstone_callbacks
    USING (site_id = current_setting('gbos.site_id', true));

REVOKE ALL ON email_gateway.email_material_terminal_authorities FROM PUBLIC;
REVOKE ALL ON email_gateway.email_material_terminal_authority_state FROM PUBLIC;
REVOKE ALL ON email_gateway.email_material_tombstone_callbacks FROM PUBLIC;

REVOKE ALL ON FUNCTION email_gateway.email_material_authority_payload_digest(
    text, text, text, text, bigint, text, text, text, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.email_material_callback_payload_digest(
    text, text, text, text, text, timestamptz, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.create_sent_email_material_authorities(text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.create_discarded_email_material_authority(
    text, text, text, bigint, text, text, timestamptz, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.resolve_email_material_terminal_authority(text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.claim_email_material_authority_registration(
    text, text, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.heartbeat_email_material_authority_registration(
    text, text, text, integer, bigint, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.ack_email_material_authority_registration(
    text, text, text, integer, bigint, text, text, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.fail_email_material_authority_registration(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION email_gateway.accept_email_material_tombstone_callback(
    text, text, text, text, text, timestamptz, text, text, timestamptz
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION email_gateway.create_sent_email_material_authorities(text, text)
    TO gbos_email_send_worker;
GRANT EXECUTE ON FUNCTION email_gateway.create_discarded_email_material_authority(
    text, text, text, bigint, text, text, timestamptz, text
) TO gbos_email_gateway_app;
GRANT EXECUTE ON FUNCTION email_gateway.resolve_email_material_terminal_authority(text, text)
    TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.claim_email_material_authority_registration(
    text, text, timestamptz, timestamptz
) TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.heartbeat_email_material_authority_registration(
    text, text, text, integer, bigint, timestamptz, timestamptz
) TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.ack_email_material_authority_registration(
    text, text, text, integer, bigint, text, text, timestamptz, timestamptz
) TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.fail_email_material_authority_registration(
    text, text, text, integer, bigint, text, timestamptz, timestamptz
) TO gbos_email_gateway_retention_worker;
GRANT EXECUTE ON FUNCTION email_gateway.accept_email_material_tombstone_callback(
    text, text, text, text, text, timestamptz, text, text, timestamptz
) TO gbos_email_gateway_retention_worker;
