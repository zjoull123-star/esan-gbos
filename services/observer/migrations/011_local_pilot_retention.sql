CREATE TABLE IF NOT EXISTS observer.retention_runs (
    site_id text PRIMARY KEY,
    run_id text NOT NULL CHECK (char_length(run_id) BETWEEN 1 AND 256),
    worker_id text NOT NULL CHECK (char_length(worker_id) BETWEEN 1 AND 256),
    status text NOT NULL CHECK (status IN ('running', 'completed')),
    lease_expires_at timestamptz NOT NULL,
    lease_generation bigint NOT NULL CHECK (lease_generation >= 1),
    started_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    scanned_count bigint NOT NULL DEFAULT 0 CHECK (scanned_count >= 0),
    eligible_count bigint NOT NULL DEFAULT 0 CHECK (eligible_count >= 0),
    legal_hold_count bigint NOT NULL DEFAULT 0 CHECK (legal_hold_count >= 0),
    historical_reference_count bigint NOT NULL DEFAULT 0
        CHECK (historical_reference_count >= 0),
    metadata_deleted_count bigint NOT NULL DEFAULT 0
        CHECK (metadata_deleted_count >= 0),
    cas_deleted_count bigint NOT NULL DEFAULT 0 CHECK (cas_deleted_count >= 0),
    vault_deleted_count bigint NOT NULL DEFAULT 0 CHECK (vault_deleted_count >= 0),
    CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS observer.retention_cas_tombstones (
    site_id text NOT NULL,
    object_ref text NOT NULL CHECK (char_length(object_ref) BETWEEN 1 AND 512),
    object_sha256 char(64) NOT NULL
        CHECK (object_sha256 ~ '^[a-f0-9]{64}$'),
    status text NOT NULL CHECK (status IN ('pending', 'leased', 'deleted')),
    run_id text,
    lease_owner text,
    lease_expires_at timestamptz,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    deleted_at timestamptz,
    PRIMARY KEY (site_id, object_ref),
    CHECK (
        (
            status = 'leased'
            AND run_id IS NOT NULL
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND lease_generation >= 1
            AND deleted_at IS NULL
        )
        OR (
            status = 'pending'
            AND run_id IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND deleted_at IS NULL
        )
        OR (
            status = 'deleted'
            AND run_id IS NULL
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND deleted_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS retention_cas_tombstones_claim_idx
    ON observer.retention_cas_tombstones (
        site_id, status, lease_expires_at, created_at, object_ref
    )
    WHERE status IN ('pending', 'leased');

ALTER TABLE observer.inbound_deliveries
    ADD COLUMN IF NOT EXISTS retention_until timestamptz;

UPDATE observer.inbound_deliveries
SET retention_until = received_at + interval '30 days'
WHERE retention_until IS NULL;

UPDATE observer.raw_objects
SET retention_until = created_at + interval '30 days'
WHERE retention_until IS NULL;

UPDATE observer.observation_events
SET retention_until = ingested_at + interval '30 days'
WHERE retention_until IS NULL;

CREATE OR REPLACE FUNCTION observer.enforce_observer_retention_boundary()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    boundary timestamptz;
BEGIN
    IF TG_TABLE_NAME = 'raw_objects' THEN
        boundary := NEW.created_at + interval '30 days';
    ELSE
        boundary := NEW.ingested_at + interval '30 days';
    END IF;
    IF TG_OP = 'INSERT' AND NEW.retention_until IS NULL THEN
        NEW.retention_until := boundary;
    ELSIF TG_OP = 'UPDATE' AND NEW.retention_until IS DISTINCT FROM OLD.retention_until THEN
        RAISE EXCEPTION 'Observer retention boundary is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.retention_until IS DISTINCT FROM boundary THEN
        RAISE EXCEPTION 'Observer retention boundary must be exactly 30 days'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS raw_objects_retention_boundary ON observer.raw_objects;
CREATE TRIGGER raw_objects_retention_boundary
    BEFORE INSERT OR UPDATE OF retention_until, created_at
    ON observer.raw_objects
    FOR EACH ROW
    EXECUTE FUNCTION observer.enforce_observer_retention_boundary();

DROP TRIGGER IF EXISTS observation_events_retention_boundary
    ON observer.observation_events;
CREATE TRIGGER observation_events_retention_boundary
    BEFORE INSERT OR UPDATE OF retention_until, ingested_at
    ON observer.observation_events
    FOR EACH ROW
    EXECUTE FUNCTION observer.enforce_observer_retention_boundary();

ALTER TABLE observer.raw_objects ALTER COLUMN retention_until SET NOT NULL;
ALTER TABLE observer.observation_events ALTER COLUMN retention_until SET NOT NULL;

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'raw_objects_exact_retention_until_ck'
          AND conrelid = 'observer.raw_objects'::regclass
    ) THEN
        ALTER TABLE observer.raw_objects
            ADD CONSTRAINT raw_objects_exact_retention_until_ck
            CHECK (retention_until = created_at + interval '30 days') NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'observation_events_exact_retention_until_ck'
          AND conrelid = 'observer.observation_events'::regclass
    ) THEN
        ALTER TABLE observer.observation_events
            ADD CONSTRAINT observation_events_exact_retention_until_ck
            CHECK (retention_until = ingested_at + interval '30 days') NOT VALID;
    END IF;
END
$block$;

ALTER TABLE observer.raw_objects
    VALIDATE CONSTRAINT raw_objects_exact_retention_until_ck;
ALTER TABLE observer.observation_events
    VALIDATE CONSTRAINT observation_events_exact_retention_until_ck;

CREATE OR REPLACE FUNCTION observer.enforce_inbound_delivery_retention()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.retention_until IS NULL THEN
            NEW.retention_until := NEW.received_at + interval '30 days';
        END IF;
    ELSIF NEW.retention_until IS DISTINCT FROM OLD.retention_until THEN
        RAISE EXCEPTION 'inbound delivery retention boundary is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.retention_until IS DISTINCT FROM NEW.received_at + interval '30 days' THEN
        RAISE EXCEPTION 'inbound delivery retention boundary must be exactly 30 days'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS inbound_deliveries_retention_boundary
    ON observer.inbound_deliveries;
CREATE TRIGGER inbound_deliveries_retention_boundary
    BEFORE INSERT OR UPDATE OF retention_until
    ON observer.inbound_deliveries
    FOR EACH ROW
    EXECUTE FUNCTION observer.enforce_inbound_delivery_retention();

ALTER TABLE observer.inbound_deliveries
    ALTER COLUMN retention_until SET NOT NULL;

DO $block$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'inbound_deliveries_retention_until_ck'
          AND conrelid = 'observer.inbound_deliveries'::regclass
    ) THEN
        ALTER TABLE observer.inbound_deliveries
            ADD CONSTRAINT inbound_deliveries_retention_until_ck
            CHECK (retention_until = received_at + interval '30 days') NOT VALID;
    END IF;
END
$block$;

ALTER TABLE observer.inbound_deliveries
    VALIDATE CONSTRAINT inbound_deliveries_retention_until_ck;

CREATE OR REPLACE FUNCTION observer.reject_tombstoned_cas_reference()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
DECLARE
    candidate_ref text;
BEGIN
    IF TG_TABLE_NAME = 'evidence_refs' THEN
        candidate_ref := NEW.content_object_ref;
    ELSE
        candidate_ref := NEW.object_ref;
    END IF;
    IF candidate_ref IS NULL THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(NEW.site_id || chr(31) || candidate_ref, 0)
    );
    IF EXISTS (
        SELECT 1
        FROM observer.retention_cas_tombstones AS tombstone
        WHERE tombstone.site_id = NEW.site_id
          AND tombstone.object_ref = candidate_ref
    ) THEN
        RAISE EXCEPTION 'CAS reference is permanently retention tombstoned'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS raw_objects_retention_tombstone_guard
    ON observer.raw_objects;
CREATE TRIGGER raw_objects_retention_tombstone_guard
    BEFORE INSERT OR UPDATE OF object_ref
    ON observer.raw_objects
    FOR EACH ROW
    EXECUTE FUNCTION observer.reject_tombstoned_cas_reference();

DROP TRIGGER IF EXISTS evidence_refs_retention_tombstone_guard
    ON observer.evidence_refs;
CREATE TRIGGER evidence_refs_retention_tombstone_guard
    BEFORE INSERT OR UPDATE OF content_object_ref
    ON observer.evidence_refs
    FOR EACH ROW
    EXECUTE FUNCTION observer.reject_tombstoned_cas_reference();

DROP TRIGGER IF EXISTS inbound_deliveries_retention_tombstone_guard
    ON observer.inbound_deliveries;
CREATE TRIGGER inbound_deliveries_retention_tombstone_guard
    BEFORE INSERT OR UPDATE OF object_ref
    ON observer.inbound_deliveries
    FOR EACH ROW
    EXECUTE FUNCTION observer.reject_tombstoned_cas_reference();

CREATE OR REPLACE FUNCTION observer.preview_retention_batch(
    p_site_id text,
    p_now timestamptz,
    p_limit integer
)
RETURNS TABLE (
    scanned_count bigint,
    eligible_count bigint,
    legal_hold_count bigint,
    historical_reference_count bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer, context
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_now IS NULL
       OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'invalid retention preview scope'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN QUERY
    WITH batch AS (
        SELECT event.event_id
        FROM observer.observation_events AS event
        WHERE event.site_id = p_site_id
          AND event.retention_until IS NOT NULL
          AND event.retention_until <= p_now
        ORDER BY event.retention_until, event.event_id
        LIMIT p_limit
    ), classified AS (
        SELECT
            batch.event_id,
            EXISTS (
                SELECT 1
                FROM observer.evidence_refs AS evidence
                JOIN observer.legal_holds AS hold
                  ON hold.site_id = evidence.site_id
                 AND hold.evidence_id = evidence.evidence_id
                WHERE evidence.site_id = p_site_id
                  AND evidence.event_id = batch.event_id
                  AND hold.released_at IS NULL
            ) AS is_held,
            EXISTS (
                SELECT 1
                FROM observer.evidence_refs AS evidence
                JOIN context.evidence_records AS historical
                  ON historical.site_id = evidence.site_id
                 AND historical.observer_evidence_id = evidence.evidence_id
                WHERE evidence.site_id = p_site_id
                  AND evidence.event_id = batch.event_id
            ) AS is_historical
        FROM batch
    )
    SELECT
        count(*)::bigint,
        count(*) FILTER (WHERE NOT is_held AND NOT is_historical)::bigint,
        count(*) FILTER (WHERE is_held)::bigint,
        count(*) FILTER (WHERE NOT is_held AND is_historical)::bigint
    FROM classified;
END
$function$;

CREATE OR REPLACE FUNCTION observer.claim_retention_run(
    p_site_id text,
    p_run_id text,
    p_worker_id text,
    p_now timestamptz,
    p_lease_until timestamptz
)
RETURNS TABLE (run_id text, worker_id text, lease_generation bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_site_id IS DISTINCT FROM current_setting('app.site_id', true)
       OR p_run_id IS NULL OR char_length(p_run_id) NOT BETWEEN 1 AND 256
       OR p_worker_id IS NULL OR char_length(p_worker_id) NOT BETWEEN 1 AND 256
       OR p_now IS NULL OR p_lease_until <= p_now
       OR p_lease_until > p_now + interval '1 hour' THEN
        RAISE EXCEPTION 'invalid retention run claim'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN QUERY
    INSERT INTO observer.retention_runs (
        site_id, run_id, worker_id, status, lease_expires_at,
        lease_generation, started_at, updated_at
    ) VALUES (
        p_site_id, p_run_id, p_worker_id, 'running', p_lease_until,
        1, p_now, p_now
    )
    ON CONFLICT (site_id) DO UPDATE
    SET
        run_id = EXCLUDED.run_id,
        worker_id = EXCLUDED.worker_id,
        status = 'running',
        lease_expires_at = EXCLUDED.lease_expires_at,
        lease_generation = observer.retention_runs.lease_generation + 1,
        started_at = EXCLUDED.started_at,
        updated_at = EXCLUDED.updated_at,
        completed_at = NULL,
        scanned_count = 0,
        eligible_count = 0,
        legal_hold_count = 0,
        historical_reference_count = 0,
        metadata_deleted_count = 0,
        cas_deleted_count = 0,
        vault_deleted_count = 0
    WHERE observer.retention_runs.status = 'completed'
       OR observer.retention_runs.lease_expires_at <= p_now
       OR (
            observer.retention_runs.run_id = p_run_id
            AND observer.retention_runs.worker_id = p_worker_id
       )
    RETURNING
        observer.retention_runs.run_id,
        observer.retention_runs.worker_id,
        observer.retention_runs.lease_generation;
END
$function$;

CREATE OR REPLACE FUNCTION observer.expire_retention_metadata(
    p_site_id text,
    p_run_id text,
    p_worker_id text,
    p_run_generation bigint,
    p_now timestamptz,
    p_limit integer
)
RETURNS TABLE (
    scanned_count bigint,
    eligible_count bigint,
    legal_hold_count bigint,
    historical_reference_count bigint,
    metadata_deleted_count bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer, context
AS $function$
DECLARE
    v_scanned bigint;
    v_eligible bigint;
    v_held bigint;
    v_historical bigint;
    v_deleted bigint := 0;
    v_ref text;
    v_sha text;
BEGIN
    IF p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'invalid retention batch size'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM 1
    FROM observer.retention_runs AS run
    WHERE run.site_id = p_site_id
      AND run.run_id = p_run_id
      AND run.worker_id = p_worker_id
      AND run.lease_generation = p_run_generation
      AND run.status = 'running'
      AND run.lease_expires_at > p_now
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'retention run fence conflict'
            USING ERRCODE = 'serialization_failure';
    END IF;

    SELECT preview.scanned_count, preview.eligible_count,
           preview.legal_hold_count, preview.historical_reference_count
    INTO v_scanned, v_eligible, v_held, v_historical
    FROM observer.preview_retention_batch(p_site_id, p_now, p_limit) AS preview;

    CREATE TEMP TABLE retention_event_batch ON COMMIT DROP AS
    SELECT event.event_id
    FROM observer.observation_events AS event
    WHERE event.site_id = p_site_id
      AND event.retention_until IS NOT NULL
      AND event.retention_until <= p_now
      AND NOT EXISTS (
          SELECT 1
          FROM observer.evidence_refs AS evidence
          JOIN observer.legal_holds AS hold
            ON hold.site_id = evidence.site_id
           AND hold.evidence_id = evidence.evidence_id
          WHERE evidence.site_id = p_site_id
            AND evidence.event_id = event.event_id
            AND hold.released_at IS NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM observer.evidence_refs AS evidence
          JOIN context.evidence_records AS historical
            ON historical.site_id = evidence.site_id
           AND historical.observer_evidence_id = evidence.evidence_id
          WHERE evidence.site_id = p_site_id
            AND evidence.event_id = event.event_id
      )
    ORDER BY event.retention_until, event.event_id
    LIMIT p_limit
    FOR UPDATE OF event SKIP LOCKED;

    CREATE TEMP TABLE retention_delivery_batch ON COMMIT DROP AS
    SELECT delivery.connector, delivery.connector_instance_id, delivery.delivery_id
    FROM observer.inbound_deliveries AS delivery
    WHERE delivery.site_id = p_site_id
      AND delivery.retention_until <= p_now
      AND NOT EXISTS (
          SELECT 1
          FROM observer.observation_events AS event
          WHERE event.site_id = delivery.site_id
            AND event.connector = delivery.connector
            AND event.connector_instance_id = delivery.connector_instance_id
            AND event.delivery_id = delivery.delivery_id
            AND NOT EXISTS (
                SELECT 1
                FROM retention_event_batch AS selected
                WHERE selected.event_id = event.event_id
            )
      )
    ORDER BY delivery.retention_until, delivery.delivery_id
    LIMIT p_limit
    FOR UPDATE OF delivery SKIP LOCKED;

    CREATE TEMP TABLE retention_cas_candidates (
        object_ref text PRIMARY KEY,
        object_sha256 char(64) NOT NULL
    ) ON COMMIT DROP;

    INSERT INTO retention_cas_candidates (object_ref, object_sha256)
    SELECT raw.object_ref, raw.sha256
    FROM observer.raw_objects AS raw
    JOIN observer.observation_events AS event
      ON event.site_id = raw.site_id
     AND event.raw_object_id = raw.object_id
    JOIN retention_event_batch AS selected ON selected.event_id = event.event_id
    WHERE raw.site_id = p_site_id
    ON CONFLICT (object_ref) DO NOTHING;

    INSERT INTO retention_cas_candidates (object_ref, object_sha256)
    SELECT evidence.content_object_ref, evidence.raw_sha256
    FROM observer.evidence_refs AS evidence
    JOIN retention_event_batch AS selected ON selected.event_id = evidence.event_id
    WHERE evidence.site_id = p_site_id
      AND evidence.content_object_ref IS NOT NULL
    ON CONFLICT (object_ref) DO NOTHING;

    INSERT INTO retention_cas_candidates (object_ref, object_sha256)
    SELECT delivery.object_ref, delivery.exact_body_sha256
    FROM observer.inbound_deliveries AS delivery
    JOIN retention_delivery_batch AS selected
      ON selected.connector = delivery.connector
     AND selected.connector_instance_id = delivery.connector_instance_id
     AND selected.delivery_id = delivery.delivery_id
    WHERE delivery.site_id = p_site_id
      AND delivery.object_ref IS NOT NULL
    ON CONFLICT (object_ref) DO NOTHING;

    DELETE FROM observer.local_pilot_dead_letter AS dead
    WHERE dead.site_id = p_site_id
      AND (
          EXISTS (
              SELECT 1
              FROM observer.context_publication_outbox AS outbox
              JOIN retention_event_batch AS selected
                ON selected.event_id = outbox.observation_event_id
              WHERE outbox.site_id = dead.site_id
                AND outbox.outbox_id = dead.outbox_id
          )
          OR EXISTS (
              SELECT 1
              FROM observer.processing_jobs AS job
              JOIN retention_delivery_batch AS selected
                ON selected.connector = job.connector
               AND selected.connector_instance_id = job.connector_instance_id
               AND selected.delivery_id = job.delivery_id
              WHERE job.site_id = dead.site_id
                AND job.job_id = dead.job_id
          )
      );

    DELETE FROM observer.local_pilot_quarantine AS quarantine
    WHERE quarantine.site_id = p_site_id
      AND EXISTS (
          SELECT 1
          FROM observer.processing_jobs AS job
          JOIN retention_delivery_batch AS selected
            ON selected.connector = job.connector
           AND selected.connector_instance_id = job.connector_instance_id
           AND selected.delivery_id = job.delivery_id
          WHERE job.site_id = quarantine.site_id
            AND job.job_id = quarantine.job_id
      );

    DELETE FROM observer.context_publication_outbox AS outbox
    WHERE outbox.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = outbox.observation_event_id
      );

    DELETE FROM observer.derivation_edges AS edge
    WHERE edge.site_id = p_site_id
      AND (
          EXISTS (
              SELECT 1 FROM retention_event_batch AS selected
              WHERE selected.event_id IN (edge.source_id, edge.derived_id)
          )
          OR EXISTS (
              SELECT 1
              FROM observer.evidence_refs AS evidence
              JOIN retention_event_batch AS selected
                ON selected.event_id = evidence.event_id
              WHERE evidence.site_id = edge.site_id
                AND evidence.evidence_id IN (edge.source_id, edge.derived_id)
          )
          OR EXISTS (
              SELECT 1
              FROM observer.processor_runs AS run
              JOIN retention_event_batch AS selected ON selected.event_id = run.event_id
              WHERE run.site_id = edge.site_id
                AND run.processor_run_id = edge.processor_run_id
          )
      );

    DELETE FROM observer.processor_runs AS run
    WHERE run.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = run.event_id
      );
    DELETE FROM observer.consent AS consent
    WHERE consent.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = consent.event_id
      );
    DELETE FROM observer.dead_letter AS dead
    WHERE dead.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = dead.event_id
      );
    DELETE FROM observer.legal_holds AS hold
    WHERE hold.site_id = p_site_id
      AND hold.released_at IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM observer.evidence_refs AS evidence
          JOIN retention_event_batch AS selected ON selected.event_id = evidence.event_id
          WHERE evidence.site_id = hold.site_id
            AND evidence.evidence_id = hold.evidence_id
      );
    DELETE FROM observer.event_evidence AS link
    WHERE link.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = link.event_id
      );
    DELETE FROM observer.evidence_refs AS evidence
    WHERE evidence.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = evidence.event_id
      );
    UPDATE observer.manual_import_jobs AS job
    SET result_event_id = NULL
    WHERE job.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = job.result_event_id
      );
    UPDATE observer.checkpoints AS checkpoint
    SET last_event_id = NULL
    WHERE checkpoint.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = checkpoint.last_event_id
      );

    DELETE FROM observer.observation_events AS event
    WHERE event.site_id = p_site_id
      AND EXISTS (
          SELECT 1 FROM retention_event_batch AS selected
          WHERE selected.event_id = event.event_id
      );
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    DELETE FROM observer.processing_jobs AS job
    WHERE job.site_id = p_site_id
      AND EXISTS (
          SELECT 1
          FROM retention_delivery_batch AS selected
          WHERE selected.connector = job.connector
            AND selected.connector_instance_id = job.connector_instance_id
            AND selected.delivery_id = job.delivery_id
      );
    DELETE FROM observer.inbound_delivery_events AS link
    WHERE link.site_id = p_site_id
      AND EXISTS (
          SELECT 1
          FROM retention_delivery_batch AS selected
          WHERE selected.connector = link.connector
            AND selected.connector_instance_id = link.connector_instance_id
            AND selected.delivery_id = link.delivery_id
      );
    DELETE FROM observer.inbound_deliveries AS delivery
    WHERE delivery.site_id = p_site_id
      AND EXISTS (
          SELECT 1
          FROM retention_delivery_batch AS selected
          WHERE selected.connector = delivery.connector
            AND selected.connector_instance_id = delivery.connector_instance_id
            AND selected.delivery_id = delivery.delivery_id
      );

    DELETE FROM observer.raw_objects AS raw
    WHERE raw.site_id = p_site_id
      AND raw.retention_until <= p_now
      AND EXISTS (
          SELECT 1 FROM retention_cas_candidates AS candidate
          WHERE candidate.object_ref = raw.object_ref
      )
      AND NOT EXISTS (
          SELECT 1 FROM observer.observation_events AS event
          WHERE event.site_id = raw.site_id
            AND event.raw_object_id = raw.object_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM observer.evidence_refs AS evidence
          WHERE evidence.site_id = raw.site_id
            AND evidence.raw_object_id = raw.object_id
      );

    FOR v_ref, v_sha IN
        SELECT candidate.object_ref, candidate.object_sha256
        FROM retention_cas_candidates AS candidate
        ORDER BY candidate.object_ref
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended(p_site_id || chr(31) || v_ref, 0)
        );
        IF NOT EXISTS (
            SELECT 1 FROM observer.raw_objects AS raw
            WHERE raw.site_id = p_site_id AND raw.object_ref = v_ref
        ) AND NOT EXISTS (
            SELECT 1 FROM observer.evidence_refs AS evidence
            WHERE evidence.site_id = p_site_id
              AND evidence.content_object_ref = v_ref
        ) AND NOT EXISTS (
            SELECT 1 FROM observer.inbound_deliveries AS delivery
            WHERE delivery.site_id = p_site_id AND delivery.object_ref = v_ref
        ) THEN
            INSERT INTO observer.retention_cas_tombstones (
                site_id, object_ref, object_sha256, status, created_at, updated_at
            ) VALUES (
                p_site_id, v_ref, v_sha, 'pending', p_now, p_now
            ) ON CONFLICT (site_id, object_ref) DO NOTHING;
        END IF;
    END LOOP;

    INSERT INTO observer.deletion_receipts (
        site_id, receipt_id, target_type, target_id, target_sha256,
        outcome, retained_reason, deleted_at
    )
    SELECT
        p_site_id,
        'retention-event-' || md5(p_site_id || chr(31) || selected.event_id),
        'observation_event',
        selected.event_id,
        NULL,
        'deleted',
        NULL,
        p_now
    FROM retention_event_batch AS selected
    ON CONFLICT (site_id, receipt_id) DO NOTHING;

    UPDATE observer.retention_runs AS run
    SET
        scanned_count = v_scanned,
        eligible_count = v_eligible,
        legal_hold_count = v_held,
        historical_reference_count = v_historical,
        metadata_deleted_count = v_deleted,
        updated_at = p_now
    WHERE run.site_id = p_site_id
      AND run.run_id = p_run_id
      AND run.worker_id = p_worker_id
      AND run.lease_generation = p_run_generation;

    RETURN QUERY SELECT v_scanned, v_eligible, v_held, v_historical, v_deleted;
END
$function$;

CREATE OR REPLACE FUNCTION observer.claim_retention_cas_deletions(
    p_site_id text,
    p_run_id text,
    p_worker_id text,
    p_run_generation bigint,
    p_now timestamptz,
    p_lease_until timestamptz,
    p_limit integer
)
RETURNS TABLE (object_ref text, object_sha256 text, lease_generation bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    PERFORM 1
    FROM observer.retention_runs AS run
    WHERE run.site_id = p_site_id
      AND run.run_id = p_run_id
      AND run.worker_id = p_worker_id
      AND run.lease_generation = p_run_generation
      AND run.status = 'running'
      AND run.lease_expires_at > p_now
    FOR UPDATE;
    IF NOT FOUND OR p_limit NOT BETWEEN 1 AND 1000
       OR p_lease_until <= p_now OR p_lease_until > p_now + interval '1 hour' THEN
        RAISE EXCEPTION 'retention CAS claim fence conflict'
            USING ERRCODE = 'serialization_failure';
    END IF;
    RETURN QUERY
    WITH candidates AS (
        SELECT tombstone.site_id, tombstone.object_ref
        FROM observer.retention_cas_tombstones AS tombstone
        WHERE tombstone.site_id = p_site_id
          AND (
              tombstone.status = 'pending'
              OR (
                  tombstone.status = 'leased'
                  AND tombstone.lease_expires_at <= p_now
              )
          )
        ORDER BY tombstone.created_at, tombstone.object_ref
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE observer.retention_cas_tombstones AS tombstone
    SET
        status = 'leased',
        run_id = p_run_id,
        lease_owner = p_worker_id,
        lease_expires_at = p_lease_until,
        lease_generation = tombstone.lease_generation + 1,
        updated_at = p_now
    FROM candidates
    WHERE tombstone.site_id = candidates.site_id
      AND tombstone.object_ref = candidates.object_ref
    RETURNING
        tombstone.object_ref,
        tombstone.object_sha256::text,
        tombstone.lease_generation;
END
$function$;

CREATE OR REPLACE FUNCTION observer.complete_retention_cas_deletion(
    p_site_id text,
    p_run_id text,
    p_worker_id text,
    p_run_generation bigint,
    p_object_ref text,
    p_cas_generation bigint,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM observer.retention_runs AS run
        WHERE run.site_id = p_site_id
          AND run.run_id = p_run_id
          AND run.worker_id = p_worker_id
          AND run.lease_generation = p_run_generation
          AND run.status = 'running'
          AND run.lease_expires_at > p_now
    ) THEN
        RETURN false;
    END IF;
    UPDATE observer.retention_cas_tombstones AS tombstone
    SET
        status = 'deleted',
        run_id = NULL,
        lease_owner = NULL,
        lease_expires_at = NULL,
        updated_at = p_now,
        deleted_at = p_now
    WHERE tombstone.site_id = p_site_id
      AND tombstone.object_ref = p_object_ref
      AND tombstone.status = 'leased'
      AND tombstone.run_id = p_run_id
      AND tombstone.lease_owner = p_worker_id
      AND tombstone.lease_generation = p_cas_generation
      AND tombstone.lease_expires_at > p_now;
    RETURN FOUND;
END
$function$;

CREATE OR REPLACE FUNCTION observer.complete_retention_run(
    p_site_id text,
    p_run_id text,
    p_worker_id text,
    p_run_generation bigint,
    p_now timestamptz,
    p_metadata_deleted bigint,
    p_cas_deleted bigint,
    p_vault_deleted bigint
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, observer
AS $function$
BEGIN
    IF p_metadata_deleted < 0 OR p_cas_deleted < 0 OR p_vault_deleted < 0 THEN
        RETURN false;
    END IF;
    UPDATE observer.retention_runs AS run
    SET
        status = 'completed',
        metadata_deleted_count = p_metadata_deleted,
        cas_deleted_count = p_cas_deleted,
        vault_deleted_count = p_vault_deleted,
        updated_at = p_now,
        completed_at = p_now
    WHERE run.site_id = p_site_id
      AND run.run_id = p_run_id
      AND run.worker_id = p_worker_id
      AND run.lease_generation = p_run_generation
      AND run.status = 'running'
      AND run.lease_expires_at > p_now;
    RETURN FOUND;
END
$function$;

REVOKE ALL ON FUNCTION observer.enforce_inbound_delivery_retention() FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.enforce_observer_retention_boundary() FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.reject_tombstoned_cas_reference() FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.preview_retention_batch(text, timestamptz, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.claim_retention_run(
    text, text, text, timestamptz, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.expire_retention_metadata(
    text, text, text, bigint, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.claim_retention_cas_deletions(
    text, text, text, bigint, timestamptz, timestamptz, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.complete_retention_cas_deletion(
    text, text, text, bigint, text, bigint, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION observer.complete_retention_run(
    text, text, text, bigint, timestamptz, bigint, bigint, bigint
) FROM PUBLIC;

ALTER TABLE observer.retention_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.retention_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS retention_runs_site_isolation ON observer.retention_runs;
CREATE POLICY retention_runs_site_isolation ON observer.retention_runs
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

ALTER TABLE observer.retention_cas_tombstones ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.retention_cas_tombstones FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS retention_cas_tombstones_site_isolation
    ON observer.retention_cas_tombstones;
CREATE POLICY retention_cas_tombstones_site_isolation
    ON observer.retention_cas_tombstones
    USING (site_id = current_setting('app.site_id', true))
    WITH CHECK (site_id = current_setting('app.site_id', true));

REVOKE ALL ON observer.retention_runs FROM PUBLIC;
REVOKE ALL ON observer.retention_cas_tombstones FROM PUBLIC;
GRANT SELECT ON observer.retention_runs TO gbos_observer_app;
GRANT SELECT ON observer.retention_cas_tombstones TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.preview_retention_batch(
    text, timestamptz, integer
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.claim_retention_run(
    text, text, text, timestamptz, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.expire_retention_metadata(
    text, text, text, bigint, timestamptz, integer
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.claim_retention_cas_deletions(
    text, text, text, bigint, timestamptz, timestamptz, integer
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.complete_retention_cas_deletion(
    text, text, text, bigint, text, bigint, timestamptz
) TO gbos_observer_app;
GRANT EXECUTE ON FUNCTION observer.complete_retention_run(
    text, text, text, bigint, timestamptz, bigint, bigint, bigint
) TO gbos_observer_app;
