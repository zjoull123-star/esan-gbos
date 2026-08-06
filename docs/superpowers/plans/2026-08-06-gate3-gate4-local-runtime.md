# ESAN GBOS Gate 3–4 Local Runtime Implementation Plan

> This plan is executable only after Gate 2 receives a verified `Go`. Gate 3
> must pass before Gate 4 runtime work begins.

## Goal

Implement the local, synthetic-data-capable observation, context, decision and
agent runtime required by Gate 3 and Gate 4 without connecting Kingdee, sending
external messages, using production channel credentials, or presenting
synthetic output as an official business metric.

## Frozen boundaries

- Gate 3 owns `Observation → Evidence Record → ExtractedFact proposal`.
- Gate 4 owns `Conflict → Verified Fact → Decision → Action Proposal →
  Action Guard → Review Case → ApprovedCommand`.
- Observer and Agent Runtime never connect directly to MariaDB/Frappe tables.
- Context and Agent persistence use PostgreSQL with explicit `site_id`.
- Raw evidence bytes live behind an immutable object-store interface; Frappe
  receives references and summaries only.
- Real model providers are disabled. Gate 3 uses deterministic, tool-free test
  processors behind the production interface and labels every output
  `synthetic` or `deterministic_test_processor`.
- No `kingdee-*` scope, endpoint, environment variable, import, or network
  destination may exist in Gate 3/4 runtime configuration.
- No external send, quotation publication, price/discount/payment/delivery
  commitment, Won/Lost change, order, payment, delete, archive, or Kingdee
  mutation is executable.

## Repository layout

```text
services/observer/
  observer/
    api.py
    application.py
    canonicalize.py
    evidence_store.py
    processing.py
    security.py
    storage.py
  migrations/
  fixtures/

services/context/
  context_service/
    api.py
    application.py
    models.py
    storage.py
    temporal.py
  migrations/

services/agent_runtime/
  agent_runtime/
    api.py
    actions.py
    agents.py
    guard.py
    queue.py
    runtime.py
    sandbox.py
    storage.py
  migrations/

tests/observer/
tests/context/
tests/agent_runtime/
tests/integration/
docs/evidence/gate3/
docs/evidence/gate4/
```

## Gate 3 implementation

### Task 1: Service foundations and PostgreSQL schema

1. Write failing tests for configuration defaults, explicit site identity,
   schema migration idempotence and service startup with all production
   connectors/model providers disabled.
2. Add PostgreSQL migrations for:
   - observation events and deduplication keys;
   - connector checkpoints, replay windows and dead-letter records;
   - immutable evidence records and derivation edges;
   - fact proposals and entity-resolution proposals;
   - retention, deletion receipt and legal-hold state.
3. Add repository interfaces plus PostgreSQL implementations. Unit tests may
   use deterministic fakes, but Gate 3 integration acceptance must run against
   the pinned observer PostgreSQL container.
4. Prove migrations run twice, tenant predicates are mandatory, and no service
   can issue an unscoped list/get query.

### Task 2: Approved local connector and canonical ingestion

1. Implement `manual_import` as the first approved local connector. It accepts
   fixture email/message/transcript packages only; all real provider
   connectors remain disabled until a separate approved test account exists.
2. Authenticate imports with a local test-only service identity, enforce body,
   attachment, media type and decompression budgets, and quarantine rejected
   uploads.
3. Implement provider ID and content-hash fallback idempotency, checkpoint
   advancement, replay, out-of-order handling and dead-letter recovery.
4. Validate every normalized event against
   `CanonicalObservationEvent`; duplicate input returns the original stable
   event ID without duplicating evidence or facts.

### Task 3: Immutable evidence and lifecycle

1. Implement a content-addressed local object-store adapter for tests and an
   object-store protocol for later cloud use.
2. Verify SHA-256 on write/read, reject overwrite under an existing digest, and
   preserve message offset or recording start/end location.
3. Implement retention due calculation, consent withdrawal, deletion receipt
   and legal-hold override. Deletion removes bytes while retaining the minimum
   non-sensitive audit tombstone.
4. Ensure logs never contain raw message bodies, full email/telephone values,
   tokens, object bytes or unrestricted evidence URLs.

### Task 4: Deterministic processing and Context write path

1. Define interfaces for transcription, language detection, Chinese summary
   and fact extraction; prohibit tool/network access inside processors.
2. Implement deterministic test processors for text and transcript fixtures.
   They must record processor, prompt/rule and output versions, budget use and
   input EvidenceRef.
3. Persist only `ExtractedFact.status = proposed`; no processor can confirm a
   fact, resolve a conflict, create a Decision, DraftMutation or
   ApprovedCommand.
4. Implement entity-resolution proposals and conflict detection. Uncertain
   matches create review proposals; they do not merge identities.

### Task 5: Gate 3 security and acceptance

Required tests:

- duplicate, replay, out-of-order and checkpoint recovery;
- cross-site event/evidence/fact access denial;
- consent withdrawal, deletion, retention and legal hold;
- malicious filename, MIME mismatch, oversized/decompression-bomb input;
- prompt injection retained as untrusted content and unable to select tools;
- hash/offset/time-range evidence replay;
- network/model/Kingdee call counts equal zero;
- PostgreSQL migration and backup/restore smoke;
- contract examples and all Gate 0–2 regression tests.

Gate 3 evidence must distinguish:

- `technical_local_go`: fixture/manual-import pipeline and PostgreSQL runtime;
- `external_channel_canary`: `not_started` until a user-approved provider test
  account and data-processing basis are available;
- production, Kingdee, real model and cloud readiness: `no_go`.

Gate 4 may begin only when the local Gate 3 runtime is `Go`, every
Critical/High finding is closed or formally accepted, and the remaining
external-channel canary is not represented as completed.

## Gate 4 implementation

### Task 1: Durable Agent Task queue

1. Write concurrency tests before implementation: simultaneous workers may
   lease a task once only; an expired lease is recoverable; a live lease cannot
   be stolen.
2. Add Agent Task, Timeline and dead-letter PostgreSQL migrations. Implement
   `FOR UPDATE SKIP LOCKED`, explicit lease owner/expiry, heartbeat, attempt and
   maximum-attempt handling.
3. Enforce idempotency by `site_id + idempotency_key`, deterministic retry
   classification and parent/causation/correlation lineage.
4. Record timeline sequence transactionally. Cross-record ordering is checked
   in repository/service tests, not delegated to JSON Schema.

### Task 2: Context/Decision workflow

1. Implement conflict creation without overwriting proposals.
2. Implement human/rule confirmation that emits
   `VerifiedBusinessFact` only with evidence, proposal version and a
   Decision Record.
3. Enforce optimistic version checks and double time
   (`valid_time`/`recorded_time`). Reject stale confirmation and silent fact
   replacement; supersession remains explicit and reversible in audit.
4. Add trace queries from Decision to exact fact/evidence versions.

### Task 3: Action Guard and Frappe review boundary

1. Implement one policy decision point invoked before a tool call and before
   result persistence.
2. Freeze the action matrix:
   - allow scoped read;
   - allow internal AI Draft/Fact Proposal/Work Item proposal;
   - require human review for fact confirmation and reversible internal
     transitions not explicitly auto-allowed;
   - require human approval for commercial commitments;
   - deny external sends, formal documents, order/payment, destructive action
     and every Kingdee mutation.
3. Extend GBOS through a versioned BFF review command, without changing the
   frozen BFF v1 contract or allowing PWA direct `frappe.client` writes.
4. A `GBOS Review Case` approval stores reviewer, reason, target revision,
   before/after state, payload hash and evidence. Only then may an
   `ApprovedCommand` be issued for an allow-listed internal GBOS command.
5. Replay returns the prior result; changed payload under the same key returns
   `idempotency_conflict`; stale revision returns a conflict.

### Task 4: Sales, Purchase and Product/Sample agents

Each agent receives a minimal typed context and may only call registered
in-process tools:

- Sales: summarize evidence and propose an internal follow-up Work Item.
- Purchase: compare synthetic sourcing candidates and propose a Review Case;
  it cannot select a supplier.
- Product/Sample: identify sample feedback and propose an internal sample Work
  Item; it cannot promise a delivery date.

Agent output must carry evidence IDs, policy/model/prompt/tool versions,
confidence, budget use and timeline. Unknown evidence, tool or subject fails
closed.

### Task 5: Model gateway, sandbox and evaluations

1. Implement a provider interface with deterministic local provider only.
   Network-backed providers are disabled by default and absent from Gate 4
   acceptance configuration.
2. Enforce token/cost/time/tool budgets before each step and after provider
   output. Exceeded budgets stop or dead-letter without expanding permissions.
3. Treat all observation text as untrusted data. It cannot alter system policy,
   enable tools, request secrets or bypass review.
4. Run evaluation fixtures for hallucinated facts, wrong entity links,
   cross-site access, prompt injection, tool escalation, action aliasing,
   malicious provider output and ambiguous external-side-effect results.

### Task 6: Gate 4 PWA and acceptance

Add role-scoped review/timeline surfaces to `/gbos/review` and record detail
views. They show:

- proposal versus verified status;
- evidence pointers and deterministic/synthetic labels;
- conflict and decision trace;
- Action Guard outcome and reason;
- reviewer, revision and audit state.

No raw Restricted evidence is cached by the service worker. CEO prototype
cards sourced from synthetic Agent/Context data must display “演示数据” and
must not be named official KPI/forecast/revenue.

Required Gate 4 acceptance:

- concurrent leases do not double execute;
- crash/expiry recovery, retry and dead-letter work;
- all budgets and site/role/purpose/classification checks fail closed;
- Fact → Decision → Action lineage is replayable;
- Action Guard positive and negative matrix passes twice
  (pre-tool/post-result);
- reviewer override is complete and stale/replayed commands are safe;
- Sales/Purchase/Product agents cannot use undeclared tools;
- external send/quote/Won-Lost/order/payment/Kingdee calls all equal zero;
- five core PWA pages retain Gate 1 accessibility/mobile/cache invariants;
- full repository, PostgreSQL integration, static, secret, checksum and
  regression suites pass.

## Evidence and integration sequence

For each Gate:

1. Commit implementation and tests without final evidence metadata.
2. Run the full reproducible verification matrix.
3. Write compact evidence JSON/summary and a Gate-local `SHA256SUMS`.
4. Commit only evidence metadata/checksum.
5. Re-run evidence, secret and regression tests.
6. Fast-forward merge to local `main` only after the Gate is `Go`.

Do not modify historical Gate 0/1 evidence manifests. Do not push, deploy or
activate any external capability without separate authorization.
