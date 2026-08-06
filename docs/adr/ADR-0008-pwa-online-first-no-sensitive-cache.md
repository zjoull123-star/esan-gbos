# ADR-0008: PWA 在线优先且不缓存敏感数据

- Status: Gate 0/1 decision; implementation evidence is pending
- Date: 2026-08-06

## Context

An installable PWA improves reach on mobile devices, but browser caches and
offline storage are difficult to revoke on shared or lost devices. GBOS handles
communication, evidence, and financial-adjacent workflow data.

## Decision

- The PWA is **online-first**. A network check precedes authenticated data
  reads and formal actions; loss of connectivity fails closed with a clear
  retry state.
- The service worker may cache only the static app shell and explicitly
  non-sensitive, versioned assets. It must not cache API responses, raw
  communications, evidence, extracted facts, drafts, orders, inventory,
  finance data, tokens, or personal identifiers.
- Sensitive data must not be written to Cache Storage, `localStorage`,
  `sessionStorage`, IndexedDB, WebSQL, or an offline queue. Keep it in memory
  for the active view and clear it on logout, timeout, or site change.
- API responses use no-store/private cache headers where sensitive; service
  worker fetch handlers default to network-only for authenticated routes.
- Offline install is not an offline business mode. Any future offline feature
  needs a new ADR, device-loss threat review, explicit data scope, and revocation
  proof.

## Consequences

- Users cannot review sensitive records or issue commands without a live,
  authorized session.
- The shell can load during an outage, but it must not imply that stale data is
  current or that a command was accepted.
- Browser QA and security review are required in addition to API tests.

## Verification gate

Inspect the service-worker manifest and browser storage after login, record
view, draft, export, logout, and failed-network flows. Evidence must show no
sensitive payload or token remains. A static shell cache is not production data
availability evidence.
