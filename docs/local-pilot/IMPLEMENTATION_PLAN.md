# Local Shadow Pilot Infrastructure Implementation Plan

**Goal:** Run an isolated, fail-closed local GBOS shadow-pilot boundary for governed
channel ingestion, identity resolution and model proposals without enabling outbound
actions, Kingdee, cloud deployment or production state changes.

**Current validation reference:**
`ad58ab3ea8c0d521cebd90c2642709d135f98fac`. The locked Frappe/PWA image is
`sha256:22c3a2c129442588d0353c6a8f564aec593afc63b7354b4294d37aa9d40f7625`
and the local runtime image is
`sha256:6cc85a0e0f39e683af2f4fde15e93b706a76489831d18acd30f78867ec45cdee`.
Both images carry that exact runtime-code revision label. Later branch commits are
limited to image-lock, test and documentation successors and are not represented as
additional runtime code inside the images.

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
- [x] Current Frappe/PWA and runtime images built, inspected and recorded.
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
- [x] Current pre-canary evidence package records source/image bindings and zero
  external activity without modifying historical Gate evidence.

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

- [ ] Securely provide an Email credential, DeepSeek API key, identity HMAC key,
  approved trusted-phrase lexicon and Frappe identity-resolver API key/secret through
  macOS Keychain; none may enter the repository or evidence artifacts.
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
