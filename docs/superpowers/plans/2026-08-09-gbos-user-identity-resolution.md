# ESAN GBOS User Identity Resolution and Email Shadow Pilot Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add trusted, human-reviewed links between AI observation participants, Frappe system users, and customer parties before enabling the first real Email + DeepSeek shadow pilot.

**Architecture:** Frappe remains the authority for users, teams, CRM parties, and approved external identities. Observer stores opaque provider-subject references, trusted connector team routing, and a revision-pinned read projection; AI may create association proposals but cannot confirm identity or expand access. The first real channel remains Email IMAP, with no historical backfill and no outbound action.

**Tech Stack:** Frappe Framework v16, MariaDB, Python 3.14, FastAPI, PostgreSQL 17 with forced RLS, JSON Schema 2020-12, Vue 3, TypeScript, Vitest, Playwright, OrbStack Compose, macOS Keychain, DeepSeek `deepseek-v4-flash`.

**Approved design:** `docs/superpowers/specs/2026-08-09-gbos-user-identity-resolution-design.md`

---

## Implementation status snapshot — 2026-08-11

The original offline implementation baseline is `c98f6a5`. The runtime code validation
reference is `ad58ab3ea8c0d521cebd90c2642709d135f98fac`; the final branch includes only
image-lock/test/docs successors after it, so later documentation commits are not inside
the runtime images. The previously older-source image lock was rebuilt and recorded at
that runtime validation reference (image-lock recording commit `e10a780`). This status
section records what was actually implemented and verified; it does not authorize the
real canary. If final code changes again, rebuild and record images before running it.

The four user relationships remain separate and **禁止相互推导**:

- Team data access: `Observation.team_ref ↔ GBOS Team Member.user`.
- Connector account owner: `Connector Instance.account_user_ref`.
- Communication participant: `Participant.identity_ref`.
- CRM business assignment: `Deal owner / owner_user / assigned_to`.

| Scope | Current status | Remaining evidence |
| --- | --- | --- |
| Task 1–3 | Implemented and verified | None for the offline contract/baseline scope |
| Task 4–5 | Implemented and verified | 真实 Frappe v16 local site, double migrate and native identity tests passed |
| Task 6–8 | Implemented and verified | PostgreSQL migration/checksum chain ran twice; forced-RLS coverage retained |
| Task 9–11 | Implemented and verified | Live Frappe subset plus responsive frontend harness passed |
| Task 12 | Implemented and verified | Local composition, current-source image inspect/record, Prometheus/runtime boundaries, model fatal latch and offline drills verified |
| Task 13 | **部分完成** | Credential-free closure passed (`2687 passed/42 skipped/1 warning`, frontend unit `188`, Playwright harness `22`); real Email/DeepSeek credentials and observed model identity remain absent |

The current verdict is `credential_free_closure=go` and
`real_email_deepseek_canary=no_go`. The checked-in formal state remains
`local_pilot_go=false`; `production_go=false`; checked-in Email/DeepSeek are disabled;
real Email, real DeepSeek, `response_reported_observed_model`, Kingdee, cloud, production
and external send remain No-Go, unknown or not run. Model fatal latch behavior is
fail-closed. Email is source-bound to STATUS-only checkpoint/receipt/preflight, and the
machine DB-attested narrow-window verifier reports only `response_reported_observed_model`
without free-form observed-model input. 72 小时连续运行不再作为本阶段退出条件；it is
deferred/not required for this stage and does not relax any live channel, model, outbound
or production gate. Root's isolated PostgreSQL matrix is `42 passed, 10 deselected,
1 warning` across Gate3/4/5, Context and Media; its validation DB/network/volume was
removed. This snapshot used governed dependency/image/scanner network and isolated
PostgreSQL validation/build/scanner containers only; provider/channel network and pilot
application services were not used or started. 真实 Email + DeepSeek canary 未执行。

---

## Frozen boundaries

- Canonical path is `/Users/ericesan/Documents/GBOS`; do not use the trailing-space directory.
- Start from `main` SHA `735eed6ed7d352ced43bd048d5e63effe1a8cef3` or its descendant after baseline repair.
- Keep `production_go=false`, `local_pilot_go=false`, Kingdee/cloud/external send disabled in the committed formal manifest.
- The canary uses a generated, repository-external local manifest.
- No history backfill; Email begins at an operator-approved activation time.
- Raw evidence and token mappings retain for 30 days.
- AI never approves identity, merges customers, changes Deal stage, sends a message, quotes, commits price/delivery, or creates an order.
- `GBOS External Identity` is the authoritative mapping. Observer never connects directly to Frappe MariaDB.
- Team access, channel-account ownership, communication participation, and business assignment remain separate relations.

## File ownership

### Platform/CI owner

- Modify: `scripts/local-pilot/preflight.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/infra/test_local_pilot_scripts.py`
- Modify: `infra/local/compose.yml`
- Modify: `infra/local/runtime-entrypoints.json`

### Contract/Frappe owner

- Create: `contracts/local_pilot/external-identity-resolution-v1.0.schema.json`
- Create: `contracts/local_pilot/examples/valid/external-identity-resolution-v1.0.json`
- Modify: `contracts/README.md`
- Modify: `tests/contracts/test_local_pilot_contracts.py`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_external_identity/gbos_external_identity.py`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_external_identity/gbos_external_identity.json`
- Modify: `apps/esan_gbos/esan_gbos/gbos/doctype/gbos_review_case/gbos_review_case.py`
- Modify: `apps/esan_gbos/esan_gbos/domain/review_dto.py`
- Create: `apps/esan_gbos/esan_gbos/api/internal/identity_resolution.py`
- Create: `apps/esan_gbos/esan_gbos/identity_resolver_access.py`
- Modify: `apps/esan_gbos/esan_gbos/hooks.py`
- Modify: `apps/esan_gbos/esan_gbos/install.py`

### Observer/runtime owner

- Create: `services/observer/migrations/009_local_pilot_identity_resolution.sql`
- Create: `services/observer/observer/identity_resolution.py`
- Modify: `services/observer/observer/normalizers.py`
- Modify: `services/observer/observer/local_pilot_storage.py`
- Modify: `services/observer/observer/read_service.py`
- Create: `services/local_pilot_runtime/identity_resolution_worker.py`

### BFF/PWA owner

- Create: `apps/esan_gbos/esan_gbos/api/v4/identity.py`
- Modify: `contracts/bff-v4.openapi.json`
- Modify: `apps/esan_gbos/frontend/src/api/types.ts`
- Modify: `apps/esan_gbos/frontend/src/api/bff.ts`
- Modify: `apps/esan_gbos/frontend/src/views/CommunicationDetailView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/ReviewQueueView.vue`

No two owners may modify the same file concurrently. The primary agent owns migrations, shared contract indexes, Compose integration, secrets, real channel/model canaries, evidence, merge, and push.

## Chunk 1: Restore a trustworthy execution baseline

### Task 1: Fix the local-pilot preflight and close the CI blind spot

**Files:** Platform/CI owner paths.

- [ ] Write a failing test that invokes `python -m py_compile scripts/local-pilot/preflight.py` against the real file, without a Docker stub.
- [ ] Run the test and confirm the current Python 3 `except` syntax fails.
- [ ] Replace the invalid exception clause with:

```python
except (IndexError, KeyError, TypeError, json.JSONDecodeError):
    return None
```

- [ ] Add CI `compileall` coverage for tracked Python under `apps`, `services`, `scripts`, and `tests` before Ruff/mypy.
- [ ] Keep formal preflight fail-closed: disabled formal manifest must exit `78`, not `1`.
- [ ] Run the synthetic preflight against deterministic image-inspect stubs and expect `0`.
- [ ] Run `uv run --frozen pytest tests/infra -q`, Ruff, format, mypy, and `git diff --check`.
- [ ] Commit only baseline paths with `fix(local-pilot): compile and execute preflight safely`.

### Task 2: Reconcile the current handoff truth

- [ ] Correct README DocType inventory to 15 parent and 3 child DocTypes.
- [ ] Update the permission matrix to record the approved CEO auto-elevation role bundle.
- [ ] Replace stale “AI provider unselected” text with “DeepSeek boundary implemented, real model identity unobserved”.
- [ ] Reconcile runtime entrypoint status and Frappe/runtime image digests against `images.lock.json`.
- [ ] Add a current-SHA handoff status without modifying historical Gate evidence.
- [ ] Require documentation tests to fail on stale role, digest, model, or runtime-status statements.
- [ ] Commit with `docs(handoff): reconcile current GBOS runtime truth`.

## Chunk 2: Freeze the external-identity contract and Frappe authority

### Task 3: Add the closed identity-resolution contract

**Files:** Contract owner paths.

- [ ] Write failing schema tests for a record containing only:

```json
{
  "schema_version": "1.0",
  "site_id": "gbos.localhost",
  "identity_provider": "email",
  "external_subject_ref": "extid:v1:email:opaque-token",
  "mapping_ref": "EID-01K...",
  "mapping_revision": 1,
  "team_ref": "TEAM-SALES",
  "target_type": "User",
  "target_ref": "sales@example.invalid",
  "status": "confirmed",
  "resolved_at": "2026-08-09T00:00:00Z"
}
```

- [ ] Reject raw email/phone patterns, unknown provider/target/status, extra fields, invalid ULID/revision, missing team, and unbounded strings.
- [ ] Add the schema, valid example, README index, and complete-set test.
- [ ] Run contract tests and commit with `feat(contracts): freeze external identity resolution`.

### Task 4: Harden `GBOS External Identity`

**Files:** Frappe DocType, review, permission, install, and focused tests.

- [ ] Write failing tests proving `external_subject` accepts only `extid:v1:<provider>:<opaque>` and never raw email/phone.
- [ ] Require exactly one target for `User` or `Party`; `Channel` has no participant target.
- [ ] Require linked User membership or Party Profile ownership in the same team.
- [ ] Reject duplicate provider/subject, cross-team target, Approved AI mutation, stale revision, and generic `set_value` bypass.
- [ ] Add `GBOS External Identity` to pinned Review Case subject fields.
- [ ] Keep only `Approved + Active` mappings effective; Pending/Rejected/Superseded/Revoked remain non-authoritative.
- [ ] Add tests for review pinning, approval, rejection, supersession, and revision conflict.
- [ ] Run domain, permission, governance, Frappe metadata, migration, and double-migrate tests.
- [ ] Commit with `feat(frappe): govern external identity mappings`.

### Task 5: Add a least-privilege internal identity resolver

- [ ] Add role `Observer Identity Resolver` and a no-desk service user provisioning helper.
- [ ] Expose a non-whitelisted-to-guests internal endpoint accepting a bounded batch of provider + opaque subject refs + expected team.
- [ ] Authenticate site, purpose, request ID, auth-ref and Frappe token on every request.
- [ ] Return only approved mapping ref/revision, team, target type/ref and status; never return raw provider identity or unrelated CRM fields.
- [ ] Fail on zero/multiple conflicting approved mappings, cross-team mapping, stale revision, unknown provider, oversized batch, or role mismatch.
- [ ] Deny list/export/delete/report/share/print/email and all write access to the service user.
- [ ] Add direct DocType bypass, REST/list, exception-cleanup and error-redaction tests.
- [ ] Commit with `feat(frappe): expose governed identity resolution`.

## Chunk 3: Stable Observer identity and resolution projection

### Task 6: Replace delivery-scoped identity where a trusted provider subject exists

- [ ] Define `IdentityTokenResolver.resolve(site_id, purpose, provider, subject) -> identity_ref`.
- [ ] Use an injected HMAC-SHA256 key loaded from a regular, non-symlink `0400/0600` secret file.
- [ ] Include site, purpose and provider in the HMAC input; never expose subject or digest input through `repr`, errors or logs.
- [ ] Email: normalize sender/recipient provider subjects before evidence publication.
- [ ] WhatsApp: normalize `wa_id/from`; WeCom: normalize official `userid/external_userid` only when supplied by the trusted SDK.
- [ ] Preserve `unresolved:delivery:<ULID>` when no valid subject exists.
- [ ] Add tests: same subject/scope is stable; different site/purpose/provider differs; whitespace/case normalization is provider-specific; malformed/oversized inputs quarantine; PII sentinel never reaches stored document/model request/log.
- [ ] Commit with `feat(observer): derive scoped participant identities`.

### Task 7: Persist connector-account ownership and confirmed resolution projection

- [ ] Add migration `009` with nullable `account_user_ref` on connector routing and a separate `participant_identity_resolutions` table.
- [ ] Force RLS and site policy; grant only the Observer application role the minimum SELECT/INSERT/UPDATE rights.
- [ ] Keep `event.team_ref` sourced only from the locked Connector Instance row; never accept team/user/party from normalized or model output.
- [ ] Store mapping ref/revision, target type/ref, team, status and timestamps; store no provider subject, display name, phone, prompt or raw response. A Frappe `User.name` may be an email-shaped authoritative internal reference, but must remain permission-protected and absent from model/log/error surfaces.
- [ ] Enforce idempotent upsert by site/provider/subject/mapping revision and reject target drift.
- [ ] Preserve immutable participant refs and event rows; resolution is an additive projection, not an event rewrite.
- [ ] Add real PostgreSQL tests for migration twice, RLS, cross-site writes, restart, replay, revocation and ABA/fencing behavior.
- [ ] Commit with `feat(observer): project confirmed participant identities`.

### Task 8: Use confirmed identity only for self-access and party enrichment

- [ ] Change communication reads so team access remains primary.
- [ ] Allow actor self-access only when a confirmed active `User` resolution target exactly equals `frappe.session.user`.
- [ ] Derive displayed Party Profile from a confirmed `Party` projection without updating immutable `observation_events.party_ref`.
- [ ] Pending/Rejected/Revoked mappings must not grant access or enrich the party.
- [ ] CEO/GBOS Admin wildcard remains unchanged and still does not imply raw Restricted evidence access.
- [ ] Add SQL and in-memory parity tests for team, self, party, CEO, revoked and cross-team cases.
- [ ] Commit with `fix(observer): bind communication access to confirmed identity`.

## Chunk 4: Proposal, review and materialization

### Task 9: Convert association suggestions into pinned identity Review Cases

- [ ] Reuse existing `association_suggestions`; do not add a competing AI identity-fact type.
- [ ] Permit candidates only from authorized same-team Users, Party Profiles and Contacts returned by Frappe; model-supplied arbitrary target refs are rejected.
- [ ] Create `GBOS External Identity` as `origin=AI`, `review_status=AI Draft` with opaque subject ref and proposed target.
- [ ] `submit_for_review` must atomically set Pending and create one Review Case pinned to mapping revision, payload hash, evidence and policy version.
- [ ] Reviewer approval activates the mapping; rejection leaves it non-authoritative; neither decision changes CRM master data.
- [ ] Same idempotency key returns the original Draft/Review Case; changed payload conflicts.
- [ ] Add crash-after-Frappe-response, retry, stale subject, wrong reviewer, cross-team and duplicate candidate tests.
- [ ] Commit with `feat(identity): materialize reviewed association proposals`.

## Chunk 5: BFF and PWA workflow

### Task 10: Add identity-resolution reads and commands

- [ ] Add BFF v4 queries for unresolved/proposed/confirmed identity status and mapping detail.
- [ ] Add only two user commands: submit an identity Draft for review and revoke an approved mapping; continue to use Review Case decide for approval/rejection.
- [ ] Enforce CSRF, roles, team/record permission, expected revision, idempotency and audit request ID.
- [ ] Sales users may suggest a same-team Party/Contact; only Integration Admin/GBOS Admin may revoke; only assigned Reviewer or GBOS Admin may decide.
- [ ] Keep PWA clients same-origin, `cache: no-store`, and Service Worker NetworkOnly.
- [ ] Update OpenAPI and client contract tests before implementation.
- [ ] Commit with `feat(bff): expose governed identity resolution`.

### Task 11: Integrate identity state into existing pages

- [ ] Communication detail shows `未解析 / 已建议 / 待审核 / 已确认 / 已撤回` without exposing raw subject.
- [ ] Add a same-team candidate picker and “提交审核”; no direct confirm button.
- [ ] Review Queue adds an Identity Resolution filter and pinned evidence/target/revision view.
- [ ] Show channel-account owner separately from message participants and Work Item assignee.
- [ ] Keep original communication content hidden by default; reveal remains a separate authorized action.
- [ ] Add Vitest and Playwright cases for success, duplicate click, stale revision, permission denied, rejected mapping, revocation and responsive layouts.
- [ ] Verify 320/375/768/1024/1440px, keyboard order, 200% zoom and zero axe Critical/Serious findings.
- [ ] Commit with `feat(pwa): review observation identities`.

## Chunk 6: Offline composition and Email + DeepSeek canary

### Task 12: Compose the identity worker with fake transport first

- [ ] Add the identity resolver worker to runtime entrypoints and Compose with internal-only networking.
- [ ] Materialize HMAC and Frappe service credentials from Keychain to `0600` secrets.
- [ ] Add kill switches checked before DB/HTTP; no default credentials or inline secret environment variables.
- [ ] Complete fake-transport E2E:

```text
Email fixture
→ durable inbox
→ stable participant identity
→ Observer event/team
→ unresolved association proposal
→ Review Case approval
→ approved External Identity
→ second Email fixture
→ confirmed User/Party resolution
→ correctly scoped PWA read
```

- [ ] Prove no external network, no raw identity in logs, no duplicate mapping/draft, and restart-safe checkpoints.
- [ ] Add health/readiness, backlog, unresolved-count, conflict-count and resolver-latency metrics with nonempty alerts.
- [ ] Commit with `feat(local-pilot): compose identity resolution worker`.

### Task 13: Run the first real Email + DeepSeek shadow canary

**Primary-agent-only external inputs:** IMAP host/port/mailbox/app password/folder, activation time, target team, account user, DeepSeek API key/balance, HMAC key, current trusted phrase lexicon.

**Current boundary:** `credential_free_closure=go` and
`real_email_deepseek_canary=no_go`. Full pytest is `2687 passed/42 skipped/1 warning`;
Ruff check/format, mypy, compileall, secret scan, frontend lint/typecheck/build, unit
`188` and Playwright harness `22` are green. Model fatal latch behavior is fail-closed;
Email uses source-bound STATUS-only checkpoint/receipt/preflight; the machine
DB-attested narrow-window verifier reports only `response_reported_observed_model`.
No real IMAP connection or DeepSeek call has occurred, and
`response_reported_observed_model` is still unknown. The previously older-source locked
runtime images have now been rebuilt/recorded from final code; if final code changes again,
rebuild/record before the real canary. 72 小时
连续运行不再作为本阶段退出条件；it is deferred/not required for this stage and a
shorter evidence-bound health sample is sufficient once the real canary can be run.
Missing Email credential, DeepSeek API key, identity HMAC, trusted phrase lexicon and
Frappe identity-resolver credentials remain outside the repository and block the real run.

- [x] Keep committed formal manifest disabled and `external_send=false`.
- [x] Rebuild and record Frappe/runtime images from `ad58ab3`; re-run this gate if final code changes.
- [x] Verify credential-free model fatal latch, Email STATUS-only checkpoint/receipt/preflight, and chain-verifier boundaries.
- [x] Verify 30-day retention dry-run, emergency stop and credential-free restart/failure drills.
- [x] Produce a current credential-free closure package bound to `ad58ab3` without rewriting historical Gate evidence.
- [ ] Obtain the missing external inputs and generate a repository-external real canary manifest.

- [ ] Generate a repository-external canary manifest enabling only Email and model projection.
- [ ] Send new test emails from one known internal user and one known customer address after activation time.
- [ ] Verify BODY.PEEK does not mark read, move, delete or backfill messages.
- [ ] Approve one User and one Party mapping through the real Review UI.
- [ ] Send a second message from each identity and verify stable automatic resolution without new approval.
- [ ] Verify DeepSeek request contains no raw email/phone/name/organization sentinel and the machine chain attestation reports the response-reported model identity; mismatch stops the pilot.
- [ ] Verify AI Draft/Review output, model usage, $50 warning, $100 stop, 30-day retention and emergency stop.
- [ ] Run restart, UIDVALIDITY, duplicate UID, attachment quarantine, 429/timeout/invalid JSON and mapping-revocation drills.
- [ ] Produce a real-canary evidence package bound to source SHA, rebuilt image digests and the machine chain attestation's response-reported model identity; do not rewrite historical Gate evidence or accept free-form observed-model input.
- [ ] Declare only `Email + DeepSeek + identity resolution local shadow Go` when all checks pass. Kingdee, cloud, production, external send and formal compliance remain No-Go.

## Final verification matrix

- [ ] `python -m compileall` covers all tracked Python.
- [ ] Ruff check/format and mypy pass for all changed services.
- [ ] Contract examples and complete-set validation pass.
- [ ] Frappe fresh install, double migrate and native permission tests pass.
- [ ] Observer/Context/Agent PostgreSQL migrations run twice with forced RLS.
- [ ] Same sender resolves stably; cross-site/purpose identity does not correlate.
- [ ] No raw provider subject appears in token fields, logs, errors, model requests or CI artifacts; permission-protected Frappe target refs appear only in the authoritative mapping and minimal resolution projection.
- [ ] Pending/Rejected/Revoked mappings never grant self-access.
- [ ] Team routing cannot be changed by provider or model output.
- [ ] AI cannot approve, merge, send, quote, change Deal stage or create an order.
- [ ] Frontend lint, typecheck, unit, build, Playwright, axe and offline/cache tests pass.
- [ ] Kingdee calls, outbound sends, cloud business storage and formal automatic commands equal zero.
