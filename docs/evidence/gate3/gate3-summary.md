# Gate 3 evidence summary

Status: **Gate 3 Technical Local Go for deterministic Gate 4 implementation**.

Implementation commit
`d7f28317c2c14677381b0912b8c6c0fcaf519138` passed the local synthetic
Observer, Context and PostgreSQL verification matrix. This is a narrow
technical local Go. It is not a production, real-channel, real-model, Kingdee
or cloud approval.

## Verified local path

- A signed `manual_import` request accepts only explicitly synthetic, bounded
  UTF-8 text or JSON fixtures.
- The Observer writes immutable content-addressed evidence, event metadata,
  participant, checkpoint, processor lineage and derivation records.
- PostgreSQL 17 + pgvector 0.8.2 uses two non-superuser/non-BYPASSRLS
  application roles and forced row-level security.
- Context publication persists Evidence first, then a proposal-only Fact and
  Entity Resolution Proposal with stable IDs, provenance and idempotency.
- One live local synthetic flow returned HTTP 200 and database verification
  observed one event, one Context Evidence, one Fact Proposal and one Entity
  Resolution Proposal.
- Migration replay, checkpoint ordering, dead-letter, cross-site denial,
  backup/restore, legal hold, deletion receipt, prompt injection and metadata
  redaction tests passed.

## Verification results

- Repository suite: `478 passed, 7 skipped`. The seven default skips are the
  PostgreSQL-marked cases that were run separately against the pinned local
  container.
- PostgreSQL integration: `7 passed, 8 deselected`.
- Contract suite: `184 passed`.
- Observer suite: `24 passed`; Context suite: `36 passed`.
- Gate 3 infrastructure and HTTP API: `14 passed`.
- Ruff, format, Mypy, secret scan, Compose validation, historical checksums
  and `git diff --check`: passed.
- The upstream Starlette TestClient reports one httpx deprecation warning; no
  functional test failed.

## Explicitly unavailable

| Capability | State |
|---|---|
| external channel canary | `not_started` |
| real model provider | `not_started` / No-Go |
| Kingdee metadata or business query | `not_started` / No-Go |
| cloud runtime or object storage | `not_started` / No-Go |
| production release | `not_applicable` / No-Go |

No external channel, real model, Kingdee, cloud or production credential was
loaded. External network calls, Kingdee calls, model-provider calls, external
messages and Frappe business writes were all observed as zero inside the tested
capability boundary.

## Gate decision and limits

Gate 4 local deterministic implementation may start. The external channel
canary remains `not_started` and is not represented as complete.

The local filesystem is not production object storage, the host Python
processes are not a dedicated digest-pinned service image, and a general
malware scanner is not present. Formal Security Owner and Privacy/Legal Owner
reviews remain pending. Therefore real data, real channels, real models,
Kingdee, cloud and production all remain No-Go.
