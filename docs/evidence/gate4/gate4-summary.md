# Gate 4 evidence summary

Status: **Gate 4 Technical Local Go for governed Gate 5 implementation**.

Implementation commit
`36e86f571bcae0a8063ce6ee3d064beaf8d5317b` passed the deterministic
agent, policy, Context decision, human review, PostgreSQL, Frappe and PWA
verification matrix. This is a local synthetic-data decision. It is not a
real-model, Kingdee, cloud or production approval.

## Verified local path

- Sales, Purchase and Product agents run deterministically from synthetic
  context and can produce only internal Action Proposals.
- Action Guard is fail-closed. It has no external-send, quotation, order,
  Kingdee-write or general writer capability.
- Agent tasks have durable leases, retry/recheck, dead-letter, timeline,
  idempotency and same-site parent constraints in PostgreSQL.
- Context confirmation requires a human decision and persists an immutable
  Decision, exact proposal/evidence/fact relations and a versioned Verified
  Business Fact.
- `DraftMutation -> Review Case -> human decision` is enforced through the
  versioned BFF. Generic DocType writes cannot decide a case, and an assigned
  Reviewer cannot mutate the pinned subject through a second business role.
- The Review PWA supports assigned queues, frozen evidence, decision conflicts,
  keyboard use and 375/768/1440 responsive layouts.

## Verification results

- Repository suite: `672 passed, 10 skipped`. The ten default skips were the
  live PostgreSQL cases run separately.
- Gate 4 PostgreSQL integration: `3 passed`.
- Gate 3 regression PostgreSQL integration: `7 passed, 3 skipped,
  8 deselected`; the three skips are Gate 4 cases run by the Gate 4 command.
- Frappe site integration: `31 passed`.
- Frontend unit tests: `61 passed`; production build passed.
- Playwright: `5 passed`, including axe, responsive, keyboard and offline
  cache checks.
- Ruff, format, Mypy, secret scan, repeated migrations, image/source labels,
  health checks and `git diff --check`: passed.

The exact final image is
`sha256:5a3e4d924fbc7911d6f05b355a6090f139c5e433cfe90e8b59875485bd9dbcb2`
and labels the implementation commit plus source digest
`01b88f1bec0d59636080f9f28847826a3770839a5228a86975a5b894add78d00`.
The site reports Frappe 16.30.0, ERPNext 16.31.0, Frappe CRM 1.81.0 and
`esan_gbos` 0.1.0.

## Explicitly unavailable

| Capability | State |
|---|---|
| real model provider | `not_started` / No-Go |
| automatic external message or quotation | `not_available` / No-Go |
| Kingdee metadata or business query | `deferred_to_gate5` / No-Go |
| Kingdee write | `not_available` / No-Go |
| Tencent Cloud runtime | `not_started` / No-Go |
| production release | `not_started` / No-Go |

No real model, external channel, Kingdee, cloud or production credential was
loaded. Model-provider calls, Kingdee calls, external messages and formal
business writes were zero inside the tested capability boundary.

## Gate decision and limits

Gate 5 governed metrics, read-only Kingdee adapter and local pre-production
work may start. Real Kingdee authentication and queries remain
`blocked_external_input` until the restricted account and endpoint are
provided. Formal Security, Privacy/Legal and business-owner review remains
pending, so real personal data, external channels, cloud deployment and
production remain No-Go.
