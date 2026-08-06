# Gate 1 evidence summary

Status: **pass for the local disposable Gate 1 boundary**.

Go/No-Go: **Gate 1 Go** after the pull-request head is green. Gates 2–6 were
not started and remain **No-Go**.

## Immutable runtime

- Image: `esan-gbos-final:gate1`, Linux ARM64, created
  `2026-08-06T10:52:25.274279038+08:00`.
- Digest:
  `sha256:a55e3dc432cabc7e4a1bbe4951d1586c97e65151b41a5d9c7e5eb0632d61f1e9`.
- Runtime source commit:
  `deccc2caaa2d25cebceab2aff99dbbbb4e037a04`.
- App source archive hash:
  `b6da2818182774b2246e4429c519a936de09ea40630d4985e865d3ccd5776339`.
- Fresh site `gbos-final.localhost` installed exactly `frappe`, `erpnext`,
  `crm`, and `esan_gbos`; two consecutive migrations exited 0.
- The final image contains Python 3.14.2 and a minimal Node 24.13.0 realtime
  runtime. Frontend build dependencies and upstream app `node_modules` are
  absent from the final runtime.

## Business, BFF, and permission evidence

- Deterministic fixtures materialized 500 CRM organizations, contacts, leads,
  deals and Party Profiles; 240 product/sample/demand/sourcing chains; 280
  Work Items; and 280 Review Cases.
- Rerunning the seed inserted 0 records and skipped the exact expected counts.
- The Frappe suite ran 21/21 integration tests covering team isolation,
  list/document parity, CRM link scoping, Reviewer and Privacy/Audit denial,
  composite external keys, state machines, revision, idempotency, and the
  ERPNext transaction guard.
- Real HTTP verification observed: guest 401, list 200, idempotent replay 200,
  conflicting idempotency key 409, stale revision 409, missing CSRF 400, wrong
  method 405, and Reviewer write denial 403.
- The HTTP smoke created one additional synthetic Sample Project. Backup and
  restore therefore correctly observed 241 samples, while Party Profiles
  remained 500 and Work Items remained 280.
- Sales Order, Purchase Order, Stock Entry, and GL Entry counts were 0 before
  and after the GBOS flow and after restore.

## Frontend and browser evidence

- Repository Python suite: 200 passing tests.
- Frontend: ESLint, strict typecheck, 56 unit tests, production build, and the
  5-test Playwright harness pass.
- Role-specific live checks passed for CEO, Sales User, Buyer, Product/R&D,
  and Reviewer. Each user saw only its permitted workspace navigation.
- The five live pages had no Critical/Serious axe result, no console/page/HTTP
  error, and returned the required CSP.
- Sales at 375, 768, and 1440 pixels had no horizontal overflow. The first
  keyboard focus was “跳到主要内容”.
- Service Worker was active; localStorage, sessionStorage, IndexedDB, and
  business API cache entries were empty. Going offline changed visible records
  from 25 to 0 and displayed “需要联网”.
- Screenshots under [screenshots](screenshots/) contain only synthetic fixture
  data and cover the five workspaces plus the 375-pixel sales layout.

## Query performance

Local warmed p95 across 50 requests per endpoint:

| Query | p95 |
|---|---:|
| `party.get_360` | 11.68 ms |
| `work_item.list` | 6.85 ms |
| `sample.get_status` | 6.38 ms |
| `sourcing.get_board` | 8.80 ms |

All are below the Gate 1 local threshold of 800 ms.

## Security, SBOM, and recovery

- Final-image security scan exited 0 with 0 unwaived High/Critical findings.
  Scoped Gate 0/1 exceptions remain visible and continue to block Gate 5/6.
- Security log SHA-256:
  `f6da3de94994021fca0ff4f2d25f12653ca7fff71f80f7fb8fe5115eb943439d`.
- CycloneDX 1.7 SBOM: 1,570 components; SHA-256
  `fa70d8bd0d13f8fa0e02ee1586e17fb09aba7466eda34df393362406519d8544`.
- License inventory SHA-256:
  `ba56c022cecef8dfb5595619b76a2d77f5bb84dfa8c204f42e25da3bc2b0ecb4`.
- The compressed synthetic database backup SHA-256 is
  `86d7d2585def1bc2015d058e6c2490c92991319de3fc715285cff23346db420b`.
  Restore into a separate clean four-App site succeeded, followed by migration
  and exact count checks. Public/private file archives were also restored.

## Limitations and downstream blocks

- The visible upstream `duckdb_sync.cleanup_old_syncs` warning remains open;
  migrations still exit 0.
- The private GitHub repository cannot enable rulesets on the current account
  plan. Main-agent-only merge and a green head commit are mandatory.
- The formal image is a local ARM64 artifact, not a published registry image.
  Pull requests build an ephemeral `linux/amd64` image for the fresh-site
  smoke without pushing it. No registry publication or production deployment
  was authorized.
- No Kingdee request, channel ingestion, real model invocation, automatic
  external message, or production network write occurred.
- Gate 2 requires a separately authorized read-only Kingdee adapter. Gate 3
  requires compliant channel selection. Gate 4 requires the
  `DraftMutation → Review Case → ApprovedCommand` flow. Gate 5/6 remain
  blocked by their own security, privacy, infrastructure, UAT, and release
  evidence.

Raw logs, backups, and multi-megabyte SBOM files are intentionally not
committed. CI uploads bounded-retention artifacts. Review and CI status live
on [PR #1](https://github.com/zjoull123-star/esan-gbos/pull/1).
