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
The actual UI is a Frappe PWA and its local composition is not present. Until
the owning workstreams provide that composition, a runtime Containerfile, the
declared entrypoints, and locally inspected digest-locked images, preflight
must return nonzero and `start` must not read credentials or run containers.
Compose config success is syntax validation only. It is not runtime evidence.
