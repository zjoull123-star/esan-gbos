# Gate 5 evidence summary

Status: **Technical Local Go for Gate 6 control implementation**.

Implementation commit
`cd00add47c72d35d1cf6e0c3971147342b205b74` passed the governed-metric,
read-only Kingdee boundary, projection, PostgreSQL, Frappe and browser checks.
This is a local synthetic-data result. It is not a real Kingdee canary,
Singapore preproduction deployment, privacy approval, UAT result or production
release.

## Verified local path

- The Metrics service accepts only three versioned metric keys and bounded
  windows. It does not accept SQL, expressions, table names, arbitrary URLs or
  model-generated calculations.
- Freshness, coverage, reconciliation and governed-source checks fail closed.
  Failed checks return `unavailable` without an official value or unit.
- Projection batches, lineage, checkpoints and query audit are site-scoped;
  four Metrics tables use forced RLS and append-only controls.
- The Kingdee adapter exposes `metadata.get` plus seven exact `.get` tools.
  Writer discovery, writer tools and mutation calls are zero. Live transport is
  disabled and cannot fall back to synthetic data.
- The CEO cockpit shows the source mode, window, as-of time, freshness,
  coverage, reconciliation and lineage. Synthetic values are visibly marked
  “演示 / 合成数据”.

## Verification results

- Repository suite: `833 passed, 12 skipped`. The twelve default skips are the
  opt-in PostgreSQL integration cases run by their dedicated commands.
- Gate 5 PostgreSQL integration: `2 passed` after repeated migrations.
- Frappe site integration: `34 passed` after two migrations on the hardened
  final image.
- Frontend unit tests: `77 passed`; lint, typecheck and production build passed.
- Playwright: `6 passed`; a separate authenticated live-browser check had zero
  console errors/warnings and no horizontal overflow at 375px.
- Guest Metrics BFF access returned HTTP 401. The authenticated local CEO view
  returned three available synthetic metrics.

The exact final image is
`sha256:e604fef4a0eb0a22bff399c2057add9ca03d991627c454803a2d015b19e1eb1f`.
It labels the implementation commit and source digest
`37cd2cfc8860fc5934b3c12c3f328fea16c73b3ad7508029f2229fc8442e9093`.
Nonessential `curl`, `libcurl` and `git` packages were removed from the runtime
after the current vulnerability database identified `CVE-2026-8458`; the exact
image then reported zero unwaived High/Critical findings.

## Independent entry-gate status

| Area | State |
|---|---|
| local technical readiness | `go` |
| live Kingdee canary | `blocked_external_input` |
| Singapore preproduction | `blocked_external_input` |
| formal Security Owner review | `pending_external_review` |
| privacy and cross-border review | `blocked_external_input` |
| business-owner UAT | `blocked_external_input` |
| production | `no_go` |

No real Kingdee startup, authentication, metadata query, business query or
mutation occurred. No Tencent Cloud resource was changed and no production
credential was loaded.

## Security limit

The exact image has zero **unwaived** High/Critical findings. However, the 57
time-bounded Gate 0/1 local-only waiver entries covering 103 exact PURLs still
expire on 2026-09-30 and explicitly block production unless remediated or
independently approved. Scanner success is therefore not production approval.

Gate 6 release-control, recovery, monitoring and governance assets may now be
implemented locally. Production remains No-Go until every external and human
entry gate has evidence.
