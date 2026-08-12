# Local Shadow Pilot Infrastructure Implementation Plan

**Goal:** Run an isolated, fail-closed local GBOS shadow-pilot boundary for governed
channel ingestion, identity resolution and model proposals without enabling outbound
actions, Kingdee, cloud deployment or production state changes.

**Current validation references:** Frappe source reference
`485d3def0ea30ee49a3899d71c10b0787ba0429f`, runtime source reference
`bb260632ff44c7065a88327f264612139a9070a2`, and image-lock recording commit
`a599a5200e2a8e1b5e42301d74fe8d9d914161c4`. The locked Frappe/PWA image is
`sha256:2a0440df614314dec036ecc934e37aa0b3713b8cb8610e3ca2bd8ed69f9187c2`;
the local runtime image is
`sha256:de037ad28a020689fec8b72f743ad0224afdf5c2ca6856a2ea5568fabd45e568`.
Their source SHA256 labels are respectively
`441e33dec9acd744dd1b461ae49e950d18f764f05ae74e90357091a698320405` and
`c23d41903977fb350764ceee8a21efad70ce1079a7b6eed4503a87af3ac37db3`; each image carries
its own exact source revision and source-hash label.

## Current architecture

- Frappe/PWA, MariaDB, PostgreSQL, Redis, Observer API, Context API and Agent API are
  composed in the isolated `esan-gbos-local-pilot` project.
- UI, databases and internal APIs remain local; the checked-in formal manifest keeps
  channels, models, tunnel and external send disabled.
- Channel/model capabilities require explicit profiles, kill switches, exact manifests,
  least-privilege credentials and digest-locked images.
- Persistent evidence and tokenizer-vault volumes run as non-root UID/GID
  `10001:10001` with mode `0700`.
- The PWA is available locally at `http://127.0.0.1:58080/gbos`; this is local
  synthetic runtime evidence, not a production deployment.

## Completed locally

- [x] Compose isolation, loopback exposure, profiles, secrets, image locks and
  fail-closed preflight.
- [x] Current-source Frappe/PWA and runtime images rebuilt, inspected and recorded;
  the synthetic core was restarted from these recorded images.
- [x] Fresh Frappe v16 site install for ERPNext, CRM and `esan_gbos`, followed by two
  migrations and native identity permission tests.
- [x] Observer, Context, Agent and Media PostgreSQL migration chain executed twice with
  a consistent checksum ledger.
- [x] Frontend lint, typecheck, unit, build, responsive harness and current-site live
  subset verified.
- [x] Retention dry-run completed without deletion; emergency-stop containment was
  exercised and the disabled synthetic core was explicitly restored.
- [x] Credential-free fault drills covered duplicate UID, UIDVALIDITY change,
  attachment quarantine, model retry/protocol failure, identity restart and revocation.
- [x] Immediate identity authority denial, dynamic target eligibility, rejected mapping
  resubmission, governed revoke/review PWA and automatic retention scheduling verified.
- [x] The earlier disposable PostgreSQL matrix passed 43 tests; the current runtime
  source additionally passed isolated three-role migration/start-guard/chain SQL.
  Fresh current-image Frappe v16 identity/app native suites passed 13/59 tests after
  two migrations; all disposable state was removed.
- [x] Current pre-canary evidence package records source/image bindings and zero
  external activity without modifying historical Gate evidence.

The final credential-free P0 run records full backend `3064 passed, 44 skipped, 1 warning`,
failed `0`; the warning is the existing Starlette TestClient/httpx deprecation. Ruff check,
Ruff format (`528 files`), mypy (`101 sources`), compileall, and `scripts/dev/secret-scan`
are green. Frontend lint/typecheck/build are green, with unit `197 passed` and
frontend-harness Playwright `25 passed`. A disposable no-volume pgvector Gate 3 run recorded
15 migration-ledger entries, applied migrations twice, passed `17` integration tests with
one existing warning, and removed its container. Full-history Gitleaks scanned `263 commits`
with `0 leaks` using the reviewed exact synthetic allowlist committed at
`c27687ec6b39e669014b9ae8980cf6565556aaba`; this is not an unreviewed zero claim.

Current-image live-site Playwright `test:e2e:site` at `http://127.0.0.1:58080` completed in
`6.5s`: `4 passed, 21 skipped, 0 failed`. Applicable live scopes were five role workspaces axe,
CEO cockpit governance/source values, keyboard skip/nav order, and
integrations+communications Restricted/3 viewports. The 21 skipped scenarios were
harness-only by design; this is not all 25 live. The repo-external `0600` synthetic CEO
storage state was sourced in-process from Keychain, and temporary auth state/test-results
were deleted afterward.

Trivy filesystem and current locked-image scans exited `0`; report only `0` unwaived
High/Critical, `0` image secrets and `0` misconfigurations. The historical waiver set has
`57` entries covering `103` exact PURLs, expiring `2026-09-30`, and is not a total-findings
zero claim. The synthetic core is healthy on the current images; formal preflight returns
`rc78` solely because `local_pilot_go=false`. Email IMAP login/checkpoint/canary remains
unrun due missing working client authorization, DeepSeek response-reported model remains
`unknown`, and `production_go=false`/`local_pilot_go=false` remain unchanged.

The earlier `3060 passed, 44 skipped, 3 failed` result is retained in current closure evidence
as pre-doc-fix stale-current-doc mismatch only; the final run above closed that mismatch.

## Deferred by explicit scope decision

72 小时连续运行不再作为本阶段退出条件。It is deferred as an optional later
stability observation; evidence may record the actual canary runtime, but no fixed
72-hour duration is required for this phase.

This decision does not relax any channel, identity, model, outbound or production
control. In particular:

```text
credential_free_readiness=go
real_email_deepseek_canary=no_go
observed_model_identity=unknown
production_go=false
local_pilot_go=false
external_send=false
```

## Remaining real-canary work

- [x] Generate and store the local identity HMAC key and Frappe identity-resolver API
  key/secret in macOS Keychain without putting values in the repository or evidence.
- [ ] Securely provide an Email credential, DeepSeek API key and approved
  trusted-phrase lexicon through macOS Keychain; none may enter the repository or evidence.
- [ ] Generate a repository-external canary manifest enabling only one Email instance
  and model projection after an approved activation time; do not backfill history.
- [ ] Prove IMAP TLS, `BODY.PEEK`, UID/UIDVALIDITY checkpoints, attachment isolation and
  no read/move/delete behavior on new test messages.
- [ ] Prove unresolved participant → human review → confirmed User/Party mapping and
  stable second-message resolution without granting access from raw participant data.
- [ ] Prove model requests contain no raw email, phone, name or organization sentinel;
  record the returned model identity and stop on mismatch, invalid JSON or budget gate.
- [ ] Re-run live restart, provider failure, revocation, retention and emergency-stop
  drills; capture a new real-canary evidence package.

Only after those steps may the project declare
`Email + DeepSeek + identity resolution local shadow Go`. Kingdee, cloud deployment,
production, external send and formal business commands remain independently No-Go.
