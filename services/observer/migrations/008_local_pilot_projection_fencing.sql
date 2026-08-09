ALTER TABLE observer.context_publication_outbox
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'context_publication_outbox_lease_generation_ck'
          AND conrelid = 'observer.context_publication_outbox'::regclass
    ) THEN
        ALTER TABLE observer.context_publication_outbox
            ADD CONSTRAINT context_publication_outbox_lease_generation_ck
            CHECK (lease_generation >= 0) NOT VALID;
    END IF;
END
$$;

ALTER TABLE observer.context_publication_outbox
    VALIDATE CONSTRAINT context_publication_outbox_lease_generation_ck;

CREATE INDEX IF NOT EXISTS context_publication_outbox_projection_claim_idx
    ON observer.context_publication_outbox (
        site_id, status, next_retry_at, created_at, outbox_id
    )
    WHERE status IN ('queued', 'leased', 'retry_wait');

ALTER TABLE observer.context_publication_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE observer.context_publication_outbox FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS context_publication_outbox_site_isolation
    ON observer.context_publication_outbox;
CREATE POLICY context_publication_outbox_site_isolation
    ON observer.context_publication_outbox
    USING (
        site_id = current_setting('app.site_id', true)
        AND EXISTS (
            SELECT 1
            FROM observer.observation_events AS event
            WHERE event.site_id = context_publication_outbox.site_id
              AND event.event_id =
                  context_publication_outbox.observation_event_id
              AND event.processing_purpose = COALESCE(
                  NULLIF(
                      current_setting('app.processing_purpose', true),
                      ''
                  ),
                  'observation_processing'
              )
        )
    )
    WITH CHECK (
        site_id = current_setting('app.site_id', true)
        AND EXISTS (
            SELECT 1
            FROM observer.observation_events AS event
            WHERE event.site_id = context_publication_outbox.site_id
              AND event.event_id =
                  context_publication_outbox.observation_event_id
              AND event.processing_purpose = COALESCE(
                  NULLIF(
                      current_setting('app.processing_purpose', true),
                      ''
                  ),
                  'observation_processing'
              )
        )
    );

REVOKE ALL ON observer.context_publication_outbox FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE
    ON observer.context_publication_outbox
    TO gbos_observer_app;
