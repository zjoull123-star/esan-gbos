# GBOS Independent Email Gateway Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a provider-neutral, human-governed Email Gateway that receives durable Observer email publications, operates a separate CRM Inbox, reuses Frappe identity and routing authority, and sends only after a revision-pinned human ApprovedCommand passes Action Guard.

**Architecture:** Observer remains the sole writer for provider intake, CAS, deliveries, checkpoints/cursors, processing jobs, and quarantine. Frappe remains the sole authority for users, teams, parties, external identities, business owners, Review Cases, and ApprovedCommands. The new PostgreSQL Email Gateway owns only mailbox business configuration, publication receipts, Inbox/Conversation workflow, routing decisions, drafts, authorized Send Outbox rows, provider receipts, and audit; cross-database boundaries use durable outboxes plus idempotent consumers and never claim distributed ACID.

**Tech Stack:** Python 3.14, PostgreSQL 17 with forced RLS, Frappe Framework v16/MariaDB, FastAPI, JSON Schema 2020-12, Vue 3, TypeScript, Vitest, Playwright, Compose, `MountedFileSecretProvider`, existing Observer CAS and Action Guard.

**Approved design:** `docs/superpowers/specs/2026-08-13-gbos-email-gateway-design.md` at `bfed9699d3be456a50cfe07dbcfd9c3bcfac6385`

---

## Frozen boundaries and execution rules

- Begin from `bfed969` or a reviewed descendant. Preserve unrelated dirty files and never share a write-owned file between concurrent workers.
- Keep `local_pilot_go=false`, `production_go=false`, `external_send=false`, and every new Gateway/WeCom kill switch closed in committed manifests.
- Do not access a real mailbox, create a WeCom application, read a provider secret, call DeepSeek, or send mail in Chunks 1–3.
- Do not reuse `wecom_archive.py`; it is conversation-archive code. Application mail uses provider name `wecom_app_mail`.
- Do not add Gateway tables for provider deliveries, cursor/checkpoint, raw EML, attachments, quarantine, or an independent identity revision.
- Do not write participant raw addresses, provider subject IDs, bodies, or secrets to Gateway tables, logs, metrics, URLs, ordinary DOM, exception text, or `repr`. The configured mailbox address is the sole persistent exception: store it only in its designated encrypted/Restricted field and reveal it only to authorized config roles. A Frappe User ref may be email-shaped but is not evidence that an external participant address was confirmed. Send Outbox stores only opaque role-tagged participant refs and mapping revisions; raw sender/recipient material remains in the final MIME EvidenceRef under Observer and may exist only transiently in the authorized send process.
- Use only platform-managed logical secret names resolved by `MountedFileSecretProvider`; tests inject fakes and never depend on Keychain or live credentials.
- Preserve `/gbos/communications` and historical observations. Add a separate Email Inbox surface and do not migrate old observations into conversations.
- Every database write is site-scoped, revision-pinned, idempotent, audited, and protected by FORCE RLS plus least grants.
- Each task starts with an observable RED, makes the smallest implementation GREEN, runs its focused regressions, and commits only the named paths.
- For every commit, pass every task-owned file as an explicit pathspec (one `git add --` command may list many exact files); parent-directory, wildcard, and repository-wide pathspecs are forbidden. Run `git diff --cached --name-only | sort` and require an exact match with the task-owned file set before committing.

## File ownership map

### Contracts and governance owner

- Create: `contracts/email_gateway/`
- Modify: `contracts/README.md`
- Modify: `docs/adr/ADR-0004-ai-drafts-and-human-commands.md`
- Modify: `docs/permission-matrix.md`
- Modify: `tests/contracts/`
- Modify: `tests/governance/`

### Observer intake owner

- Create: `services/observer/migrations/014_email_gateway_publication.sql`
- Create: `services/observer/migrations/015_wecom_app_mail_signals.sql`
- Create: `services/observer/observer/email_publication.py`
- Create: `services/observer/observer/email_checkpoint_fence.py`
- Create: `services/observer/observer/email_address_match.py`
- Create: `services/observer/observer/connectors/email_provider.py`
- Create: `services/observer/observer/connectors/fake_email_provider.py`
- Create: `services/observer/observer/connectors/wecom_app_mail.py`
- Create: `services/observer/observer/connectors/wecom_app_mail_callback.py`
- Modify: existing Observer email ingestion, scheduler, storage, normalizer, and tests listed per task.

### Email Gateway owner

- Create: `services/email_gateway/` and `services/email_gateway/migrations/`
- Create: `tests/email_gateway/`
- Create: `tests/integration/test_email_gateway_*.py`
- Do not write Observer or Frappe files while another owner is active.

### Frappe authority and command owner

- Create: closed Gateway authority and email-send command modules under `apps/esan_gbos/esan_gbos/`.
- Modify: `GBOS Party Profile`, `GBOS External Identity` review policy, Review Case, role/permission fixtures, and their tests.
- Own all new Frappe DocTypes for Email Send Approval, Approved Command, and Command Publication.

### Runtime and infrastructure owner

- Create: Gateway/WeCom entrypoints under `services/local_pilot_runtime/`.
- Modify: `infra/local/compose.yml`, manifests, entrypoint inventory, render/migrate/secret scripts, runtime contract, and infra tests.
- The primary agent alone owns live provider activation, production mutations, evidence, merge, and push.

### BFF and PWA owner

- Create: closed BFF v5 Email API and frontend Email Gateway modules/views/components/tests.
- Modify: frontend router/navigation/service worker only after backend contract tests are GREEN.

---

## Chunk 1: Provider-neutral Gateway core

### Task 1: Freeze the provider-neutral Email contracts

**Files:**

- Create: `contracts/email_gateway/email-message-publication-v1.0.schema.json`
- Create: `contracts/email_gateway/mailbox-connector-projection-v1.0.schema.json`
- Create: `contracts/email_gateway/frappe-identity-projection-v1.0.schema.json`
- Create: `contracts/email_gateway/frappe-route-authority-v1.0.schema.json`
- Create: `contracts/email_gateway/email-address-match-attestation-v1.0.schema.json`
- Create: `contracts/email_gateway/examples/provider-neutral-v1.json` containing the named valid and invalid cases for all five schemas
- Modify: `contracts/README.md`
- Create: `tests/contracts/test_email_gateway_contracts.py`

- [ ] **Step 1: Write failing complete-set and closed-shape tests**

  Require exact schema sets, reject additional properties, raw email/phone/provider IDs, duplicate address roles, unbounded strings, unknown enum members, bad ULIDs/digests/revisions, and mismatched site/team/purpose.

  ```python
  def test_email_publication_rejects_raw_address() -> None:
      value = valid_publication()
      value["participants"][0]["identity_ref"] = "sales@example.invalid"
      with pytest.raises(jsonschema.ValidationError):
          publication_validator.validate(value)
  ```

- [ ] **Step 2: Run the contract test and verify RED**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py -q`

  Expected: FAIL because `contracts/email_gateway` and the five schemas do not exist.

- [ ] **Step 3: Add the five closed schemas and examples**

  The publication payload must contain only `publication_id`, site/mailbox/config/Observer references, received time, opaque role-tagged participants, subject/header digests, evidence references, revision, and idempotency key. Mailbox connector projection must bind `activation_watermark` to mailbox/config revision; Observer rejects every provider candidate earlier than it before delivery/CAS. Address-match attestation contains only opaque address ref, candidate target ref/type, evidence ref, normalization version, boolean match, observed time, expiry, and digest; no raw address. Route authority must return exactly one of:

  ```json
  {"route_status":"assigned","party_ref":"PTY-01KZA5X900WTDAZG0748EWQ7ZW","party_revision":2,"team_ref":"TEM-01KZQB094BY8XFHEBB3ENN6TAW","team_revision":3,"owner_user_ref":"user@example.invalid","owner_eligibility_revision":"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","resolved_at":"2026-08-13T00:00:00Z"}
  ```

  ```json
  {"route_status":"unassigned","safe_reason_code":"owner_unavailable","resolved_at":"2026-08-13T00:00:00Z"}
  ```

- [ ] **Step 4: Verify contracts GREEN**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/contracts -q`

  Expected: all contract tests PASS.

- [ ] **Step 5: Commit the contract freeze**

  ```bash
  git add -- contracts/email_gateway/email-message-publication-v1.0.schema.json contracts/email_gateway/mailbox-connector-projection-v1.0.schema.json contracts/email_gateway/frappe-identity-projection-v1.0.schema.json contracts/email_gateway/frappe-route-authority-v1.0.schema.json contracts/email_gateway/email-address-match-attestation-v1.0.schema.json contracts/email_gateway/examples/provider-neutral-v1.json contracts/README.md tests/contracts/test_email_gateway_contracts.py
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): freeze provider-neutral contracts"
  ```

### Task 2: Extend Frappe authority without creating a second identity system

**Files:**

- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/gbos_party_profile.json`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/gbos_party_profile.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v1/party.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/identity_review.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v4/identity.py`
- Modify: `apps/esan_gbos/esan_gbos/api/internal/identity_resolution.py`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.py`
- Create: `apps/esan_gbos/esan_gbos/domain/external_identity_projection.py`
- Create: `apps/esan_gbos/esan_gbos/api/internal/email_gateway_authority.py`
- Create: `apps/esan_gbos/esan_gbos/email_gateway_authority_access.py`
- Create: `apps/esan_gbos/esan_gbos/email_gateway_authority_service.py`
- Modify: `apps/esan_gbos/esan_gbos/hooks.py`
- Modify: `apps/esan_gbos/esan_gbos/install.py`
- Modify: `apps/esan_gbos/esan_gbos/fixtures/role.json`
- Modify: `apps/esan_gbos/esan_gbos/domain/permissions.py`
- Modify: `apps/esan_gbos/esan_gbos/permissions.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/test_gbos_party_profile.py`
- Create: `tests/domain/test_internal_email_gateway_authority.py`
- Create: `tests/domain/test_email_gateway_authority_service.py`
- Extend: `tests/domain/test_identity_review.py`
- Extend: `tests/domain/test_internal_identity_resolution.py`
- Extend: `tests/domain/test_permissions.py`
- Create: `scripts/dev/test-email-gateway-frappe`
- Create: `tests/infra/test_email_gateway_frappe.py`

- [ ] **Step 1: Write RED tests for Party owner and route authority**

  Cover disabled/cross-team/non-member owner, inactive/unapproved Party, missing owner, stale expected revisions, ambiguous records, and forbidden inference from document owner, Contact, CC, prior Gateway state, or Deal. Every unsafe case must return `unassigned` rather than a user.

- [ ] **Step 2: Write RED tests for purpose-specific identity review**

  Prove employee/User mapping is approvable only by GBOS Admin or Integration Admin; customer/Party mapping only by same-team Sales Manager or Reviewer; Sales User, AI, generic DocType writes, and cross-team actors fail. Add a human-origin submission path that does not require an AI suggestion.

- [ ] **Step 3: Run focused tests and verify RED**

  Run: `uv run --frozen pytest tests/domain/test_internal_email_gateway_authority.py tests/domain/test_email_gateway_authority_service.py tests/domain/test_identity_review.py tests/domain/test_permissions.py -q`

  Expected: FAIL on missing owner field, authority endpoint, service role, and human submission policy.

- [ ] **Step 4: Add `owner_user` and exact eligibility validation**

  Add a nullable Link to User on Party Profile. Accept a non-empty value only when it is an enabled System User and enabled member of the exact Party team; an empty owner remains valid and routes to unassigned. Compute `owner_eligibility_revision` as a stable digest over User enabled state, exact membership identity/version, team revision, and Party revision; do not invent a mutable counter on User.

- [ ] **Step 5: Extract one shared external-identity projection builder**

  Both the existing Observer resolver and new Gateway authority endpoint must call the same builder. The Gateway endpoint returns only mapping ref/revision/status/type/team; it never returns raw external subject, email address, Contact email, or unrestricted DocType data. Extend `test_internal_identity_resolution.py` so extraction cannot change the existing resolver response or purpose binding.

- [ ] **Step 6: Implement the dedicated authority service identity**

  Add desk-less role `Email Gateway Authority Consumer`, exact site/purpose/auth-ref/token checks, no standard/custom DocPerm, no list/export/delete/share/print/email, `Cache-Control: no-store`, and request-scope cleanup. Load its credential through the mounted secret provider in runtime composition later.

- [ ] **Step 7: Implement purpose-specific Review Case commands**

  Reuse `GBOS External Identity` as sole authority. Human submission stores only opaque address ref, mapping target, purpose, evidence ref, address-match attestation ref/digest, expected revision, request/idempotency IDs, and creates the pinned Review Case. Approval requires an unexpired Observer attestation proving exact normalized equality with the current User email or Party-linked Contact email; evidence mismatch, stale attestation, target drift, and unauthorized target fail. Approval uses existing controlled flags/commands; it never grants general write permission.

- [ ] **Step 8: Verify native and fake-Frappe authority paths**

  First run the focused command from Step 3 and require PASS. Then implement `scripts/dev/test-email-gateway-frappe` to create an exact-current-source, unique Compose project/site/volumes, migrate twice, run only the named native authority modules, and remove containers/networks/volumes in `trap`. Run: `scripts/dev/test-email-gateway-frappe`. Expected: second migration is a no-op, native authority/permission tests PASS, teardown leaves no project, and no email/provider network occurs.

- [ ] **Step 9: Commit Frappe authority changes**

  ```bash
  git add -- apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/gbos_party_profile.json apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/gbos_party_profile.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_party_profile/test_gbos_party_profile.py apps/esan_gbos/esan_gbos/api/v1/party.py apps/esan_gbos/esan_gbos/api/v4/identity.py apps/esan_gbos/esan_gbos/api/internal/identity_resolution.py apps/esan_gbos/esan_gbos/domain/identity_review.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.py apps/esan_gbos/esan_gbos/domain/external_identity_projection.py apps/esan_gbos/esan_gbos/api/internal/email_gateway_authority.py apps/esan_gbos/esan_gbos/email_gateway_authority_access.py apps/esan_gbos/esan_gbos/email_gateway_authority_service.py apps/esan_gbos/esan_gbos/hooks.py apps/esan_gbos/esan_gbos/install.py apps/esan_gbos/esan_gbos/fixtures/role.json apps/esan_gbos/esan_gbos/domain/permissions.py apps/esan_gbos/esan_gbos/permissions.py tests/domain/test_internal_email_gateway_authority.py tests/domain/test_email_gateway_authority_service.py tests/domain/test_identity_review.py tests/domain/test_internal_identity_resolution.py tests/domain/test_permissions.py scripts/dev/test-email-gateway-frappe tests/infra/test_email_gateway_frappe.py
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): add closed Frappe mail authority"
  ```

### Task 3: Preserve email facts and fence Observer checkpoint advancement

**Files:**

- Create: `services/observer/migrations/014_email_gateway_publication.sql`
- Create: `services/observer/observer/email_publication.py`
- Create: `services/observer/observer/email_publication_outbox.py`
- Create: `services/observer/observer/email_checkpoint_fence.py`
- Create: `services/observer/observer/email_address_match.py`
- Create: `services/observer/observer/connectors/email_provider.py`
- Create: `services/observer/observer/connectors/fake_email_provider.py`
- Modify: `services/observer/observer/connectors/email_delivery.py`
- Modify: `services/observer/observer/normalizers.py`
- Modify: `services/observer/observer/models.py`
- Modify: `services/observer/observer/protocols.py`
- Modify: `services/observer/observer/local_pilot_storage.py`
- Modify: `services/observer/observer/local_pilot_sink.py`
- Modify: `services/observer/observer/local_pilot_ingestion.py`
- Modify: `services/observer/observer/scheduler.py`
- Create: `tests/observer/test_email_publication.py`
- Create: `tests/observer/test_email_publication_outbox.py`
- Create: `tests/observer/test_email_checkpoint_fence.py`
- Create: `tests/observer/test_email_address_match.py`
- Create: `tests/observer/test_fake_email_provider.py`
- Extend: `tests/observer/test_email_delivery.py`
- Extend: `tests/observer/test_local_pilot_sink.py`
- Extend: `tests/observer/test_local_pilot_scheduler.py`
- Extend: `tests/integration/test_gate3_postgres.py`
- Create: `scripts/dev/test-email-gateway-postgres`
- Create: `tests/infra/test_email_gateway_postgres_script.py`

- [ ] **Step 1: Capture the current checkpoint-order RED**

  Add a test in which CAS/delivery acceptance succeeds, normalization/publication crashes, and the scheduler is invoked again. Assert the checkpoint must remain unchanged and the same provider item must be recoverable. The current scheduler should fail this assertion because it advances after `DurableDeliveryInbox.accept()`.

- [ ] **Step 2: Add header-role preservation RED tests**

  Parse Subject, Message-ID, In-Reply-To, References, and exact from/to/cc/bcc roles. Persist only bounded subject projection/digest, header digests, opaque identity refs, and EvidenceRefs. Assert raw addresses and header values do not appear in `repr`, logs, metrics, or stored publication JSON.

- [ ] **Step 3: Add migration RED tests**

  Require `email_connector_config_projections`, `email_poll_batches`, `email_poll_batch_deliveries`, and append-only `email_message_publication_outbox`. Require composite site keys, FORCE RLS, exact app grants, immutable publication payload/digest, and idempotent replay. Do not add Gateway or provider cursor tables.

- [ ] **Step 4: Add address-match attestation RED tests**

  Through an authenticated, exact-purpose internal function, compare a transient candidate User/Contact email with the address in one authorized email EvidenceRef using the frozen normalization version. Return only the closed attestation; never persist or log either raw address. Reject stale/mismatched evidence, wrong participant role, wrong site/purpose, unauthorized caller, reused request drift, and expired attestation.

- [ ] **Step 5: Run the focused tests and verify RED**

  Run: `uv run --frozen pytest tests/observer/test_email_delivery.py tests/observer/test_email_publication.py tests/observer/test_email_publication_outbox.py tests/observer/test_email_checkpoint_fence.py tests/observer/test_email_address_match.py tests/observer/test_fake_email_provider.py tests/observer/test_local_pilot_scheduler.py tests/observer/test_local_pilot_sink.py -q`

  Expected: FAIL on missing publication/fence/attestation/fake-provider modules and the checkpoint-advanced-before-publication assertion.

- [ ] **Step 6: Implement the Observer email batch fence**

  Register a poll batch before accepting its deliveries. Each member becomes terminal only after immutable publication is written or Observer quarantine is recorded. A finalizer performs the existing checkpoint CAS only when every member is terminal. Stale leases/generations and partial batches remain retryable.

- [ ] **Step 7: Write publication in the normalized persistence transaction**

  Bind publication to site, mailbox config revision, connector instance, Observer delivery, EvidenceRefs, participant roles, and header digests. Replay with identical digest returns the original publication; drift raises a safe conflict and never advances the checkpoint.

- [ ] **Step 8: Implement the transient address-match endpoint**

  Read the bounded EvidenceRef under existing Restricted policy, normalize only the selected from/to/cc/bcc address and transient candidate with the frozen version, compare in constant-time over digests, and return an expiring signed/digested attestation. The endpoint and its errors expose no address. Gateway never calls it directly; the Frappe identity command requests it through the existing authenticated local-service boundary.

- [ ] **Step 9: Add deterministic fake provider modes**

  Support ordered success, duplicate, out-of-order, 429, timeout, malformed MIME, oversized attachment, and crash/restart without network. Feed the same Observer batch/CAS/delivery/publication path; the fake cannot mutate checkpoints directly.

- [ ] **Step 10: Run focused tests GREEN**

  Run the exact Step 5 command. Expected: all named tests PASS.

- [ ] **Step 11: Run isolated PostgreSQL GREEN**

  Implement `scripts/dev/test-email-gateway-postgres` to use a unique pinned pgvector container, random host port, no volume, 0600 temporary credentials, trap cleanup, and explicit modes `--observer-through 014` and later `--all`. Run: `scripts/dev/test-email-gateway-postgres --observer-through 014`. Expected: migrations 001–014 apply twice, focused Gate3/publication/RLS tests PASS, and the container is removed.

- [ ] **Step 12: Commit the single-writer intake fence**

  ```bash
  git add -- services/observer/migrations/014_email_gateway_publication.sql services/observer/observer/email_publication.py services/observer/observer/email_publication_outbox.py services/observer/observer/email_checkpoint_fence.py services/observer/observer/email_address_match.py services/observer/observer/connectors/email_provider.py services/observer/observer/connectors/fake_email_provider.py services/observer/observer/connectors/email_delivery.py services/observer/observer/normalizers.py services/observer/observer/models.py services/observer/observer/protocols.py services/observer/observer/local_pilot_storage.py services/observer/observer/local_pilot_sink.py services/observer/observer/local_pilot_ingestion.py services/observer/observer/scheduler.py tests/observer/test_email_publication.py tests/observer/test_email_publication_outbox.py tests/observer/test_email_checkpoint_fence.py tests/observer/test_email_address_match.py tests/observer/test_fake_email_provider.py tests/observer/test_email_delivery.py tests/observer/test_local_pilot_sink.py tests/observer/test_local_pilot_scheduler.py tests/integration/test_gate3_postgres.py scripts/dev/test-email-gateway-postgres tests/infra/test_email_gateway_postgres_script.py
  git diff --cached --name-only | sort
  git commit -m "feat(observer): publish checkpoint-fenced email facts"
  ```

### Task 4: Build the independent Gateway database and repositories

**Files:**

- Create: `services/email_gateway/__init__.py`
- Create: `services/email_gateway/models.py`
- Create: `services/email_gateway/protocols.py`
- Create: `services/email_gateway/repository.py`
- Create: `services/email_gateway/postgres.py`
- Create: `services/email_gateway/repositories/__init__.py`
- Create: `services/email_gateway/repositories/mailboxes.py`
- Create: `services/email_gateway/repositories/intake.py`
- Create: `services/email_gateway/repositories/identity.py`
- Create: `services/email_gateway/repositories/workflow.py`
- Create: `services/email_gateway/repositories/audit.py`
- Create: `services/email_gateway/mailboxes.py`
- Create: `services/email_gateway/intake.py`
- Create: `services/email_gateway/identity_projection.py`
- Create: `services/email_gateway/frappe_authority.py`
- Create: `services/email_gateway/routing.py`
- Create: `services/email_gateway/conversations.py`
- Create: `services/email_gateway/drafts.py`
- Create: `services/email_gateway/send_outbox.py`
- Create: `services/email_gateway/audit.py`
- Create: `services/email_gateway/retention.py`
- Create: `services/email_gateway/metrics.py`
- Create: `services/email_gateway/migrations/001_email_gateway_foundation.sql`
- Create: `services/email_gateway/migrations/002_email_gateway_inbox.sql`
- Create: `services/email_gateway/migrations/003_email_gateway_workflow_outboxes.sql`
- Create: `services/email_gateway/migrations/004_email_gateway_retention.sql`
- Create: `tests/email_gateway/conftest.py`
- Create: `tests/email_gateway/test_models.py`
- Create: `tests/email_gateway/test_migrations.py`
- Create: `tests/email_gateway/test_mailboxes.py`
- Create: `tests/email_gateway/test_intake.py`
- Create: `tests/email_gateway/test_identity_projection.py`
- Create: `tests/email_gateway/test_routing.py`
- Create: `tests/email_gateway/test_conversations.py`
- Create: `tests/email_gateway/test_drafts.py`
- Create: `tests/email_gateway/test_send_outbox.py`
- Create: `tests/email_gateway/test_audit.py`
- Create: `tests/email_gateway/test_retention.py`
- Create: `tests/email_gateway/test_metrics.py`
- Create: `tests/integration/test_email_gateway_postgres.py`

- [ ] **Step 1: Write model and repr RED tests**

  Define immutable dataclasses for Mailbox, PublicationReceipt, ChannelMessage, InboxItem, IdentityProjection, RouteDecision, Conversation, Draft, and inert SendOutbox. Reject unknown enum values, extra wire fields, cross-site refs, duplicate participants, invalid revision/digest, and mutable payload aliases. `repr` must redact addresses, subjects, content, identity targets, and secrets.

- [ ] **Step 2: Write migration/RLS/grant RED tests**

  Require `mailboxes`, mailbox config outbox, publication receipts, messages/participants, Inbox items, identity projection receipts, route decisions/rules, conversations/messages/suggestions, drafts, inert send outbox, audit, retention runs, and content-expiration receipts. Explicitly assert absence of delivery, checkpoint, cursor, raw EML, attachment, quarantine, and independent identity-authority tables.

- [ ] **Step 3: Run the Gateway core tests and verify RED**

  Run: `uv run --frozen pytest tests/email_gateway tests/integration/test_email_gateway_postgres.py -q`

  Expected: FAIL on missing Gateway modules, migrations, repositories, RLS/grants, and inert-outbound enforcement.

- [ ] **Step 4: Implement migrations**

  Use a separate `email_gateway.schema_migrations` ledger, composite site foreign keys, FORCE RLS, append-only audit/receipt triggers, CAS revision checks, five-state leases, and least roles. Gateway has no grant on `observer.*`; Observer publisher has no grant on `email_gateway.*`.

  Keep `models.py` limited to bounded value objects, `repository.py` limited to Protocols, and `postgres.py` limited to connection/transaction helpers. Put SQL for mailbox, intake, identity, workflow, and audit in the five focused repository modules; no generic file owns all tables.

- [ ] **Step 5: Implement mailbox CAS and config publication**

  Allow multiple `entry_role=primary`. Store only logical `credential_ref`, address display under Restricted policy, default team, account owner, inbound/outbound flags, status, and config revision. Every security change increments revision and makes old claims/approvals stale.

- [ ] **Step 6: Implement atomic publication receipt and independent Inbox creation**

  One transaction validates the closed publication, creates/gets ChannelMessage, stores the exact mailbox-scoped receipt, and creates exactly one Inbox Item for that mailbox. A duplicate publication returns the same receipt; the same Message-ID at a different mailbox creates another Inbox Item.

- [ ] **Step 7: Implement identity projection and deterministic routing**

  Consume only Frappe mapping ref/revision/status/type/team. Route in this order: mailbox fixes team → live Frappe route authority → same-team explicit rule → unassigned. Missing/ambiguous/stale/disabled/cross-team authority is unassigned. AI output cannot enter the routing command shape.

- [ ] **Step 8: Implement suggestions and manual merge primitives**

  Message-ID family, participant/time, and digest produce `proposed` suggestions only. Accept/reject/split requires authorized actor, expected revisions, same team, idempotency, and audit. No provider thread or confidence score can mutate Conversation automatically.

- [ ] **Step 9: Keep drafts and Send Outbox inert**

  Draft create/edit is internal only. `send_outbox` exists for schema evolution but its repository constructor/insert raises `outbound_not_authorized` while Stage 4 command verification is absent or `outbound_enabled=false`. No outbound transport protocol is imported in Chunks 1–3.

- [ ] **Step 10: Run Gateway unit and PostgreSQL tests**

  First run: `uv run --frozen pytest tests/email_gateway -q`. Expected: PASS. Then run: `scripts/dev/test-email-gateway-postgres --all`. Expected: Observer 001–014 and Gateway 001–004 apply twice, Gateway integration/RLS/grant tests PASS, and the disposable container is removed.

  Expected: unit suite and disposable PostgreSQL migration-twice/RLS/grant suite PASS.

- [ ] **Step 11: Commit the Gateway core**

  ```bash
  git add -- services/email_gateway/__init__.py services/email_gateway/models.py services/email_gateway/protocols.py services/email_gateway/repository.py services/email_gateway/postgres.py services/email_gateway/repositories/__init__.py services/email_gateway/repositories/mailboxes.py services/email_gateway/repositories/intake.py services/email_gateway/repositories/identity.py services/email_gateway/repositories/workflow.py services/email_gateway/repositories/audit.py services/email_gateway/mailboxes.py services/email_gateway/intake.py services/email_gateway/identity_projection.py services/email_gateway/frappe_authority.py services/email_gateway/routing.py services/email_gateway/conversations.py services/email_gateway/drafts.py services/email_gateway/send_outbox.py services/email_gateway/audit.py services/email_gateway/retention.py services/email_gateway/metrics.py services/email_gateway/migrations/001_email_gateway_foundation.sql services/email_gateway/migrations/002_email_gateway_inbox.sql services/email_gateway/migrations/003_email_gateway_workflow_outboxes.sql services/email_gateway/migrations/004_email_gateway_retention.sql tests/email_gateway/conftest.py tests/email_gateway/test_models.py tests/email_gateway/test_migrations.py tests/email_gateway/test_mailboxes.py tests/email_gateway/test_intake.py tests/email_gateway/test_identity_projection.py tests/email_gateway/test_routing.py tests/email_gateway/test_conversations.py tests/email_gateway/test_drafts.py tests/email_gateway/test_send_outbox.py tests/email_gateway/test_audit.py tests/email_gateway/test_retention.py tests/email_gateway/test_metrics.py tests/integration/test_email_gateway_postgres.py
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): add isolated inbox core"
  ```

### Task 5: Compose the default-off core and prove the fake-provider chain

**Files:**

- Create: `services/local_pilot_runtime/email_gateway_config.py`
- Create: `services/local_pilot_runtime/email_gateway_api.py`
- Create: `services/local_pilot_runtime/email_gateway_worker.py`
- Create: `services/local_pilot_runtime/email_publication_worker.py`
- Create: `services/local_pilot_runtime/mailbox_config_projection_worker.py`
- Create: `services/email_gateway/api.py`
- Modify: `services/local_pilot_runtime/runtime_support.py`
- Modify: `services/local_pilot_runtime/channel_config.py`
- Modify: `services/local_pilot_runtime/pollers.py`
- Modify: `contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- Modify: `scripts/local-pilot/migrate`
- Modify: `scripts/local-pilot/render-config`
- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`
- Modify: `infra/local/local-pilot-manifest.json`
- Create: `contracts/bff-v5.openapi.json`
- Create: `apps/esan_gbos/esan_gbos/api/v5/__init__.py`
- Create: `apps/esan_gbos/esan_gbos/api/v5/gateway.py`
- Create: `apps/esan_gbos/esan_gbos/api/v5/email_inbox.py`
- Create: `apps/esan_gbos/esan_gbos/api/v5/email_admin.py`
- Create: `apps/esan_gbos/esan_gbos/domain/v5_email_dto.py`
- Create: `tests/contracts/test_bff_v5_openapi.py`
- Create: `tests/domain/test_bff_v5_email.py`
- Create: `tests/domain/test_bff_v5_runtime.py`
- Create: `apps/esan_gbos/frontend/src/api/email-gateway-types.ts`
- Create: `apps/esan_gbos/frontend/src/api/email-gateway.ts`
- Create: `apps/esan_gbos/frontend/src/views/EmailInboxView.vue`
- Create: `apps/esan_gbos/frontend/src/views/EmailGatewayAdminView.vue`
- Modify: `apps/esan_gbos/frontend/src/router.ts`
- Modify: `apps/esan_gbos/frontend/src/navigation.ts`
- Create: `apps/esan_gbos/frontend/tests/email-gateway.test.ts`
- Create: `apps/esan_gbos/frontend/tests/v5.test.ts`
- Create: `tests/local_pilot_runtime/test_email_gateway_config.py`
- Create: `tests/local_pilot_runtime/test_email_gateway_api.py`
- Create: `tests/local_pilot_runtime/test_email_gateway_worker.py`
- Extend: `tests/local_pilot_runtime/test_channel_config.py`
- Extend: `tests/local_pilot_runtime/test_pollers.py`
- Create: `tests/infra/test_email_gateway_runtime_composition.py`
- Create: `tests/integration/test_email_gateway_offline_e2e.py`

- [ ] **Step 1: Write preflight-before-side-effect RED tests**

  Missing/invalid manifest, kill switch, emergency stop, database role, authority URL/auth-ref, bearer, or 0400/0600 secret must return 78 before any DB/server/Frappe/provider factory. Reject inline/env secrets and arbitrary URLs. Default switches are `GBOS_EMAIL_GATEWAY_KILL_SWITCH=true`, `GBOS_EMAIL_PUBLICATION_KILL_SWITCH=true`, and `GBOS_EXTERNAL_SEND_ENABLED=false`.

- [ ] **Step 2: Write Phase 1 BFF/PWA RED tests**

  Freeze only these v5 operations: mailbox list/get/upsert/status, safe Inbox list/get, and connector-health read. Add routes `/gbos/email` and `/gbos/email-gateway`. Config view must support multiple primary mailboxes and safe enable/pause/revoke with expected revision; Inbox is read-only in Phase 1. Tests require role/deep-link parity, 375/768/1440 layout, keyboard/axe, no raw participant/body/provider ID, and no API cache.

- [ ] **Step 3: Run runtime/BFF/PWA tests and verify RED**

  Run: `uv run --frozen pytest tests/local_pilot_runtime/test_email_gateway_config.py tests/local_pilot_runtime/test_email_gateway_api.py tests/local_pilot_runtime/test_email_gateway_worker.py tests/local_pilot_runtime/test_channel_config.py tests/local_pilot_runtime/test_pollers.py tests/infra/test_email_gateway_runtime_composition.py tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py -q`

  Then run: `corepack pnpm --dir apps/esan_gbos/frontend exec vitest run tests/email-gateway.test.ts tests/v5.test.ts`.

  Expected: FAIL on missing entrypoints, BFF v5 contract/API, routes, and views.

- [ ] **Step 4: Add a dedicated Gateway role and closed config**

  Add `gbos_email_gateway_app`, logical `postgres_email_gateway_password`, exact local service URLs, and a separate Gateway migration ledger. Replace the single-instance Email mailbox input with a closed revisioned mailbox list whose provider kind is `fake|imap_smtp|wecom_app_mail` and business mode is `primary|selective_archive|migration`; this does not rename or reuse the existing chat-archive `wecom` channel. Translate the legacy IMAP instance only to a disabled `selective_archive`/`migration` mailbox with an explicit cutover publication revision and activation watermark. Do not move or backfill its delivery/checkpoint/quarantine/history, and do not enable it. The Gateway container joins only local-internal, has no controlled-egress in Chunk 1, and receives no provider credential.

- [ ] **Step 5: Implement the two explicit cross-database relays**

  `observer-email-publication-worker` opens only the Observer publisher DB role, claims one Observer publication outbox row, calls Gateway `POST /internal/v1/email-publications/accept` with exact site/purpose/scoped bearer, and marks the Observer row delivered only after the Gateway returns its stable receipt. Gateway API opens only Gateway DB and atomically stores publication receipt/Inbox.

  `mailbox-config-projection-worker` opens only Gateway DB, claims one mailbox config outbox row, calls Observer `POST /internal/v1/email-connectors/apply-config` with a different scoped bearer, and marks the Gateway row delivered only after Observer returns the exact config-projection receipt. Observer API opens only Observer DB. Both relays use fence/generation/heartbeat, stable request IDs, digest replay, bounded retry/dead-letter, and `finally` close. Neither process holds both DB credentials; acknowledgement loss replays safely; no distributed transaction is claimed.

- [ ] **Step 6: Implement the Phase 1 BFF and PWA**

  Bind BFF to exact internal `http://email-gateway-api:8004`, mounted `email_gateway_bff_bearer`, and auth-ref. BFF resolves current Frappe actor/team/roles and delegates exact scope; Gateway authorizes before SQL LIMIT. Admin mailbox writes create Gateway config outbox revisions; connector health is read live through the Observer-owned safe health endpoint. The Inbox view renders only safe summaries/details from fake publications and has no claim/merge/draft/send controls yet.

- [ ] **Step 7: Add content-free metrics and retention**

  Expose only fixed labels such as outcome/state/provider kind; never mailbox address, message ID, participant, mapping ref, or safe error payload. In Chunk 1, retention may expire only unconfirmed display projections when the corresponding Observer evidence expiry/tombstone permits it; active draft refs and all audit/authority receipts remain retained. Task 13 freezes terminal-draft and CRM-lifecycle rules. Observer remains responsible for CAS/legal hold/tombstones.

- [ ] **Step 8: Prove the complete offline chain**

  Use deterministic fake provider → Observer batch/CAS/delivery/publication/checkpoint → Gateway receipt/independent Inbox → Frappe identity/route fakes → suggestion/manual merge. Cover duplicate, crash/restart, 429, quarantine, multiple primary mailboxes, same message at two mailboxes, stale authority, and `external_send=false`. Add a legacy-config migration regression proving old IMAP state is not copied/backfilled/double-written and stays disabled at the exact cutover revision. Assert zero network/provider/model calls.

- [ ] **Step 9: Run Chunk 1 gates**

  Run the exact Step 3 backend and frontend commands and require PASS. Then run `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/domain/test_internal_email_gateway_authority.py tests/domain/test_email_gateway_authority_service.py tests/observer/test_email_publication.py tests/observer/test_email_checkpoint_fence.py tests/email_gateway tests/integration/test_email_gateway_offline_e2e.py -q`; `uv run --frozen ruff check services/email_gateway services/observer/observer services/local_pilot_runtime apps/esan_gbos/esan_gbos tests`; `uv run --frozen ruff format --check services/email_gateway services/observer/observer services/local_pilot_runtime apps/esan_gbos/esan_gbos tests`; `uv run --frozen mypy services/email_gateway services/observer/observer/email_publication.py services/observer/observer/email_checkpoint_fence.py services/local_pilot_runtime`; `uv run --frozen python -m compileall -q apps services scripts tests`; and `git diff --check`. Expected: all GREEN; frontend lint/typecheck/build GREEN; formal preflight remains exit 78 because pilot Go is false.

- [ ] **Step 10: Commit runtime composition**

  ```bash
  git add -- services/local_pilot_runtime/email_gateway_config.py services/local_pilot_runtime/email_gateway_api.py services/local_pilot_runtime/email_gateway_worker.py services/local_pilot_runtime/email_publication_worker.py services/local_pilot_runtime/mailbox_config_projection_worker.py services/email_gateway/api.py services/local_pilot_runtime/runtime_support.py services/local_pilot_runtime/channel_config.py services/local_pilot_runtime/pollers.py contracts/local_pilot/local-pilot-manifest-v1.0.schema.json scripts/local-pilot/migrate scripts/local-pilot/render-config scripts/local-pilot/prepare-secrets infra/local/compose.yml infra/local/runtime-entrypoints.json infra/local/local-pilot-manifest.json contracts/bff-v5.openapi.json apps/esan_gbos/esan_gbos/api/v5/__init__.py apps/esan_gbos/esan_gbos/api/v5/gateway.py apps/esan_gbos/esan_gbos/api/v5/email_inbox.py apps/esan_gbos/esan_gbos/api/v5/email_admin.py apps/esan_gbos/esan_gbos/domain/v5_email_dto.py tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py apps/esan_gbos/frontend/src/api/email-gateway-types.ts apps/esan_gbos/frontend/src/api/email-gateway.ts apps/esan_gbos/frontend/src/views/EmailInboxView.vue apps/esan_gbos/frontend/src/views/EmailGatewayAdminView.vue apps/esan_gbos/frontend/src/router.ts apps/esan_gbos/frontend/src/navigation.ts apps/esan_gbos/frontend/tests/email-gateway.test.ts apps/esan_gbos/frontend/tests/v5.test.ts tests/local_pilot_runtime/test_email_gateway_config.py tests/local_pilot_runtime/test_email_gateway_api.py tests/local_pilot_runtime/test_email_gateway_worker.py tests/local_pilot_runtime/test_channel_config.py tests/local_pilot_runtime/test_pollers.py tests/infra/test_email_gateway_runtime_composition.py tests/integration/test_email_gateway_offline_e2e.py
  git diff --cached --name-only | sort
  git commit -m "feat(local-pilot): compose disabled email gateway core"
  ```

---

## Chunk 2: WeCom application-mail shadow ingress

### Task 6: Freeze the authoritative WeCom application-mail protocol

**Files:**

- Create: `contracts/email_gateway/wecom-app-mail-callback-v1.0.schema.json`
- Create: `contracts/email_gateway/wecom-app-mail-list-v1.0.schema.json`
- Create: `contracts/email_gateway/wecom-app-mail-message-v1.0.schema.json`
- Create: `contracts/email_gateway/wecom-app-mail-token-v1.0.schema.json`
- Create: `contracts/email_gateway/wecom-app-mail-error-v1.0.schema.json`
- Create: `tests/fixtures/wecom_app_mail/official-inbound-fixtures-v1.json` containing all named sanitized callback/token/list/message/error cases
- Extend: `tests/contracts/test_email_gateway_contracts.py`
- Create: `docs/compat/wecom-app-mail-contract.md`

- [ ] **Step 1: Re-read the current official WeCom documentation**

  Record official URLs, access date, application permission, callback verification/encryption, token acquisition, new-mail event, list/pagination, full EML/message fetch, stable IDs, rate limits, revocation/errors, and outbound availability. Do not treat the prior ChatGPT conversation or an API mirror as the final wire authority.

- [ ] **Step 2: Stop fail-closed if authoritative details are unavailable**

  Callback, token, list, message/EML, pagination, limit, stable ID, and every error used by Tasks 7–8 are mandatory. If any is unresolved, Task 6 remains RED and Tasks 7–9 must not start. Optional outbound details may remain unresolved because outbound stays disabled; Task 18 must stop until its send/status contract is official. Do not infer request fields from screenshots or other SDKs.

- [ ] **Step 3: Write sanitized fixture/parser RED tests**

  Cover the one officially confirmed callback serialization and one officially confirmed response serialization; do not leave “XML/JSON as applicable.” Cover valid encrypted callback, duplicate/replayed callback, receive event with count only, token response, paginated list, full message/EML response, empty page, 429, token expiry, revocation, every mapped provider error, unknown fields, oversized values, and malformed encoding.

- [ ] **Step 4: Run the contract test and verify RED before implementation**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py -q`

  Expected before adding schemas/fixtures: FAIL on missing WeCom callback/token/list/message/error schemas and provenance completeness assertions.

- [ ] **Step 5: Add closed contracts and provenance note**

  Pin every field actually used. State explicitly that callback is a wake signal, not a delivery/cursor; only pulled stable message IDs become Observer deliveries.

- [ ] **Step 6: Verify GREEN and commit contract freeze**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py -q`

  ```bash
  git add -- contracts/email_gateway/wecom-app-mail-callback-v1.0.schema.json contracts/email_gateway/wecom-app-mail-list-v1.0.schema.json contracts/email_gateway/wecom-app-mail-message-v1.0.schema.json contracts/email_gateway/wecom-app-mail-token-v1.0.schema.json contracts/email_gateway/wecom-app-mail-error-v1.0.schema.json tests/fixtures/wecom_app_mail/official-inbound-fixtures-v1.json tests/contracts/test_email_gateway_contracts.py docs/compat/wecom-app-mail-contract.md
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): freeze WeCom application mail contract"
  ```

### Task 7: Implement callback signals with separate secrets and replay protection

**Files:**

- Create: `services/observer/migrations/015_wecom_app_mail_signals.sql`
- Create: `services/observer/observer/connectors/wecom_app_mail_callback.py`
- Create: `services/observer/observer/email_signal_queue.py`
- Modify: `services/observer/observer/local_pilot_api.py`
- Create: `services/local_pilot_runtime/wecom_app_mail_webhook.py`
- Create: `tests/observer/test_wecom_app_mail_callback.py`
- Create: `tests/local_pilot_runtime/test_wecom_app_mail_webhook.py`
- Create: `tests/infra/test_wecom_app_mail_runtime.py`
- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `scripts/local-pilot/render-config`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`

- [ ] **Step 1: Write callback crypto/replay RED tests**

  Verify URL challenge and callback signature/decryption against frozen fixtures; reject wrong corp/app binding, timestamp window, nonce replay, duplicate body drift, oversized input, unknown event, invalid padding/XML/JSON, and unsafe errors. A valid duplicate returns the original signal receipt.

- [ ] **Step 2: Write least-secret composition RED tests**

  The callback runtime receives `wecom_app_mail_callback_token`, `wecom_app_mail_callback_aes_key`, and a separate `observer_email_signal_bearer`; it receives no DB credential. It must not receive app secret, DB password, model key, SMTP credential, or external-send capability.

- [ ] **Step 3: Run callback tests and verify RED**

  Run: `uv run --frozen pytest tests/observer/test_wecom_app_mail_callback.py tests/local_pilot_runtime/test_wecom_app_mail_webhook.py tests/infra/test_wecom_app_mail_runtime.py -q`

  Expected: FAIL on missing migration, callback parser, signal queue/API, and webhook runtime.

- [ ] **Step 4: Add Observer-owned signal storage and closed ingest API**

  Migration stores callback receipt, digest, timestamp/nonce replay fence, mailbox connector ref, new-mail count hint, activation watermark/config revision, lease/ack/dead-letter, and reconciliation wake signal under FORCE RLS. It stores no message, raw callback plaintext, Gateway workflow state, or provider cursor. `POST /internal/v1/email-signals/accept` validates the scoped signal bearer/site/purpose and writes through the Observer app transaction; callback runtime has no DB access.

- [ ] **Step 5: Implement default-off webhook runtime**

  Require exact public callback path behind the approved webhook-tunnel boundary, body/content-type/size, manifest revision, kill switch, and mounted secrets before opening the server. It verifies/decrypts then calls only the closed Observer signal-ingest API; it never opens PostgreSQL or pulls messages inline.

- [ ] **Step 6: Run GREEN and commit callback path**

  Run the exact Step 3 command and `scripts/dev/test-email-gateway-postgres --all`. Expected: no external network; all tests and migration-twice PASS; malformed callbacks have stable safe codes and no secret/plaintext leakage.

  ```bash
  git add -- services/observer/migrations/015_wecom_app_mail_signals.sql services/observer/observer/connectors/wecom_app_mail_callback.py services/observer/observer/email_signal_queue.py services/observer/observer/local_pilot_api.py services/local_pilot_runtime/wecom_app_mail_webhook.py tests/observer/test_wecom_app_mail_callback.py tests/local_pilot_runtime/test_wecom_app_mail_webhook.py tests/infra/test_wecom_app_mail_runtime.py scripts/local-pilot/prepare-secrets scripts/local-pilot/render-config infra/local/compose.yml infra/local/runtime-entrypoints.json
  git diff --cached --name-only | sort
  git commit -m "feat(observer): accept governed WeCom mail signals"
  ```

### Task 8: Implement pull, token, pagination, and reconciliation through Observer

**Files:**

- Create: `services/observer/observer/connectors/wecom_app_mail.py`
- Create: `services/local_pilot_runtime/wecom_app_mail_poller.py`
- Create: `services/local_pilot_runtime/wecom_app_mail_reconciler.py`
- Create: `tests/observer/test_wecom_app_mail.py`
- Create: `tests/local_pilot_runtime/test_wecom_app_mail_poller.py`
- Create: `tests/local_pilot_runtime/test_wecom_app_mail_reconciler.py`
- Extend: `tests/infra/test_wecom_app_mail_runtime.py`

- [ ] **Step 1: Write injected-transport RED tests**

  Cover token cache/expiry, request binding, pagination, out-of-order pages, duplicate stable IDs, 429 Retry-After, bounded 5xx retry, timeout, token invalidation, revocation pause, malformed response, oversized EML, and exact safe-error mapping. `repr`/exceptions must not contain token, corp/app ID, mailbox address, raw EML, or provider payload.

- [ ] **Step 2: Write signal lease and activation-boundary RED tests**

  Prove the poller claims one durable signal with worker/generation/fence, acknowledges only after its entire pull batch reaches publication/quarantine and checkpoint finalization, and safely reclaims after crash/lease expiry. Callback count is only a hint. Bind `activation_watermark` to mailbox/config revision; the effective lower bound is always `max(activation_watermark, bounded_overlap_start)`. Reject exact pre-watermark candidates before CAS/delivery; accept exact-boundary candidates; preserve the fence across restart, stale config, callback wake, and periodic reconciliation.

- [ ] **Step 3: Run focused tests and verify RED**

  Run: `uv run --frozen pytest tests/observer/test_wecom_app_mail.py tests/local_pilot_runtime/test_wecom_app_mail_poller.py tests/local_pilot_runtime/test_wecom_app_mail_reconciler.py tests/infra/test_wecom_app_mail_runtime.py -q`

  Expected: FAIL on missing provider/poller/reconciler and signal-claim/watermark behavior.

- [ ] **Step 4: Implement the provider-neutral pull protocol**

  `WeComAppMailProvider` returns `ProviderEmailCandidate(provider_message_id, received_at, raw_eml, cursor_candidate)` to Observer. Only the Observer batch fence can persist/advance the cursor. Provider code receives no Gateway repository or business authority.

- [ ] **Step 5: Implement durable signal claim/ack and periodic reconciliation**

  Poller uses the Observer app DB role to claim/heartbeat/ack the durable signal; it also owns the CAS mount and common intake transaction. It receives no Gateway DB credential. Ack occurs only after the Observer batch is terminal and checkpoint finalizer succeeds; a crash leaves a reclaimable signal. Reconciler creates the same signal type on schedule and polls from `max(activation_watermark, bounded_overlap_start)`. Lost callbacks must still be recovered; callback count does not determine completion.

- [ ] **Step 6: Enforce separate puller credential scope**

  Puller receives only `wecom_app_mail_app_secret` and non-secret corp/app/mailbox config. It does not receive callback AES/token, SMTP/outbound credential, identity HMAC, or model key. Load through `MountedFileSecretProvider` after all non-secret preflight checks.

- [ ] **Step 7: Route every result through the common Observer fence**

  Full EML enters the same CAS → delivery/job → normalization/publication → checkpoint finalizer built in Chunk 1. Malformed/oversized messages become Observer quarantine; token/provider errors do not advance cursor.

- [ ] **Step 8: Run GREEN and commit pull/reconcile path**

  Run: `uv run --frozen pytest tests/observer/test_wecom_app_mail.py tests/local_pilot_runtime/test_wecom_app_mail_poller.py tests/local_pilot_runtime/test_wecom_app_mail_reconciler.py tests/infra/test_wecom_app_mail_runtime.py -q`

  ```bash
  git add -- services/observer/observer/connectors/wecom_app_mail.py services/local_pilot_runtime/wecom_app_mail_poller.py services/local_pilot_runtime/wecom_app_mail_reconciler.py tests/observer/test_wecom_app_mail.py tests/local_pilot_runtime/test_wecom_app_mail_poller.py tests/local_pilot_runtime/test_wecom_app_mail_reconciler.py tests/infra/test_wecom_app_mail_runtime.py
  git diff --cached --name-only | sort
  git commit -m "feat(local-pilot): pull WeCom mail through Observer"
  ```

### Task 9: Prove shadow ingress and keep outbound closed

**Files:**

- Create: `tests/integration/test_wecom_app_mail_shadow_offline_e2e.py`
- Extend: `tests/integration/test_email_gateway_offline_e2e.py`
- Extend: `tests/infra/test_email_gateway_runtime_composition.py`
- Extend: `tests/infra/test_wecom_app_mail_runtime.py`
- Modify: `infra/local/local-pilot-manifest.json`
- Modify: `infra/local/runtime-entrypoints.json`
- Modify: `docs/local-pilot/RUNBOOK.md`
- Modify: `docs/local-pilot/SAFETY_ASSERTIONS.md`

- [ ] **Step 1: Add an offline component-chain RED**

  Frozen callback fixture wakes puller; fake WeCom transport returns pages/EML; Observer persists CAS/delivery/publication and advances checkpoint once; Gateway creates one mailbox-scoped Inbox Item. Replay callback/pages/restart must not duplicate. Assert no history before activation watermark and no outbound/model calls.

- [ ] **Step 2: Run the component chain and verify RED**

  Run: `uv run --frozen pytest tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/infra/test_email_gateway_runtime_composition.py tests/infra/test_wecom_app_mail_runtime.py -q`

  Expected: FAIL on missing shadow composition, fixture chain, and disabled-provider assertions.

- [ ] **Step 3: Add shadow-mode composition assertions**

  `wecom_app_mail` is distinct from `wecom`; callback and puller have separate secrets; only puller has controlled egress; Gateway remains local-internal; `outbound_enabled=false`; no Send Outbox worker/sender runs.

- [ ] **Step 4: Document the shadow Go/No-Go procedure**

  The real gate, executed only with explicit later authorization, requires one designated test email, exactly one new Inbox Item, exact EML digest, no history backfill, duplicate callback replay safety, and no outbound. Unit/offline tests are not this proof.

- [ ] **Step 5: Run Chunk 2 gates**

  ```bash
  uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/observer/test_wecom_app_mail_callback.py tests/observer/test_wecom_app_mail.py tests/local_pilot_runtime/test_wecom_app_mail_webhook.py tests/local_pilot_runtime/test_wecom_app_mail_poller.py tests/local_pilot_runtime/test_wecom_app_mail_reconciler.py tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/infra/test_email_gateway_runtime_composition.py tests/infra/test_wecom_app_mail_runtime.py -q
  uv run --frozen pytest tests/observer tests/email_gateway tests/local_pilot_runtime tests/infra/test_email_gateway_runtime_composition.py tests/infra/test_wecom_app_mail_runtime.py -q
  scripts/dev/test-email-gateway-postgres --all
  uv run --frozen ruff check services/observer/observer services/email_gateway services/local_pilot_runtime tests/observer tests/email_gateway tests/local_pilot_runtime tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/infra/test_wecom_app_mail_runtime.py
  uv run --frozen ruff format --check services/observer/observer services/email_gateway services/local_pilot_runtime tests/observer tests/email_gateway tests/local_pilot_runtime tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/infra/test_wecom_app_mail_runtime.py
  uv run --frozen mypy services/observer/observer services/email_gateway services/local_pilot_runtime
  uv run --frozen python -m compileall -q apps services scripts tests
  scripts/dev/secret-scan
  git diff --check
  ```

  Expected: every command exits 0 without provider network or credentials; migrations apply twice and the disposable database is removed; the frozen EML fixture's exact CAS SHA-256 matches; 429/revocation/malformed-message tests pause only their mailbox.

- [ ] **Step 6: Commit shadow readiness**

  ```bash
  git add -- tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/integration/test_email_gateway_offline_e2e.py tests/infra/test_email_gateway_runtime_composition.py tests/infra/test_wecom_app_mail_runtime.py infra/local/local-pilot-manifest.json infra/local/runtime-entrypoints.json docs/local-pilot/RUNBOOK.md docs/local-pilot/SAFETY_ASSERTIONS.md
  git diff --cached --name-only | sort
  git commit -m "test(email-gateway): prove disabled WeCom shadow chain"
  ```

---

## Chunk 3: Human-operated CRM Inbox

### Task 10: Implement the Inbox workflow, SLA, and human-only conversation changes

**Files:**

- Create: `services/email_gateway/operations.py`
- Create: `services/email_gateway/sla.py`
- Extend: `services/email_gateway/conversations.py`
- Extend: `services/email_gateway/routing.py`
- Create: `services/email_gateway/migrations/005_email_gateway_human_operations.sql`
- Create: `contracts/email_gateway/mailbox-sla-policy-v1.0.schema.json`
- Extend: `tests/contracts/test_email_gateway_contracts.py`
- Create: `tests/email_gateway/test_operations.py`
- Create: `tests/email_gateway/test_sla.py`
- Extend: `tests/email_gateway/test_routing.py`
- Extend: `tests/email_gateway/test_conversations.py`
- Create: `tests/email_gateway/test_human_operations_migration.py`

- [ ] **Step 1: Write the state-machine and RBAC RED matrix**

  Freeze exact ownership: Gateway publication creates `identity_pending` or `unassigned`; identity/routing worker may move `identity_pending → unassigned|assigned`; Sales User may claim `unassigned → assigned`, save `assigned ↔ draft`, and request `assigned|draft → waiting_internal|converted|closed`; Sales Manager/Reviewer may reassign and reopen `closed → unassigned|assigned`; only Sales Manager/Reviewer may merge/split. `send_queued`, `send_uncertain`, and `waiting_customer` are reserved for the Chunk 4 approved-command worker. `quarantined` is a read-only Observer projection. AI, Sales User, BFF, and ordinary Gateway roles cannot confirm identity, merge/split, enter outbound states, or create Send Outbox.

  Reject stale revision, cross-team actor, disabled user, impossible transition, duplicate request drift, and modification of quarantined evidence.

- [ ] **Step 2: Write claim/reassign and SLA-contract RED tests**

  Claim requires same-team eligible actor and expected revision. Reassign requires Sales Manager/Reviewer. Freeze `mailbox-sla-policy-v1.0` with policy ref/revision, first-response duration seconds (60–604800), effective time, and no pause mechanism in v1. SLA starts at Observer `received_at`, completes only on the first provider-accepted outbound receipt in Chunk 4, and is `not_applicable` for Observer quarantine. Assignment/reroute/draft does not reset due time; close snapshots the outcome; reopen retains original start/due time and appends audit. Reject clock regression and policy revision drift.

- [ ] **Step 3: Write human merge/split RED tests**

  Suggestions never mutate. Accept requires same-team authorized actor plus exact source Inbox/Conversation revisions. Cross-team merge, already-consumed source, stale suggestion, provider-thread auto-accept, or AI actor fails. Split creates a new Conversation revision and preserves source message/inbox/audit refs.

- [ ] **Step 4: Run focused tests and verify RED**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/email_gateway/test_operations.py tests/email_gateway/test_sla.py tests/email_gateway/test_routing.py tests/email_gateway/test_conversations.py tests/email_gateway/test_human_operations_migration.py -q`

  Expected: FAIL on missing SLA schema, migration 005, state/RBAC matrix, and operation implementation.

- [ ] **Step 5: Implement operations with idempotency and authorization-before-LIMIT**

  Repository methods accept an already-resolved `GatewayActorScope(site_id, team_refs, roles, actor_ref)`. List queries apply site/team/actor predicates in SQL before ORDER/LIMIT. Commands require `request_id`, idempotency key, expected revision, and closed payload digest.

- [ ] **Step 6: Implement business links without automatic CRM mutation**

  Link existing Party/Contact/CRM Lead/CRM Deal refs only after closed Frappe authority validation; in this Frappe CRM version, CRM Deal is the local Opportunity object. Creating formal CRM objects remains a separate existing human command; Gateway cannot auto-create them from email/AI.

- [ ] **Step 7: Run GREEN and migration upgrade**

  Run the exact Step 4 command. Then run `scripts/dev/test-email-gateway-postgres --all`. Expected: upgrade from Chunk 2 schema applies migration 005 once, second runner invocation is a no-op, all state/RLS/grant tests PASS, and the disposable database is removed.

- [ ] **Step 8: Commit workflow core**

  ```bash
  git add -- services/email_gateway/operations.py services/email_gateway/sla.py services/email_gateway/conversations.py services/email_gateway/routing.py services/email_gateway/migrations/005_email_gateway_human_operations.sql contracts/email_gateway/mailbox-sla-policy-v1.0.schema.json tests/contracts/test_email_gateway_contracts.py tests/email_gateway/test_operations.py tests/email_gateway/test_sla.py tests/email_gateway/test_routing.py tests/email_gateway/test_conversations.py tests/email_gateway/test_human_operations_migration.py
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): add human inbox workflow"
  ```

### Task 11: Expose a closed internal Gateway API and BFF v5

**Files:**

- Modify: `services/email_gateway/api.py`
- Create: `services/email_gateway/security.py`
- Create: `services/email_gateway/evidence.py`
- Create: `tests/email_gateway/test_api.py`
- Modify: `contracts/bff-v5.openapi.json`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/gateway.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/email_inbox.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/email_admin.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/v5_email_dto.py`
- Modify: `tests/contracts/test_bff_v5_openapi.py`
- Modify: `tests/domain/test_bff_v5_email.py`
- Modify: `tests/domain/test_bff_v5_runtime.py`
- Modify: `services/observer/observer/local_pilot_api.py`
- Modify: `services/observer/observer/read_service.py`
- Create: `services/observer/observer/email_draft_material.py`
- Extend: `tests/observer/test_local_pilot_api.py`
- Extend: `tests/observer/test_local_pilot_read.py`
- Create: `tests/observer/test_email_draft_material.py`
- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `scripts/local-pilot/render-config`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`
- Extend: `tests/infra/test_email_gateway_runtime_composition.py`

- [ ] **Step 1: Write internal API, BFF, and restricted-reveal RED tests**

  Cover exact bearer/auth-ref/site/purpose/audience, team scope, role, method/path, content type/size, CSRF delegation, request/idempotency/revision, no-store responses, and authorization-before-pagination. Reject arbitrary filter/sort/fields, raw SQL/DocType, unknown keys, external URLs, proxy headers, and sensitive error/repr content. Ordinary detail contains safe labels/projections only.

  Explicit reveal requires a fresh actor/team/inbox authorization, exact EvidenceRef bound to that Inbox, and a second internal Observer call. It returns `Cache-Control: no-store`, uses no protected query string, bypasses service-worker storage, clears browser state on 403/navigation, and never copies revealed content into Gateway storage/audit/error.

  Draft-material tests require provider-neutral Observer CAS endpoints that accept bounded UTF-8 draft content only with a fresh Gateway draft-authorization receipt and return only EvidenceRef/digest/revision. A separate finalize operation builds canonical MIME from the exact draft EvidenceRef plus authorized opaque participant roles, persists it to CAS, and returns only final MIME EvidenceRef/digest/binding. Reject stale authorization, raw address fields supplied by the browser, mismatched roles, digest/revision drift, cross-site/purpose, oversized/invalid content, replay drift, and arbitrary evidence browsing.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `uv run --frozen pytest tests/email_gateway/test_api.py tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py tests/observer/test_local_pilot_api.py tests/observer/test_local_pilot_read.py tests/observer/test_email_draft_material.py tests/infra/test_email_gateway_runtime_composition.py -q`

  Expected: FAIL on missing operation endpoints, exact v5 paths, evidence integration, and authorization-before-LIMIT assertions.

- [ ] **Step 3: Implement the exact API table**

  Freeze exactly 17 BFF operations:

  | Module | Method | Operation |
  | --- | --- | --- |
  | `email_inbox` | GET | `list`, `get` |
  | `email_inbox` | POST | `claim`, `reassign`, `transition`, `merge`, `split`, `link_business`, `save_draft`, `reveal` |
  | `email_admin` | GET | `list_mailboxes`, `get_mailbox`, `list_rules`, `connector_health` |
  | `email_admin` | POST | `upsert_mailbox`, `set_mailbox_status`, `upsert_rule` |

  List page size is 1–50; cursors are opaque; allowed Inbox sorts are exactly `received_at_desc` and `sla_due_at_asc`; filters are the closed queue-state enum plus mailbox protected ref. Gateway performs site/team/actor authorization before SQL LIMIT. BFF resolves/delegates current Frappe scope. Mailbox changes create revisioned Gateway configuration publications. `connector_health` reads through to Observer; Gateway never owns cursor/health. Identity confirmation links to the existing role-specific Frappe Review Case flow and does not add an identity write endpoint. `save_draft` first obtains a fresh Gateway draft-authorization receipt, then BFF calls Observer draft-material storage and persists only returned EvidenceRef/digest/revision through the Gateway command. No endpoint creates Send Outbox in Chunk 3.

- [ ] **Step 4: Implement one exact local transport**

  Accept only TCP `http://email-gateway-api:8004` on Compose `local-internal`, mounted `/run/secrets/email_gateway_bff_bearer`, and exact auth-ref. UDS is not part of this contract. Reject credentials/userinfo/query/alternate host/port, inline production token, symlink/wrong mode/oversize token, and config conflicts before Frappe/Gateway calls.

  `services/email_gateway/evidence.py` calls exact Observer `POST /internal/v1/bff/evidence/reveal` after Gateway authorization. Observer rechecks site/purpose/EvidenceRef scope through existing Restricted read policy and returns bounded content no-store; it never trusts a raw evidence locator supplied by the browser.

  `services/observer/observer/email_draft_material.py` owns exact `POST /internal/v1/bff/email-draft-material/save` and `POST /internal/v1/bff/email-draft-material/finalize`. Both require the separate mounted `observer_email_draft_material_bearer`, a fresh Gateway authorization receipt, exact site/purpose/Inbox/draft revision, bounded body, and closed idempotency digest. Save writes CAS content and returns only draft EvidenceRef/digest; finalize resolves addresses only from already-authorized Observer evidence/config, constructs deterministic MIME, writes CAS, and returns only final MIME EvidenceRef/digest plus opaque-role binding. Gateway and Frappe never persist the bytes or raw external addresses. Compose mounts this bearer only into BFF and Observer API; it is absent from Gateway workers and provider adapters.

- [ ] **Step 5: Implement BFF role/team delegation**

  GBOS/Integration Admin access config; Sales Manager/Reviewer access same-team supervision/mapping review; Sales User accesses assigned/team-permitted inbox operations; CEO reads governed all-team projection but does not reveal raw evidence or bypass commands. BFF passes exact delegated actor scope, never its ambient administrator scope.

- [ ] **Step 6: Run GREEN and commit APIs**

  Run the exact Step 2 command. Expected: all named tests PASS and OpenAPI has exactly the 17 operations above.

  ```bash
  git add -- services/email_gateway/api.py services/email_gateway/security.py services/email_gateway/evidence.py tests/email_gateway/test_api.py contracts/bff-v5.openapi.json apps/esan_gbos/esan_gbos/api/v5/gateway.py apps/esan_gbos/esan_gbos/api/v5/email_inbox.py apps/esan_gbos/esan_gbos/api/v5/email_admin.py apps/esan_gbos/esan_gbos/domain/v5_email_dto.py tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py services/observer/observer/local_pilot_api.py services/observer/observer/read_service.py services/observer/observer/email_draft_material.py tests/observer/test_local_pilot_api.py tests/observer/test_local_pilot_read.py tests/observer/test_email_draft_material.py scripts/local-pilot/prepare-secrets scripts/local-pilot/render-config infra/local/compose.yml infra/local/runtime-entrypoints.json tests/infra/test_email_gateway_runtime_composition.py
  git diff --cached --name-only | sort
  git commit -m "feat(bff): expose governed email inbox v5"
  ```

### Task 12: Build the separate Email Inbox and Gateway Admin PWA

**Files:**

- Modify: `apps/esan_gbos/frontend/src/api/email-gateway-types.ts`
- Modify: `apps/esan_gbos/frontend/src/api/email-gateway.ts`
- Modify: `apps/esan_gbos/frontend/src/views/EmailInboxView.vue`
- Create: `apps/esan_gbos/frontend/src/views/EmailInboxDetailView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/EmailGatewayAdminView.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/InboxQueueTabs.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/InboxItemList.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/InboxAssignmentPanel.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/IdentityProjectionPanel.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/ThreadSuggestionPanel.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/ConversationTimeline.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/BusinessLinkPanel.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/ReplyDraftEditor.vue`
- Modify: `apps/esan_gbos/frontend/src/router.ts`
- Modify: `apps/esan_gbos/frontend/src/navigation.ts`
- Modify: `apps/esan_gbos/frontend/src/service-worker.ts`
- Modify: `apps/esan_gbos/frontend/tests/email-gateway.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/v5.test.ts`
- Extend: `apps/esan_gbos/frontend/e2e/gbos.spec.ts`

- [ ] **Step 1: Write strict client/parser RED tests**

  Validate every v5 response before rendering. Reject unknown keys/enums, raw email/phone/provider IDs, unbounded labels, mismatched site/team/revision, duplicate refs, and malformed pagination. A 409 clears stale command state and reloads bounded detail; 403 clears protected detail.

- [ ] **Step 2: Write view RED tests from approved information architecture**

  Prove distinct display for receiving mailbox, channel-account owner, participant identity state, customer Party/Contact, and current business assignee. Add queues: all, identity pending, unassigned, first reply due, draft, send failure/uncertain, waiting customer/internal, converted, closed, quarantine.

- [ ] **Step 3: Run focused UI tests and verify RED**

  Run: `corepack pnpm --dir apps/esan_gbos/frontend exec vitest run tests/email-gateway.test.ts tests/v5.test.ts`

  Expected: FAIL on missing detail route/components, operation client methods, draft editor, privacy, and accessibility assertions.

- [ ] **Step 4: Implement API types/client and ResourceBoundary states**

  Use no persistent business cache, no `localStorage`/IndexedDB for API data, no query strings containing protected refs, and no generic JSON dump. Authorized raw evidence remains behind the existing explicit reveal mechanism and is hidden by default.

- [ ] **Step 5: Implement separate routes and role navigation**

  Add `/gbos/email`, `/gbos/email/:inboxItemRef`, and `/gbos/email-gateway`. Keep `/gbos/communications` unchanged. Deep-link permissions must match the navigation matrix, including standalone Integration Admin access to config without granting ordinary business inbox data.

- [ ] **Step 6: Implement operator and admin components**

  Components include queue tabs, Inbox list, assignment/SLA panel, identity projection/review link, thread suggestion panel with explicit accept/reject, conversation timeline, business links, create/edit-only Reply Draft editor, mailbox registry, Observer-read-through health/cursor/backlog status, rules, pause/revoke, and audit. Draft UI cannot submit approval or enter an outbound state; no send control exists yet.

- [ ] **Step 7: Add service-worker privacy assertions**

  Gateway/BFF responses with `no-store` are never cached. Offline mode fails closed and shows unavailable state; it must not show stale protected content or allow queued mutations.

- [ ] **Step 8: Run frontend acceptance**

  Run the exact Step 3 command, then `corepack pnpm --dir apps/esan_gbos/frontend run lint`, `typecheck`, `test:unit`, `build`, and `test:e2e`. Expected: all PASS. Cover 375/768/1440, real 200% zoom overflow, keyboard order, axe, restored focus after 403/409/dialog close, live error/status announcements, tab/list semantics, non-color-only SLA status, duplicate/rejection, route permissions, and whole-DOM/URL scan for raw/protected identifiers.

- [ ] **Step 9: Commit PWA**

  ```bash
  git add -- apps/esan_gbos/frontend/src/api/email-gateway-types.ts apps/esan_gbos/frontend/src/api/email-gateway.ts apps/esan_gbos/frontend/src/views/EmailInboxView.vue apps/esan_gbos/frontend/src/views/EmailInboxDetailView.vue apps/esan_gbos/frontend/src/views/EmailGatewayAdminView.vue apps/esan_gbos/frontend/src/components/email/InboxQueueTabs.vue apps/esan_gbos/frontend/src/components/email/InboxItemList.vue apps/esan_gbos/frontend/src/components/email/InboxAssignmentPanel.vue apps/esan_gbos/frontend/src/components/email/IdentityProjectionPanel.vue apps/esan_gbos/frontend/src/components/email/ThreadSuggestionPanel.vue apps/esan_gbos/frontend/src/components/email/ConversationTimeline.vue apps/esan_gbos/frontend/src/components/email/BusinessLinkPanel.vue apps/esan_gbos/frontend/src/components/email/ReplyDraftEditor.vue apps/esan_gbos/frontend/src/router.ts apps/esan_gbos/frontend/src/navigation.ts apps/esan_gbos/frontend/src/service-worker.ts apps/esan_gbos/frontend/tests/email-gateway.test.ts apps/esan_gbos/frontend/tests/v5.test.ts apps/esan_gbos/frontend/e2e/gbos.spec.ts
  git diff --cached --name-only | sort
  git commit -m "feat(pwa): add governed CRM email inbox"
  ```

### Task 13: Close human-operations retention, metrics, and phase evidence

**Files:**

- Extend: `services/email_gateway/retention.py`
- Extend: `services/email_gateway/metrics.py`
- Create: `services/email_gateway/migrations/006_email_gateway_human_retention.sql`
- Extend: `tests/email_gateway/test_retention.py`
- Extend: `tests/email_gateway/test_metrics.py`
- Create: `tests/integration/test_email_gateway_human_operations_offline_e2e.py`
- Modify: `infra/local/prometheus/alerts.yml`
- Extend: `tests/infra/test_email_gateway_runtime_composition.py`
- Modify: `docs/local-pilot/RUNBOOK.md`

- [ ] **Step 1: Write retention RED tests**

  Raw EML/body/attachment/final MIME remains Observer CAS responsibility. Active drafts remain available while their Inbox/Conversation is active. Sent or explicitly discarded terminal draft content references expire 30 days after terminal time. Unconfirmed display/subject projections expire with their Observer raw-evidence expiry. Confirmed CRM metadata, mapping/authority revision receipts, Conversation, assignments, SLA, business links, content digests, provider receipts, and audit follow CRM lifecycle. Legal hold and missing Observer tombstone/expiry receipt block removal.

- [ ] **Step 2: Write low-cardinality metric/readiness RED tests**

  Freeze these metrics:

  - `gbos_email_gateway_publication_backlog{state}` gauge, `state ∈ {queued,retry,leased,dead_letter}`;
  - `gbos_email_gateway_publication_oldest_age_seconds{state}` gauge over the same fixed state set, zero when no row exists;
  - `gbos_email_gateway_inbox_items{queue_state}` gauge over the closed Inbox enum;
  - `gbos_email_gateway_sla_overdue` gauge with no labels;
  - `gbos_email_gateway_identity_pending` and `gbos_email_gateway_unassigned` gauges with no labels;
  - `gbos_email_gateway_authority_failures_total{safe_reason_code}` counter over a fixed allowlist;
  - `gbos_email_gateway_worker_heartbeat_age_seconds{worker_kind}` gauge over fixed worker kinds;
  - `gbos_email_gateway_dead_letter_total{work_kind}` counter over fixed work kinds.

  Reject mailbox/address/message/participant/identity/Party/User/provider-payload/error-payload labels. Readiness requires a persisted heartbeat age ≤30 seconds. Alerts are exact: heartbeat >30 seconds for 2 minutes; dead letter increase >0 for 5 minutes; `max(gbos_email_gateway_publication_oldest_age_seconds{state=~"queued|retry"}) > 300` for 10 minutes; SLA overdue >0 for 15 minutes.

- [ ] **Step 3: Run retention/metric tests and verify RED**

  Run: `uv run --frozen pytest tests/email_gateway/test_retention.py tests/email_gateway/test_metrics.py tests/infra/test_email_gateway_runtime_composition.py -q`

  Expected: FAIL on missing migration 006, state-aware retention, metric names/labels, readiness, and four alert rules.

- [ ] **Step 4: Implement scheduled, audited retention**

  Reuse fenced lease/idempotency/receipt/emergency-stop patterns. Dry-run is read-only; execute is bounded and serial. Failures emit safe metrics and retry next interval; no CAS delete occurs without Observer durable tombstone workflow.

- [ ] **Step 5: Prove human Inbox component E2E**

  Fake provider mail → two independent mailbox Inbox items → human Party mapping review projection → exact route → claim/reassign/SLA → suggestion reject then manual merge → business link → draft edit. Restart and replay preserve one audit trail. Assert no provider/model/network call, no outbound-state transition, no Send Outbox insert, every outbound switch closed, and real identity/routing/provider gates documented but unexecuted.

- [ ] **Step 6: Run Chunk 3 gates GREEN**

  ```bash
  uv run --frozen pytest tests/email_gateway/test_retention.py tests/email_gateway/test_metrics.py tests/infra/test_email_gateway_runtime_composition.py -q
  uv run --frozen pytest tests/email_gateway tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/observer/test_local_pilot_api.py tests/observer/test_email_draft_material.py tests/integration/test_email_gateway_human_operations_offline_e2e.py -q
  scripts/dev/test-email-gateway-postgres --all
  corepack pnpm --dir apps/esan_gbos/frontend run lint
  corepack pnpm --dir apps/esan_gbos/frontend run typecheck
  corepack pnpm --dir apps/esan_gbos/frontend run test:unit
  corepack pnpm --dir apps/esan_gbos/frontend run build
  corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
  uv run --frozen ruff check services/email_gateway services/observer/observer apps/esan_gbos/esan_gbos tests/email_gateway tests/observer tests/domain
  uv run --frozen ruff format --check services/email_gateway services/observer/observer apps/esan_gbos/esan_gbos tests/email_gateway tests/observer tests/domain
  uv run --frozen mypy services/email_gateway services/observer/observer apps/esan_gbos/esan_gbos/domain
  uv run --frozen python -m compileall -q apps services scripts tests
  scripts/dev/secret-scan
  git diff --check
  ```

  Expected: every command exits 0; migration 006 applies once and the second runner is a no-op; the disposable database is removed; all frontend gates pass; alerts contain exactly the frozen rules; all external-send switches remain closed.

- [ ] **Step 7: Commit human-operations closure**

  ```bash
  git add -- services/email_gateway/retention.py services/email_gateway/metrics.py services/email_gateway/migrations/006_email_gateway_human_retention.sql tests/email_gateway/test_retention.py tests/email_gateway/test_metrics.py tests/integration/test_email_gateway_human_operations_offline_e2e.py infra/local/prometheus/alerts.yml tests/infra/test_email_gateway_runtime_composition.py docs/local-pilot/RUNBOOK.md
  git diff --cached --name-only | sort
  git commit -m "test(email-gateway): close human inbox operations"
  ```

---

## Chunk 4: Revision-pinned approved outbound

### Task 14: Freeze governance and the Email Send ApprovedCommand contract

**Files:**

- Modify: `docs/adr/ADR-0004-ai-drafts-and-human-commands.md`
- Modify: `docs/permission-matrix.md`
- Create: `contracts/email_gateway/email-send-approved-command-v2.0.schema.json`
- Create: `contracts/email_gateway/examples/email-send-approved-command-v2.json`
- Modify: `contracts/gate2/contract-evolution-matrix.json`
- Modify: `contracts/README.md`
- Create: `tests/contracts/test_email_send_approved_command.py`
- Create: `tests/governance/test_email_send_governance.py`

- [ ] **Step 1: Write governance RED tests while outbound remains disabled**

  Require documentation/permission statements for delegated current-owner approval, no general Sales User approval, no direct PWA/outbox write, Frappe durable command publication, Gateway idempotent consumption, lack of cross-DB ACID, uncertainty/reconciliation, separate executor/worker roles, and emergency stop. Assert manifests still have external send false.

- [ ] **Step 2: Write the closed v2 command RED matrix**

  Command must freeze site/purpose/team, actor and delegated approver, Review Case/policy/expiry, mailbox config revision, Inbox/Conversation/draft revisions, opaque role-tagged sender/recipient envelope, every recipient mapping ref/revision, Party/team/owner authority revisions, final MIME EvidenceRef/digest, evidence refs, request/idempotency/stable client request IDs, and payload hash. Reject extra fields, stale/zero revisions, duplicate recipients/evidence, every raw external address, actor mismatch, expired command, and digest drift.

- [ ] **Step 3: Run governance/contract tests and verify RED**

  Run: `uv run --frozen pytest tests/contracts/test_email_send_approved_command.py tests/governance/test_email_send_governance.py -q`

  Expected: FAIL on missing v2 schema/evolution entry and outdated ADR/permission statements.

- [ ] **Step 4: Update ADR and permission matrix**

  Specify two durable transactions: Frappe decision + ApprovedCommand + command publication; Gateway command receipt + Send Outbox. Define Action Guard pre-execution check, live authority recheck, append-only attempts, provider states, and no blind retry. Add service roles without System Manager or broad DocPerm.

- [ ] **Step 5: Add schema/examples and evolution matrix**

  Preserve `contracts/approved-command.schema.json` v1 compatibility. The v2 Email contract is purpose-specific and cannot be accepted by generic command paths without an explicit version adapter.

- [ ] **Step 6: Verify and commit governance**

  Run the exact Step 3 command. Expected: PASS and committed manifests remain No-Go/outbound disabled.

  ```bash
  git add -- docs/adr/ADR-0004-ai-drafts-and-human-commands.md docs/permission-matrix.md contracts/email_gateway/email-send-approved-command-v2.0.schema.json contracts/email_gateway/examples/email-send-approved-command-v2.json contracts/gate2/contract-evolution-matrix.json contracts/README.md tests/contracts/test_email_send_approved_command.py tests/governance/test_email_send_governance.py
  git diff --cached --name-only | sort
  git commit -m "docs(email-gateway): govern approved outbound commands"
  ```

### Task 15: Persist Frappe approval, ApprovedCommand, and publication atomically

**Files:**

- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/__init__.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/gbos_email_send_approval.json`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/gbos_email_send_approval.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/test_gbos_email_send_approval.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/__init__.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/gbos_approved_command.json`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/gbos_approved_command.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/test_gbos_approved_command.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/__init__.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/gbos_command_publication.json`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/gbos_command_publication.py`
- Create: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/test_gbos_command_publication.py`
- Create: `apps/esan_gbos/esan_gbos/domain/email_review_policy.py`
- Create: `apps/esan_gbos/esan_gbos/domain/approved_command.py`
- Create: `apps/esan_gbos/esan_gbos/api/v5/email_send.py`
- Create: `apps/esan_gbos/esan_gbos/api/internal/email_command_publication.py`
- Create: `apps/esan_gbos/esan_gbos/email_command_publication_service.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/gateway.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v5/email_inbox.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/v5_email_dto.py`
- Modify: `contracts/bff-v5.openapi.json`
- Modify: `tests/contracts/test_bff_v5_openapi.py`
- Extend: `tests/domain/test_bff_v5_email.py`
- Extend: `tests/domain/test_bff_v5_runtime.py`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.json`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/review_dto.py`
- Modify: `apps/esan_gbos/esan_gbos/api/v2/review_case.py`
- Modify: `apps/esan_gbos/esan_gbos/hooks.py`
- Modify: `apps/esan_gbos/esan_gbos/install.py`
- Modify: `apps/esan_gbos/esan_gbos/fixtures/role.json`
- Create: `tests/domain/test_email_send_review_policy.py`
- Create: `tests/domain/test_approved_command.py`
- Create: `tests/domain/test_internal_email_command_publication.py`
- Create: `tests/domain/test_email_command_publication_service.py`
- Extend: `tests/domain/test_permissions.py`
- Extend: `tests/domain/test_review_dto.py`
- Modify: `scripts/dev/test-email-gateway-frappe`
- Extend: `tests/infra/test_email_gateway_frappe.py`

- [ ] **Step 1: Write native/fake-Frappe DocType RED tests**

  Email Send Approval contains only Gateway refs/revisions/digests/team/owner/purpose/evidence, not plaintext draft or sending state. Approved Command and Command Publication are append-only/non-deletable. Generic list/export/share/print/email and direct `frappe.client.*` writes are denied. Role fixtures and install/migrate inventory are exact.

- [ ] **Step 2: Write delegated-owner policy RED tests**

  Only the current eligible `assignee_user_ref`, explicitly assigned to the exact `email_send_owner_v1` Review Case, may approve. Sales Manager/Reviewer/Admin cannot silently substitute unless a new policy explicitly delegates them. Reject actor/team/purpose mismatch, stale pins, revoked recipient, changed owner, expired Review Case, reused idempotency drift, AI/system/service actor, and generic review endpoint.

- [ ] **Step 3: Run focused Frappe tests and verify RED**

  Run: `uv run --frozen pytest tests/domain/test_email_send_review_policy.py tests/domain/test_approved_command.py tests/domain/test_internal_email_command_publication.py tests/domain/test_email_command_publication_service.py tests/domain/test_permissions.py tests/domain/test_review_dto.py tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py -q`

  Expected: FAIL on missing three DocTypes, specialized policy/API, transaction, and append-only permissions.

- [ ] **Step 4: Implement immutable authorization projection**

  Expand BFF v5 from the 17 Chunk 3 operations to exactly 19 by adding only `email_send.submit_for_review` and `email_send.approve`; neither endpoint sends or writes Gateway Send Outbox. Submit first obtains the exact Gateway draft/authority snapshot, then calls Observer `email-draft-material/finalize` for that draft revision and opaque participant binding. Only after the final MIME EvidenceRef/digest/binding is returned does Frappe create the immutable Email Send Approval subject and pinned Review Case. BFF submits only closed refs/digests; no raw draft, MIME, or external address is copied into MariaDB or Gateway.

  Do not add a twentieth status operation. Extend the existing `email_inbox.get` response with an optional closed `send_governance` projection containing safe review state, subject/command/outbox revisions, expiry, assigned-reviewer display label, outbox state, provider receipt outcome/timestamps, and fixed safe code. It omits mapping/User/provider refs, raw addresses/content, command payload, and provider payload. BFF composes it from the pinned Frappe safe review read plus Gateway safe outbox projection and rejects cross-Inbox or revision mismatch. Contract tests assert the exact 19-operation count and closed DTO.

- [ ] **Step 5: Commit decision, command, and publication in one transaction**

  The specialized approve endpoint locks Review Case and subject, revalidates actor/authority/expiry/hash, appends decision audit, builds v2 ApprovedCommand, and inserts Command Publication before commit. Any failure rolls back all three. Response-loss replay returns the original command/publication by outer idempotency key.

- [ ] **Step 6: Add a fenced publication worker contract**

  Add a closed internal Frappe API for a desk-less `Email Command Publication Consumer` role with exact site/purpose/auth-ref/API-key identity and no standard/custom DocPerm. Add a non-whitelisted, strict-True, production-rejecting provisioning helper that consumes only mounted API-key/API-secret files, creates or exactly verifies the fixed service User/role, commits on success, rolls back on error, and returns a redacted receipt. Freeze these methods and shapes now: `POST /api/method/esan_gbos.api.internal.email_command_publication.claim` accepts `{site_id,processing_purpose,worker_id,lease_seconds,request_id}` and returns empty or `{publication_ref,attempt,generation,fence_token,lease_expires_at,command,payload_digest}`; `heartbeat` accepts the exact claim identity and returns the renewed lease; `acknowledge` accepts claim identity plus Gateway receipt ref/digest; `release` accepts claim identity plus a fixed retry/dead-letter safe code. Every response is no-store and replay-stable. Publication claims include attempt/generation/fence/heartbeat, stable request ID, bounded retry/dead-letter, and exact Gateway receipt replay. The future worker receives only this Frappe API credential set and the dedicated Gateway command-ingest credential; it cannot send mail, read draft content, list arbitrary DocTypes, or open either database.

- [ ] **Step 7: Verify Frappe transaction and migration twice**

  Extend the existing isolated runner with the three new native DocType modules and internal publication boundary; do not point it at a shared site. Run the exact Step 3 command and `uv run --frozen pytest tests/infra/test_email_gateway_frappe.py -q`, then `scripts/dev/test-email-gateway-frappe`. Expected: all PASS; second migration is a no-op; command/publication count remains one after replay; teardown leaves no project; no Gateway/provider network in unit/native tests.

- [ ] **Step 8: Commit command authority**

  ```bash
  git add -- apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/__init__.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/gbos_email_send_approval.json apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/gbos_email_send_approval.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_email_send_approval/test_gbos_email_send_approval.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/__init__.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/gbos_approved_command.json apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/gbos_approved_command.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_approved_command/test_gbos_approved_command.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/__init__.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/gbos_command_publication.json apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/gbos_command_publication.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_command_publication/test_gbos_command_publication.py apps/esan_gbos/esan_gbos/domain/email_review_policy.py apps/esan_gbos/esan_gbos/domain/approved_command.py apps/esan_gbos/esan_gbos/api/v5/email_send.py apps/esan_gbos/esan_gbos/api/internal/email_command_publication.py apps/esan_gbos/esan_gbos/email_command_publication_service.py apps/esan_gbos/esan_gbos/api/v5/gateway.py apps/esan_gbos/esan_gbos/api/v5/email_inbox.py apps/esan_gbos/esan_gbos/domain/v5_email_dto.py contracts/bff-v5.openapi.json tests/contracts/test_bff_v5_openapi.py tests/domain/test_bff_v5_email.py tests/domain/test_bff_v5_runtime.py apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.json apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.py apps/esan_gbos/esan_gbos/domain/review_dto.py apps/esan_gbos/esan_gbos/api/v2/review_case.py apps/esan_gbos/esan_gbos/hooks.py apps/esan_gbos/esan_gbos/install.py apps/esan_gbos/esan_gbos/fixtures/role.json tests/domain/test_email_send_review_policy.py tests/domain/test_approved_command.py tests/domain/test_internal_email_command_publication.py tests/domain/test_email_command_publication_service.py tests/domain/test_permissions.py tests/domain/test_review_dto.py scripts/dev/test-email-gateway-frappe tests/infra/test_email_gateway_frappe.py
  git diff --cached --name-only | sort
  git commit -m "feat(frappe): issue durable email send commands"
  ```

### Task 16: Add the specialized Action Guard verifier

**Files:**

- Create: `services/action_guard/email_send.py`
- Modify: `services/action_guard/models.py`
- Modify: `services/action_guard/policy.py`
- Create: `tests/action_guard/test_email_send.py`
- Extend: `tests/action_guard/test_policy.py`

- [ ] **Step 1: Write the full negative RED matrix**

  Reject wrong contract/policy version, site/team/purpose/audience, actor/delegation, expired command, stale mailbox/Inbox/Conversation/draft/mapping/Party/team/owner revisions, changed normalized recipients/final MIME digest, missing/duplicate evidence, command replay drift, missing scope, emergency stop, and `external_send=false`.

- [ ] **Step 2: Run the focused Action Guard tests and verify RED**

  Run: `uv run --frozen pytest tests/action_guard/test_email_send.py tests/action_guard/test_policy.py -q`

  Expected: FAIL on the missing email verifier/model and because generic human-review state cannot authorize execution.

- [ ] **Step 3: Define an explicit verified-command model**

  ```python
  @dataclass(frozen=True, slots=True)
  class VerifiedEmailSendCommand:
      command_ref: str
      idempotency_key: str
      stable_client_request_id: str
      payload_digest: str
      policy_version: str
  ```

  Construction is possible only from closed-schema validation plus live-authority receipts; no caller-provided boolean such as `approved=True` is accepted.

- [ ] **Step 4: Implement `email_send_owner_v1` evaluation**

  Generic `external.message.send` remains `REQUIRE_HUMAN`; only the specialized verifier can convert the exact ApprovedCommand to an executable result after human decision and all live checks. Post-result verification accepts only the immutable outbox receipt shape and forbids direct provider/execution payloads.

- [ ] **Step 5: Verify policy regression and commit**

  Run: `uv run --frozen pytest tests/action_guard/test_email_send.py tests/action_guard/test_policy.py tests/agent_runtime -q`

  ```bash
  git add -- services/action_guard/email_send.py services/action_guard/models.py services/action_guard/policy.py tests/action_guard/test_email_send.py tests/action_guard/test_policy.py
  git diff --cached --name-only | sort
  git commit -m "feat(action-guard): verify delegated email send commands"
  ```

### Task 17: Consume commands and create an immutable Send Outbox

**Files:**

- Create: `services/email_gateway/outbound.py`
- Create: `services/email_gateway/provider.py`
- Create: `services/email_gateway/worker.py`
- Modify: `services/email_gateway/api.py`
- Modify: `services/email_gateway/security.py`
- Extend: `services/email_gateway/send_outbox.py`
- Create: `services/email_gateway/migrations/007_email_gateway_outbound.sql`
- Create: `tests/email_gateway/fakes/__init__.py`
- Create: `tests/email_gateway/fakes/provider.py`
- Create: `tests/email_gateway/test_outbound.py`
- Create: `tests/email_gateway/test_worker.py`
- Create: `tests/email_gateway/test_reconciliation.py`
- Create: `tests/email_gateway/test_outbound_migration.py`
- Create: `tests/integration/test_email_send_command_offline_e2e.py`
- Create: `services/local_pilot_runtime/email_command_publication_worker.py`
- Create: `services/local_pilot_runtime/email_send_worker.py`
- Create: `tests/local_pilot_runtime/test_email_command_publication_worker.py`
- Create: `tests/local_pilot_runtime/test_email_send_worker.py`
- Modify: `services/local_pilot_runtime/runtime_support.py`
- Modify: `contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- Modify: `scripts/local-pilot/start`
- Modify: `scripts/local-pilot/render-config`
- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`
- Modify: `infra/local/local-pilot-manifest.json`
- Extend: `tests/infra/test_email_gateway_runtime_composition.py`

- [ ] **Step 1: Write command-ingest transaction RED tests**

  Validate schema/auth, verify Action Guard and live Frappe/identity/Gateway revisions, then atomically insert `command_inbox` receipt and exactly one immutable Send Outbox. Replay returns the same outbox; payload drift conflicts. PWA/API DB role has no INSERT/UPDATE grant on Send Outbox; only command-executor role inserts.

- [ ] **Step 2: Run command/outbox tests and verify RED**

  Run: `uv run --frozen pytest tests/email_gateway/test_outbound.py tests/email_gateway/test_worker.py tests/email_gateway/test_reconciliation.py tests/email_gateway/test_outbound_migration.py tests/local_pilot_runtime/test_email_command_publication_worker.py tests/local_pilot_runtime/test_email_send_worker.py tests/integration/test_email_send_command_offline_e2e.py -q`

  Expected: FAIL on missing migration 007, command-ingest endpoint, immutable outbox writer, workers, and fake-provider chain.

- [ ] **Step 3: Persist the complete immutable envelope**

  Store only opaque role-tagged sender/recipient refs with mapping refs/revisions, mailbox config, draft/Inbox/Conversation, Party/team/owner revisions, final MIME EvidenceRef/digest, approver/Review Case/command/policy/time, expiry, idempotency key, and stable client request ID. Store no raw address, secret, final MIME bytes, or editable body copy. Tests scan the command receipt/outbox/audit tables and serialized `repr` for fixture addresses.

- [ ] **Step 4: Add append-only attempts and receipts**

  Worker role may lease/update authorized outbox state and append attempts/receipts only. Each attempt records fence, start/end, provider request digest, closed result, safe code, and provider receipt ref. It cannot alter the approved envelope.

- [ ] **Step 5: Implement the command-publication relay without dual database credentials**

  Freeze exact Frappe methods:

  - `POST /api/method/esan_gbos.api.internal.email_command_publication.claim` with closed request `{site_id,processing_purpose,worker_id,lease_seconds,request_id}` and either empty receipt or `{publication_ref,attempt,generation,fence_token,lease_expires_at,command,payload_digest}`;
  - `POST /api/method/esan_gbos.api.internal.email_command_publication.heartbeat` with the exact claim identity and a new lease receipt;
  - `POST /api/method/esan_gbos.api.internal.email_command_publication.acknowledge` with the exact claim identity plus Gateway receipt ref/digest;
  - `POST /api/method/esan_gbos.api.internal.email_command_publication.release` with the exact claim identity plus one fixed safe retry/dead-letter code.

  Freeze Gateway `POST /internal/v1/email-commands/accept` with exact site/purpose/audience/scoped bearer, closed ApprovedCommand plus publication ref/attempt/generation/fence/digest, and stable `{command_receipt_ref,send_outbox_ref,payload_digest}` replay response. The relay uses only the Frappe publication API key/secret credential set and the separate Gateway command-ingest bearer. It claims/heartbeats one Frappe publication, posts the frozen command, and acknowledges Frappe only after the stable Gateway receipt. Gateway atomically inserts command receipt plus one Send Outbox under its own DB role. The relay has no Frappe/MariaDB or Gateway/PostgreSQL credential, provider secret, draft-content read, or send capability.

  Compose profile-only `frappe-email-command-publication-bootstrap` and `email-command-publication-worker` on local-internal only. Start order is migrations → exact service-identity bootstrap → worker; bootstrap mounts only the Frappe key/secret files and exits before the worker starts. The worker uses exact `http://frappe-backend:8000` and `http://email-gateway-api:8004`, mounted `frappe_email_command_publication_api_key`, `frappe_email_command_publication_api_secret`, and `email_gateway_command_ingest_bearer`. Add a separate default-true kill switch and closed manifest component. Neither service has controlled egress. Infra tests prove their exact secret/capability subsets and that all preflight failures occur before an HTTP client factory.

- [ ] **Step 6: Prove deterministic fake-provider success and replay**

  One command yields one provider submission and one receipt despite worker crash/restart/response replay. Provider accepted, delivered, bounced, permanently rejected, and uncertain are distinct states. SMTP/API acceptance is not delivery.

- [ ] **Step 7: Prove uncertainty is fail-closed**

  Timeout after submission immediately enters `reconciliation_required`; the normal worker never retries it. Same approval may resume only if provider lookup by stable ID proves non-submission. Otherwise a new draft/Review Case/command/message identity and explicit duplicate-risk acknowledgement are required.

- [ ] **Step 8: Add emergency-stop and fatal authority drift**

  Before every claim and provider call, check emergency stop, external-send switch, command expiry, identity status, mailbox revision, and route authority. Drift stops pre-egress and records a safe terminal/review-required state; no claim loop can bypass it.

- [ ] **Step 9: Prove the offline command chain**

  Draft snapshot → Frappe Review Case decision → ApprovedCommand/publication → Gateway command receipt → Action Guard → Send Outbox → fake provider → receipt. Cover replay, stale owner, revoked recipient, crash before/after provider call, uncertainty, bounce, and emergency stop. Assert one external-effect call maximum.

- [ ] **Step 10: Verify migration upgrade and commit outbox execution**

  Run the exact Step 2 command plus `uv run --frozen pytest tests/action_guard tests/email_gateway tests/local_pilot_runtime/test_email_command_publication_worker.py tests/local_pilot_runtime/test_email_send_worker.py tests/infra/test_email_gateway_runtime_composition.py -q`, then `scripts/dev/test-email-gateway-postgres --all`. Expected: migration 007 upgrades the Chunk 3 schema once, a second run is a no-op, RLS/grants are exact, relay composition is default-off/least-secret, all offline tests PASS, and no network/provider credential is used.

  ```bash
  git add -- services/email_gateway/outbound.py services/email_gateway/provider.py services/email_gateway/worker.py services/email_gateway/api.py services/email_gateway/security.py services/email_gateway/send_outbox.py services/email_gateway/migrations/007_email_gateway_outbound.sql tests/email_gateway/fakes/__init__.py tests/email_gateway/fakes/provider.py tests/email_gateway/test_outbound.py tests/email_gateway/test_worker.py tests/email_gateway/test_reconciliation.py tests/email_gateway/test_outbound_migration.py tests/integration/test_email_send_command_offline_e2e.py services/local_pilot_runtime/email_command_publication_worker.py services/local_pilot_runtime/email_send_worker.py tests/local_pilot_runtime/test_email_command_publication_worker.py tests/local_pilot_runtime/test_email_send_worker.py services/local_pilot_runtime/runtime_support.py contracts/local_pilot/local-pilot-manifest-v1.0.schema.json scripts/local-pilot/start scripts/local-pilot/render-config scripts/local-pilot/prepare-secrets infra/local/compose.yml infra/local/runtime-entrypoints.json infra/local/local-pilot-manifest.json tests/infra/test_email_gateway_runtime_composition.py
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): execute approved send outbox"
  ```

### Task 18: Add provider outbound adapter and approval UI, then hold the real-send gate

**Files:**

- Create only after official contract proof: `contracts/email_gateway/wecom-app-mail-send-v1.0.schema.json`
- Create only after official contract proof: `contracts/email_gateway/wecom-app-mail-send-status-v1.0.schema.json`
- Create only after official contract proof: `tests/fixtures/wecom_app_mail/official-outbound-fixtures-v1.json`
- Modify: `contracts/README.md`
- Extend: `tests/contracts/test_email_gateway_contracts.py`
- Modify: `docs/compat/wecom-app-mail-contract.md`
- Create only after official contract proof: `services/email_gateway/providers/__init__.py`
- Create only after official contract proof: `services/email_gateway/providers/wecom_app_mail_sender.py`
- Create only after official contract proof: `tests/email_gateway/test_wecom_app_mail_sender.py`
- Create: `services/observer/observer/email_send_material.py`
- Modify: `services/observer/observer/local_pilot_api.py`
- Create: `tests/observer/test_email_send_material.py`
- Modify: `apps/esan_gbos/frontend/src/components/email/ReplyDraftEditor.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/EmailSendApprovalPanel.vue`
- Create: `apps/esan_gbos/frontend/src/components/email/SendDeliveryTimeline.vue`
- Modify: `apps/esan_gbos/frontend/src/views/EmailInboxDetailView.vue`
- Modify: `apps/esan_gbos/frontend/src/api/email-gateway-types.ts`
- Modify: `apps/esan_gbos/frontend/src/api/email-gateway.ts`
- Extend: `apps/esan_gbos/frontend/tests/email-gateway.test.ts`
- Extend: `apps/esan_gbos/frontend/tests/v5.test.ts`
- Extend: `apps/esan_gbos/frontend/e2e/gbos.spec.ts`
- Modify: `services/local_pilot_runtime/email_send_worker.py`
- Modify: `services/local_pilot_runtime/runtime_support.py`
- Modify: `contracts/local_pilot/local-pilot-manifest-v1.0.schema.json`
- Modify: `scripts/local-pilot/start`
- Modify: `scripts/local-pilot/render-config`
- Modify: `scripts/local-pilot/prepare-secrets`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`
- Extend: `tests/local_pilot_runtime/test_email_send_worker.py`
- Extend: `tests/infra/test_email_gateway_runtime_composition.py`
- Modify: `docs/local-pilot/RUNBOOK.md`
- Modify: `docs/local-pilot/SAFETY_ASSERTIONS.md`

- [ ] **Step 1: Freeze the official outbound send and status contract or stop**

  Re-read current official WeCom application-mail documentation and record source URLs/access date, credential/audience, exact send serialization, sender/mailbox binding, recipient/header/attachment limits, stable request/receipt identifiers, rate limits, error mapping, and status/lookup semantics in the compatibility note and two closed schemas. Write the sanitized fixture bundle from those exact responses. If send or post-timeout lookup cannot be proven from an authoritative source, leave these three “official contract proof” files absent, keep the adapter/runtime disabled, record the blocker, and stop Task 18. In that state Phase 4 and Task 19 closure are prohibited; Chunks 1–3 may remain completed independently.

- [ ] **Step 2: Write all provider, material, runtime, and approval-UI RED tests**

  Provider tests bind exact app/mailbox identity, opaque envelope/final MIME digest, stable client request ID, request size/attachment limits, token/auth, rate limit, and closed results. Reject arbitrary URL/host, header injection, BCC leakage, unsupported attachment, recipient drift, response-model drift, and unsafe errors. Material tests reject arbitrary EvidenceRef browsing, wrong command/digest/roles/site/purpose, repeat drift, and persistence/logging of returned bytes. Runtime tests require default-off controlled egress, exact mounted secrets, and preflight before DB/HTTP/provider factories.

  UI tests require current assignee to see frozen sender/recipient labels, customer/owner/team, draft revision, evidence summary, risk warnings, policy, expiry, and explicit approval through the existing 19-operation contract. Sensitive commercial terms show strong warnings without inventing a second reviewer. Non-assignee, stale/revoked/409/expired, offline, or uncertain cases cannot approve/send. Assert the entire DOM/URL/log capture contains no protected refs or raw addresses.

- [ ] **Step 3: Run provider/runtime/frontend tests and verify RED**

  Run: `uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/email_gateway/test_wecom_app_mail_sender.py tests/observer/test_email_send_material.py tests/local_pilot_runtime/test_email_send_worker.py tests/infra/test_email_gateway_runtime_composition.py -q`

  Then run: `corepack pnpm --dir apps/esan_gbos/frontend exec vitest run tests/email-gateway.test.ts tests/v5.test.ts`.

  Expected: FAIL on the missing sender, mounted-secret runtime binding, approval/receipt UI, and disabled composition assertions.

- [ ] **Step 4: Implement sender behind the common outbound protocol**

  Add exact Observer `POST /internal/v1/email-send-material/resolve`. It accepts only the verified command ref/digest, final MIME EvidenceRef/digest, site/purpose, and a dedicated scoped bearer; Observer revalidates the EvidenceRef binding and returns bounded `PreparedEmailSendMaterial` once per authorized request with `Cache-Control: no-store`. It cannot list/browse arbitrary evidence. The send process holds this material only in memory, validates its digest and opaque-role binding, then passes it with the mounted provider credential to the adapter. The adapter has no database, Frappe, Observer browser, model, merge, or approval capability. SMTP compatibility is a separate adapter and is not silently substituted.

- [ ] **Step 5: Implement draft/approval/receipt components**

  The UI creates/edits drafts, submits a review, and approves the specialized Review Case; it never calls provider or Send Outbox directly. After approval it displays command/outbox/delivery states and requires bounded reload on conflicts. Protected refs/raw addresses do not enter DOM/URL/logs.

- [ ] **Step 6: Compose the sender disabled**

  Add controlled-egress only to the send worker; mount only its provider secret plus the separate `observer_email_send_material_bearer`. The worker has Gateway worker DB, exact local authority/Action Guard/material URLs, and no CAS mount or arbitrary Observer credential. Keep `GBOS_EXTERNAL_SEND_ENABLED=false` and kill switch true in committed config. Formal preflight must still return No-Go for real send.

- [ ] **Step 7: Run exact provider/runtime/frontend GREEN gates**

  ```bash
  uv run --frozen pytest tests/contracts/test_email_gateway_contracts.py tests/email_gateway/test_wecom_app_mail_sender.py tests/observer/test_email_send_material.py tests/local_pilot_runtime/test_email_send_worker.py tests/infra/test_email_gateway_runtime_composition.py tests/integration/test_email_send_command_offline_e2e.py -q
  corepack pnpm --dir apps/esan_gbos/frontend exec vitest run tests/email-gateway.test.ts tests/v5.test.ts
  corepack pnpm --dir apps/esan_gbos/frontend run lint
  corepack pnpm --dir apps/esan_gbos/frontend run typecheck
  corepack pnpm --dir apps/esan_gbos/frontend run test:unit
  corepack pnpm --dir apps/esan_gbos/frontend run build
  corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
  ```

  Expected: all PASS using injected transport and fixture contracts; 375/768/1440, true 200% zoom, keyboard, axe, 403/409/reload, uncertainty, and sensitive DOM/URL scans pass; provider network call count is zero; committed `GBOS_EXTERNAL_SEND_ENABLED=false`; formal real-send preflight remains No-Go.

- [ ] **Step 8: Document—but do not execute—the real outbound gate**

  Later explicit authorization must select one test recipient and harmless message, rotate any chat-exposed password, verify current-source images/config, observe one provider send and receipt, reconcile recipient copy, exercise emergency stop, and record audit. Missing proof keeps outbound No-Go and does not block Chunks 1–3.

- [ ] **Step 9: Commit disabled outbound readiness**

  ```bash
  git add -- contracts/email_gateway/wecom-app-mail-send-v1.0.schema.json contracts/email_gateway/wecom-app-mail-send-status-v1.0.schema.json tests/fixtures/wecom_app_mail/official-outbound-fixtures-v1.json contracts/README.md tests/contracts/test_email_gateway_contracts.py docs/compat/wecom-app-mail-contract.md services/email_gateway/providers/__init__.py services/email_gateway/providers/wecom_app_mail_sender.py tests/email_gateway/test_wecom_app_mail_sender.py services/observer/observer/email_send_material.py services/observer/observer/local_pilot_api.py tests/observer/test_email_send_material.py apps/esan_gbos/frontend/src/components/email/ReplyDraftEditor.vue apps/esan_gbos/frontend/src/components/email/EmailSendApprovalPanel.vue apps/esan_gbos/frontend/src/components/email/SendDeliveryTimeline.vue apps/esan_gbos/frontend/src/views/EmailInboxDetailView.vue apps/esan_gbos/frontend/src/api/email-gateway-types.ts apps/esan_gbos/frontend/src/api/email-gateway.ts apps/esan_gbos/frontend/tests/email-gateway.test.ts apps/esan_gbos/frontend/tests/v5.test.ts apps/esan_gbos/frontend/e2e/gbos.spec.ts services/local_pilot_runtime/email_send_worker.py services/local_pilot_runtime/runtime_support.py contracts/local_pilot/local-pilot-manifest-v1.0.schema.json scripts/local-pilot/start scripts/local-pilot/render-config scripts/local-pilot/prepare-secrets infra/local/compose.yml infra/local/runtime-entrypoints.json tests/local_pilot_runtime/test_email_send_worker.py tests/infra/test_email_gateway_runtime_composition.py docs/local-pilot/RUNBOOK.md docs/local-pilot/SAFETY_ASSERTIONS.md
  git diff --cached --name-only | sort
  git commit -m "feat(email-gateway): add disabled approved outbound path"
  ```

### Task 19: Run the complete offline release matrix and produce an honest handoff

**Files:**

- Modify: `.github/workflows/ci.yml`
- Modify: `docs/HANDOFF.md`
- Modify: `docs/local-pilot/IMPLEMENTATION_PLAN.md`
- Create: `docs/evidence/email-gateway/email-gateway-evidence.json`
- Create: `docs/evidence/email-gateway/email-gateway-summary.md`
- Create: `docs/evidence/email-gateway/SHA256SUMS`
- Create: `tests/governance/test_email_gateway_handoff_truth.py`
- Create: `tests/infra/test_email_gateway_docs.py`
- Create: `tests/infra/test_email_gateway_ci.py`

- [ ] **Step 1: Enforce the Phase-4 prerequisite and write the CI RED test**

  Task 19 must not start unless Task 18 froze both authoritative outbound schemas/fixtures and all disabled adapter/material/runtime/UI gates are GREEN. If Task 18 stopped for missing official evidence, record a blocker outside this closure package and stop here.

  Require CI to collect Email Gateway contracts/unit/integration-offline tests, run MyPy on `services/email_gateway`, run the frontend v5 Email tests, and keep every real-provider test explicitly gated. Run: `uv run --frozen pytest tests/infra/test_email_gateway_ci.py -q`.

  Expected: FAIL because CI does not yet contain the exact Gateway targets.

- [ ] **Step 2: Update CI, verify GREEN, and create the validation commit**

  Run: `uv run --frozen pytest tests/infra/test_email_gateway_ci.py -q`. Expected: PASS.

  ```bash
  git add -- .github/workflows/ci.yml tests/infra/test_email_gateway_ci.py
  git diff --cached --name-only | sort
  git commit -m "ci(email-gateway): enforce offline release gates"
  test -z "$(git status --porcelain=v1)"
  git rev-parse HEAD
  ```

  Record this clean commit as `validation_reference_commit`; the later evidence commit must reference it, not itself.

- [ ] **Step 3: Run all backend/static gates at the clean validation commit**

  ```bash
  uv run --frozen pytest -q
  uv run --frozen ruff check .
  uv run --frozen ruff format --check .
  uv run --frozen mypy services/email_gateway services/observer/observer services/local_pilot_runtime services/action_guard apps/esan_gbos/esan_gbos/domain
  uv run --frozen python -m compileall -q apps services scripts tests
  scripts/dev/secret-scan
  git diff --check
  ```

  Expected: all pass except explicitly documented environment-gated native/real-provider tests; any skip count is itemized.

- [ ] **Step 4: Run database and Frappe gates in isolation**

  ```bash
  scripts/dev/test-email-gateway-postgres --all
  scripts/dev/test-email-gateway-frappe
  ```

  Expected: both commands exit 0. The PostgreSQL runner applies Observer 001–015 and Gateway 001–007 twice, then runs `tests/infra/test_email_gateway_postgres_script.py`, `tests/integration/test_email_gateway_postgres.py`, and `tests/integration/test_gate3_postgres.py` inside its governed disposable connection context. The Frappe runner migrates twice and runs `tests/infra/test_email_gateway_frappe.py` plus named native modules in a unique current-source project/site. RLS/grants/native command tests pass; both runners remove containers/networks/volumes; no shared site or old ledger is mutated.

- [ ] **Step 5: Run frontend gates**

  ```bash
  corepack pnpm --dir apps/esan_gbos/frontend run lint
  corepack pnpm --dir apps/esan_gbos/frontend run typecheck
  corepack pnpm --dir apps/esan_gbos/frontend run test:unit
  corepack pnpm --dir apps/esan_gbos/frontend run build
  corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
  ```

  Expected: every command exits 0 and the Email routes pass the required responsive/accessibility/privacy cases.

- [ ] **Step 6: Run all deterministic offline component chains**

  ```bash
  uv run --frozen pytest tests/integration/test_email_gateway_offline_e2e.py tests/integration/test_wecom_app_mail_shadow_offline_e2e.py tests/integration/test_email_gateway_human_operations_offline_e2e.py tests/integration/test_email_send_command_offline_e2e.py -q
  ```

  Expected: exit 0; fake-provider core, WeCom frozen-fixture shadow ingress, human operations, and ApprovedCommand/fake outbound each pass separately; captured provider/model/network external call count is zero. Record that injected transports are not real provider evidence.

- [ ] **Step 7: Write handoff/evidence truth tests and verify RED**

  Require handoff/evidence to bind `validation_reference_commit`, image/source state, Observer/Gateway/Frappe migration evidence, exact test/skip counts, security scans, enabled/disabled switches, the 19-operation contract, official outbound contract provenance, and all unrun external gates. Require `SHA256SUMS` to list exactly `email-gateway-evidence.json` and `email-gateway-summary.md`, never itself. Historical evidence is immutable.

  Run: `uv run --frozen pytest tests/governance/test_email_gateway_handoff_truth.py tests/infra/test_email_gateway_docs.py -q`.

  Expected: FAIL because the current handoff/evidence package does not yet describe this validation commit.

- [ ] **Step 8: Update current truth without rewriting history**

  Evidence records the exact parent `validation_reference_commit`, image/source state, tests, migrations, security scans, enabled/disabled switches, and unrun external boundaries. Keep real WeCom callback/pull, real Email/DeepSeek, real provider send, Kingdee, cloud, production, and continuous soak as No-Go unless separately observed. Do not claim the evidence commit itself was the validated code commit.

- [ ] **Step 9: Verify truth tests and checksums**

  ```bash
  uv run --frozen pytest tests/governance/test_email_gateway_handoff_truth.py tests/infra/test_email_gateway_docs.py tests/infra/test_email_gateway_ci.py -q
  uv run --frozen python -m json.tool docs/evidence/email-gateway/email-gateway-evidence.json >/dev/null
  (cd docs/evidence/email-gateway && shasum -a 256 -c SHA256SUMS)
  git diff --check
  ```

  Expected: every command exits 0; both listed artifacts verify; `SHA256SUMS` does not contain `SHA256SUMS`; evidence references the parent validation commit.

- [ ] **Step 10: Commit the documentation/evidence successor**

  ```bash
  git add -- docs/HANDOFF.md docs/local-pilot/IMPLEMENTATION_PLAN.md docs/evidence/email-gateway/email-gateway-evidence.json docs/evidence/email-gateway/email-gateway-summary.md docs/evidence/email-gateway/SHA256SUMS tests/governance/test_email_gateway_handoff_truth.py tests/infra/test_email_gateway_docs.py
  git diff --cached --name-only | sort
  git commit -m "docs(email-gateway): record offline implementation closure"
  ```

---

## Phase exit matrix

| Phase | Code exit | Real-environment exit | External effect |
| --- | --- | --- | --- |
| 1 Provider-neutral core | Contracts, Observer checkpoint fence, Frappe authority, Gateway RLS/core, config PWA, read-only Email Inbox, and fake-provider E2E GREEN | None required | None |
| 2 WeCom shadow ingress | Official contract frozen; callback/pull/reconcile offline tests GREEN; outbound closed | One designated post-activation mail creates exactly one Inbox Item with matching EML digest and no backfill | Read-only receive only |
| 3 Human Inbox | State/RBAC/SLA/merge/BFF/PWA/retention/metrics GREEN | Operators prove identity, routing, claim/reassign, merge and SLA on shadow items | No outbound |
| 4 Approved outbound | Official send/status/timeout-lookup contracts frozen; 19-operation BFF and safe detail projection GREEN; final MIME CAS/material boundary GREEN; Frappe publication HTTP relay and default-off composition GREEN; ApprovedCommand/Action Guard/Gateway migration 007/outbox/no-blind-retry/provider-adapter/UI tests GREEN | One harmless approved message produces exactly one provider submission/receipt; emergency stop and reconciliation proven | Explicitly approved send only |

No later phase weakens an earlier boundary. Missing authoritative outbound send/status/timeout-lookup proof keeps Task 18, Phase 4, and Task 19 closure blocked. Any other missing real-environment proof keeps only that phase disabled; it does not retroactively invalidate completed earlier-phase offline code or authorize another provider path.
