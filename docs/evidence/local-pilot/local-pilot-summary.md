# Local pilot synthetic-core evidence snapshot

Captured at `2026-08-07T20:07:28Z` against source/runtime baseline
`64dc48cb5d6efb62c662f638d8da5f5c12c3a7de` (`64dc48c`). This is a redacted,
local synthetic snapshot; it is not a 72-hour pilot completion or final sign-off.

## Split verdict

| Scope | Result | Evidence boundary |
| --- | --- | --- |
| Formal production composition | **No-Go** | `composition.status=not_composed`, `local_pilot_go=false`; `preflight --require-go` returns 78. Production, real channels, real DeepSeek, Kingdee, and cloud deployment remain No-Go. |
| Synthetic core | **Observed local run** | Frappe/PWA, Context, Agent, and Observer ran on loopback with channels, models, media, and tunnel disabled. This does not change the formal gate. |

## Runtime and network

The verified loopback endpoints were PWA `127.0.0.1:58080`, Context
`127.0.0.1:58001`, Agent `127.0.0.1:58002`, Observer `127.0.0.1:58003`,
PostgreSQL `127.0.0.1:55432`, and MariaDB `127.0.0.1:53306`.

`local-internal` is a bridge with `enable_ip_masquerade=false`, not
`internal: true`. PWA, Context, Agent, and Observer container probes to
`api.deepseek.com:443` were blocked. `webhook-tunnel` remains internal.

## Image, site, and persistence

- Frappe image lock digest:
  `sha256:8e62faa8f76cf60348fde64c68e6b4820f6a602b9140f973bfffffb6efa87415`.
- `setup_complete=1`; versions are Frappe `16.30.0`, ERPNext `16.31.0`, CRM
  `1.81.0`, and `esan_gbos` `0.1.0`.
- Two consecutive migration runs were checksum-consistent. Materializer bootstrap
  was skipped/idempotent.
- The second fixture run skipped every record: 13 User, 5 Team, 500 each of CRM
  Organization/Contact/Lead/Deal and Party Profile, 240 each of Product Brief,
  Sample Project, Iteration, Shipment, Feedback, Demand and Sourcing, plus 280
  Work Item and 280 Review Case.

## Browser and verification snapshot

Playwright logged in as `synthetic.ceo@example.invalid` and reached `/gbos/ceo`,
showing “经营总览” and “演示 / 合成数据”. Viewports 375, 768 and 1440 had no
horizontal overflow; console errors and warnings were both 0. The cache contained
21 static precached entries and `cached=false` for API responses.

The mainline verification snapshot recorded pytest `2151 passed/37 skipped`,
ruff/format/mypy passing with 117 service source files, and frontend
lint/typecheck/Vitest 88/build passing. The primary agent must rerun the full suite
before final sign-off; these numbers are not a release certificate.

The first partial site failure was moved recoverably inside the data volume to
`.failed-gbos.localhost-20260808T033521`; it was not deleted.

## Limits and redaction

This evidence does not claim completion of a 72-hour pilot, production readiness,
real-channel delivery, real DeepSeek or Kingdee activity, cloud deployment, external
send, or business outcomes. Secrets, cookies, Keychain values, and full logs are
omitted. The JSON record is the machine-readable source; verify both compact files
with `shasum -a 256 -c SHA256SUMS`.
