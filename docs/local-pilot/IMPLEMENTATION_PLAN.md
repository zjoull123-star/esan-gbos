# Local Shadow Pilot Infrastructure Implementation Plan

**Goal:** Run an isolated, fail-closed local GBOS shadow-pilot boundary for governed
channel ingestion, identity resolution and model proposals without enabling outbound
actions, Kingdee, cloud deployment or production state changes.

**Current source-bound image references:** Frappe source reference
`35beb2586f12043ce4b89b6875527ec4a75150b9`, runtime source reference
`1fd20d4df930fc9a70168453d29be1c9dc192522`, and image-lock recording commit
`54d9aa7866189d5fe2028aeea177f6cff8102b41`. The locked Frappe/PWA image is
`sha256:0b0e24d7e25c2e384e977c1aa00ef8d032e54aadbb84af813fb077c58fd28460`;
the local runtime image is
`sha256:489ad22e95300ec27156904d583f67979cf8142f8b31479d8b938ad3d3a6c0b1`.
Their source SHA256 labels are respectively
`f6fe3ab3938890e6d041df03bfd5857528c8e1269a631b38d6bbb527978c959d` and
`e946cdf903d87b9d387107b82801556ad85994cb7e5702c21854eebda804fd3e`; each image carries
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
- [x] The current Frappe/PWA and runtime source was rebuilt, inspected and recorded; the
  synthetic core was restarted from those images. Real channels, models, sends and terminal
  material deletion remain disabled.
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
- [x] Email Gateway 独立邮箱地址录入已闭环：Observer 以统一 Secret Provider 读取
  32-byte HMAC key，只持久化 opaque mailbox identity；v1 兼容、v2 revision/digest、
  legacy fail-closed、PWA 一次性清空和 participant-authority 重算均已验证。
- [x] The earlier disposable PostgreSQL matrix passed 43 tests; the current runtime
  source additionally passed isolated three-role migration/start-guard/chain SQL.
  Fresh current-image Frappe v16 identity/app native suites passed 13/59 tests after
  two migrations; all disposable state was removed.
- [x] Current pre-canary evidence package records source/image bindings and zero
  external activity without modifying historical Gate evidence.

The current credential-free source run records full backend
`3989 passed, 59 skipped, 1 warning`, failed `0`; the warning is the existing Starlette
TestClient/httpx deprecation. Ruff check/format (`720 files`), CI-scope mypy
(`151 sources`), compileall, and `scripts/dev/secret-scan` are green. Frontend
lint/typecheck/build are green, with unit
`232 passed` and frontend-harness Playwright `29 passed`. The disposable PostgreSQL `--all`
gate passed Observer/Context `3` tests with 16 deselected and one existing warning, plus
`2` Gateway tests, and removed its container.

The `test:e2e:site` result at `http://127.0.0.1:58080` (`4 passed, 21 skipped, 0 failed` in
`6.5s`) is historical-only and is **not rerun on the current source-bound images**. The prior
21 skipped scenarios were harness-only by design, so that snapshot was not all 25 live and
is not current proof.

Trivy filesystem and current locked-image scans exited `0`; report only `0` unwaived
High/Critical, `0` image secrets and `0` misconfigurations. The historical waiver set has
`57` entries covering `103` exact PURLs, expiring `2026-09-30`, and is not a total-findings
zero claim. The synthetic core is healthy on the current images; formal preflight returns
`rc78` solely because `local_pilot_go=false`. Email IMAP login/checkpoint/canary remains
unrun because formal go, activation-time/checkpoint control and provider validation have not
completed; local credential presence alone is insufficient. DeepSeek response-reported model
remains `unknown`, and `production_go=false`/`local_pilot_go=false` remain unchanged.

The earlier `3060 passed, 44 skipped, 3 failed` result is retained in current closure evidence
as pre-doc-fix stale-current-doc mismatch only; the final run above closed that mismatch.

The 2026-08-14 mailbox-identity source closure is a newer, separate verification slice:
backend `3630 passed, 48 skipped, 1 warning`, frontend unit `217 passed`, frontend-harness
Playwright `27 passed`, and lint/typecheck/build/Ruff/format/mypy (`130 sources`)/compileall
all green. A disposable PostgreSQL run applied the Observer chain and Email Gateway 001–009
twice and passed `3 + 2` focused real-DB tests; an isolated Frappe v16 site completed install,
two migrations and five native email/identity test modules. All disposable state was removed.
No provider, model or external-send network was used. These earlier mailbox-identity changes
are now included in the current source-bound images; a later source-group change still requires
a clean-source rebuild, image-lock refresh and attestation before canary or deployment.

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
- [x] Store the local Email, DeepSeek, trusted lexicon and runtime credential references in
  macOS Keychain without putting values in the repository or evidence. Presence is metadata,
  not provider-validity or schema proof.
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
- [ ] Resolve the WeCom `errcode=45009` governance contract explicitly. Official material does
  not prove HTTP 429/`Retry-After`; Tasks 7–9 remain stopped until the exact pause/manual-recovery
  decision is approved.
- [ ] Obtain an auditable outbound idempotency/receipt/status-reconciliation contract before
  Task 18. Task 19 remains prohibited and `external_send=false` remains mandatory.

The provider-independent emergency-stop boundary is closed: command publication and send
workers dynamically re-read `/run/gbos/EMERGENCY_STOP` before their effects, and containment
stops/verifies all seven effect-producing Email Gateway workers while preserving the API for
forensics. This does not enable external send or relax any kill switch.

Only after those steps may the project declare
`Email + DeepSeek + identity resolution local shadow Go`. Kingdee, cloud deployment,
production, external send and formal business commands remain independently No-Go.
