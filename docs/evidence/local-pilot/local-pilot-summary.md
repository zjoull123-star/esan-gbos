# Local pilot final local-validation evidence snapshot

Captured at `2026-08-07T20:18:53Z` against validation commit
`9d64a7860adc6692d4321439ea3295ff9dc45cd6` (`9d64a78`), immediately before this
evidence-only commit. This is a redacted local validation snapshot; it is not a
72-hour pilot completion, production approval, or final release sign-off.

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

## Browser and final onsite validation

Playwright logged in as `synthetic.ceo@example.invalid` and reached `/gbos/ceo`,
showing “经营总览” and “演示 / 合成数据”. Viewports 375, 768 and 1440 had no
horizontal overflow; console errors and warnings were both 0. The cache contained
21 static precached entries and `cached=false` for API responses.

The final local validation snapshot recorded:

- Backend: pytest `2155 passed, 37 skipped, 1 warning`; no failures.
- Python static checks: ruff all green; format check passed for 446 files; mypy
  passed for 117 service source files.
- Frontend: lint, typecheck, Vitest `88`, and build all green.
- Frontend harness Playwright: `7 passed`.
- Real local Frappe site Playwright: `6 passed, 1 skipped`; the only skipped item
  was the pure-frontend route sentinel.

The real-site check used an Administrator storage state only as temporary test
authorization. Cookies were cleared afterwards and the storage-state file was
overwritten; no state was retained as evidence.

The Frappe-site run also passed five-workbench axe checks, 375/768/1440 viewport
checks, keyboard ordering, CEO synthetic values and provenance, disabled-channel
empty states, no API cache or GBOS business storage, and offline fail-closed
behavior. These are local validation assertions, not production or business-outcome
evidence.

## Security scan sequence

The initial Trivy scan found three High findings in `cryptography 46.0.7`. The
dependency was upgraded and locked to `50.0.0`. The subsequent Trivy scan across
uv, pnpm, npm, `infra/dev/Containerfile.final`, and
`infra/local/Containerfile.runtime` reported 0 vulnerabilities, 0 secrets, and 0
misconfigurations with exit 0. `scripts/dev/secret-scan` also exited 0. This is a
local scan result and does not grant production authorization.

The first partial site failure was moved recoverably inside the data volume to
`.failed-gbos.localhost-20260808T033521`; it was not deleted.

## Limits and redaction

This evidence does not claim completion of a 72-hour pilot, production readiness,
real-channel delivery, real model or Kingdee activity, cloud deployment, external
send, or business outcomes. Formal composition remains `not_composed`,
`local_pilot_go=false`, and the formal preflight remains No-Go (exit 78). Secrets,
cookies, Keychain values, storage-state contents, and full logs are omitted. The
JSON record is the machine-readable source; verify both compact files with
`shasum -a 256 -c SHA256SUMS`.
