# Local Shadow Pilot Infrastructure Implementation Plan

**Goal:** Provide an isolated, fail-closed local Compose boundary for the ESAN
GBOS real-channel and model shadow pilot without touching the synthetic
development stack.

**Architecture:** State services and monitoring run on a private internal
network with dedicated named volumes. Every capability that needs egress is
placed behind an explicit Compose profile and a runtime kill switch. The
governed manifest and its `contracts/local_pilot` schema are validated before
Keychain secrets are materialized or Compose can start.

## TDD delivery order

- [x] Add static and behavioral tests for isolation, profiles, loopback ports,
  secrets, preflight, emergency stop, monitoring, and operator documentation.
- [x] Run the focused suite and observe expected failures for missing assets.
- [x] Add the minimal Compose, preflight, lifecycle scripts, alert rules, and
  inert LaunchAgent template.
- [x] Run focused tests, shell syntax checks, Compose config, secret scan, and
  applicable Ruff checks.

Commit scope is restricted to `infra/local/**`, `scripts/local-pilot/**`,
`docs/local-pilot/**`, and `tests/infra/test_local_pilot_*.py`.

The runtime entrypoints are deliberately declared but not fabricated here.
Until their owning workstream supplies them, builds the fixed local runtime
image, and the operator explicitly preloads every locked infrastructure image,
preflight must return nonzero and `start` must not read credentials or run
containers. Compose uses `pull_policy: never`; startup cannot turn a missing
local image into an implicit registry download.
