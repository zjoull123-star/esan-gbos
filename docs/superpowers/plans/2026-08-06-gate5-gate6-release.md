# ESAN GBOS Gate 5–6 Governed Analytics and Release Plan

**Goal:** turn the locally verified Gate 4 workflow into a governed analytical
surface and a production-operable release package without inventing Kingdee,
cloud, privacy, UAT, or production evidence.

**Execution rule:** implementation and deterministic local verification may
proceed without external credentials. A real Kingdee canary, Tencent Cloud
preproduction deployment, real-user UAT, cross-border approval, and production
cutover remain separate external entry gates. Missing entry inputs produce
`blocked_external_input`; they never trigger a mock-to-live fallback or a false
Go decision.

## Gate 5

### Task 1: Governed Metrics service

- Add a PostgreSQL-backed `services/metrics` service with an additive migration.
- Load only versioned definitions from a Gate 5 registry.
- Accept only an exact metric key and bounded window; never accept SQL,
  expressions, table names, arbitrary dimensions, URLs, or model-generated
  queries.
- Persist governed projection batches, source lineage, freshness, coverage,
  reconciliation, checkpoint, and immutable query audit.
- Return an `available` value only when freshness is fresh, coverage is
  sufficient, reconciliation passed, and every source is governed.
- Return `unavailable` without a value or unit when any quality gate fails.
- Make synthetic and live source modes mutually exclusive and visibly label
  synthetic output.

### Task 2: Kingdee read-only adapter and MCP boundary

- Add an independent `services/kingdee_adapter`; do not import the Gate 2 mock
  in the runtime package.
- Expose only the seven frozen `.get` tools and `metadata.get`.
- Validate every request against exact logical-object, field, filter, order,
  offset, row, timeout, site, account-set, scope, and purpose allowlists.
- Require per-request authenticated `kingdee-read` scope, destination
  allowlisting, token redaction, request budget, and structured audit.
- Reject create/update/save/submit/audit/unaudit/delete/payment, generic form,
  arbitrary SQL, raw URL, token passthrough, and unknown fields before any
  transport call.
- Provide a deterministic local transport for tests and a disabled live
  transport requiring explicit environment entry gates. A live failure returns
  unavailable and never falls back to synthetic data.
- Split live verification into startup, authentication, metadata, and business
  query evidence. Do not claim a later step from an earlier one.

### Task 3: Projection, reconciliation, exceptions, and CEO read surface

- Ingest bounded adapter responses into immutable governed projection batches.
- Store Crosswalk and incremental checkpoint state without updating Kingdee.
- Reconcile source counts/totals before promoting a batch.
- Generate exceptions from deterministic policies, not from LLM arithmetic.
- Add versioned metrics BFF endpoints and a CEO cockpit that reads only Metrics
  API responses.
- Show value, unit, window, as-of, freshness, coverage, reconciliation,
  lineage, source mode, and unavailable reason.
- Never show a stale or failed value as an official number.
- Keep the Gate 1 synthetic dashboard visibly separate from official metrics.

### Task 4: Gate 5 verification and evidence

- Contract and negative-surface tests.
- PostgreSQL migration, RLS, idempotency, checkpoint, reconciliation, and
  backup/restore tests.
- Adapter authentication/scope/SSRF/token-redaction/row-budget/timeout tests.
- Assert writer discovery count and Kingdee mutation call count are zero.
- Metrics quality-gate and synthetic/live isolation tests.
- Frontend accessibility, responsive, offline, cache, error, and lineage tests.
- Produce separate statuses for local technical readiness, live Kingdee canary,
  preproduction, privacy/security review, and UAT.

## Gate 6

### Task 5: Production topology and immutable release inputs

- Add versioned, digest-pinned single-tenant production and site-per-tenant
  templates.
- Separate application, MariaDB, PostgreSQL/pgvector, queue/cache, object
  storage, ingress/WAF, secrets/KMS, monitoring, and backup identities.
- Default every connector, live model, Kingdee, external send, and destructive
  operation to disabled.
- Add an environment preflight that fails closed on floating images, missing
  TLS/domain/secrets, public data ports, absent backup target, missing privacy
  approval, or unapproved release identity.
- Produce an immutable release manifest containing source commit, lockfile and
  image digests, migration versions, SBOM/checksums, feature flags, and rollback
  target.

### Task 6: Operability, security, privacy, and recovery

- Add service health/readiness, queue depth/age, dead-letter, error-rate,
  latency, saturation, DB, backup-age, evidence integrity, metrics freshness,
  reconciliation, connector, and audit alerts.
- Define SLOs, alert severities, ownership, escalation, runbooks, and
  maintenance windows.
- Add recoverable backup, restore, PITR, and regional-disaster procedures with
  RPO/RTO assertions and integrity checks.
- Add incident response, credential rotation, compromised connector, model
  kill switch, data breach, privacy request, retention/deletion/legal-hold,
  support access, and audit export runbooks.
- Add machine-checkable retention, deletion, export, consent withdrawal, legal
  hold, and cross-border approval manifests.
- Ensure logs and evidence packages contain no secrets, tokens, raw
  communication content, complete phone numbers, or complete email addresses.

### Task 7: Release, rollback, and Go/No-Go

- Add staged migration, compatibility, smoke, load, security, privacy,
  accessibility, backup/restore, failover, rollback, and post-deploy checks.
- Require two-person authorization for production cutover and rollback.
- Keep migration rollback data-safe; use forward fixes where schema reversal is
  unsafe.
- Produce a decision matrix with independent statuses for code, local runtime,
  preproduction, Kingdee canary, security, privacy/cross-border, UAT, backup/DR,
  operations, and production.
- The overall Gate 6 result is `go` only when all mandatory external evidence
  exists. With local assets complete but external inputs absent, report
  `technical_local_go` and `production_no_go: blocked_external_input`.

## Required verification

1. Run the complete Python, Frappe, Vue, contract, security, license, SBOM,
   migration, browser, and backup/restore suites.
2. Run each new negative capability test, including writer discovery, arbitrary
   query, SSRF, token passthrough, stale metric, failed reconciliation,
   synthetic/live mixing, unsafe cache, missing approval, floating image, and
   disabled kill switch.
3. Build a fresh site twice, migrate twice, restore backups into clean
   databases, and verify stable record/checksum results.
4. Commit raw logs only as CI artifacts; commit summaries, hashes, manifest,
   limitations, and links.
5. Do not start real Kingdee, cloud, channel, or production actions until their
   explicit entry gates are satisfied.
