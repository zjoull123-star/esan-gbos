# Local pilot responsive-interface evidence snapshot

Captured at `2026-08-08T20:12:31Z` against validation commit
`2b775ddd7aab116d6da288ac0c51efc999333a3e` (`2b775dd`), immediately before this
evidence update. This is a redacted local validation snapshot; it is not a
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
  `sha256:fdbaf8af7da81958de22798e33d9bade3c7c09d57c59faa69d39b56ab4e99542`.
  The image was built from application commit `9e0a0aa698de01c2ba0e87775e203d09fddcf305`;
  later validation-reference commits changed only acceptance tests, formatting,
  and the image lock.
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
showing “经营总览” and “演示 / 合成数据”. The controlled frontend matrix covered
320, 375, 720, 768, 1024 and 1440 pixel widths with no horizontal overflow.
The current browser run asserted zero console errors; warnings were not asserted.
The build generated 51 static precache entries and no API response was cached.

The final local validation snapshot recorded:

- Backend: pytest `2163 passed, 37 skipped, 1 warning`; no failures. The 37
  opt-in PostgreSQL integration tests were not enabled and are not counted as passed.
- Python static checks: ruff all green; format check passed for 453 files; mypy
  passed for 117 service source files.
- Frontend: lint, typecheck, Vitest `172`, and build all green.
- Frontend harness Playwright: `21 passed`.
- Real local Frappe site Playwright: `4 passed, 17 skipped`; every skip was a
  harness-only case and is reported separately rather than as passed.

An additional real-site role smoke used ephemeral synthetic sessions for CEO,
Sales User, Purchase Manager, Buyer, Product/R&D, Reviewer and Integration Admin.
All seven role cases passed: CEO saw all seven first-level menus and opened Party,
Sample and Review detail routes; six normal-role denial routes were enforced; and
console, page, request and API errors were all zero. The temporary Integration Admin
role was removed and all cookie-bearing storage-state files were deleted afterwards.

The Frappe-site run passed five-workbench axe checks, 375/768/1440 viewport checks,
keyboard ordering, CEO synthetic values and provenance, and disabled-channel empty
states. The controlled frontend harness passed the full responsive matrix, no API
cache or GBOS business storage, and offline fail-closed behavior; the real-site role
smoke independently confirmed no API cache or GBOS business storage. These are local
validation assertions, not production or business-outcome evidence.

## Security scan sequence

The earlier local-pilot snapshot's initial Trivy scan found three High findings in
`cryptography 46.0.7`. The dependency was upgraded and locked to `50.0.0`. The
subsequent Trivy scan across
uv, pnpm, npm, `infra/dev/Containerfile.final`, and
`infra/local/Containerfile.runtime` reported 0 vulnerabilities, 0 secrets, and 0
misconfigurations with exit 0. The dependency/container scan was not rerun during
this responsive-interface certification, so it is carried-forward context rather
than current-HEAD scan evidence. `scripts/dev/secret-scan` was rerun and reported
no supported secret patterns. Neither result grants production authorization.

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
