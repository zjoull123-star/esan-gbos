# ADR-0003: 金蝶 V1 只读且无写工具

- Status: Gate 0/1 decision; runtime evidence is pending
- Date: 2026-08-06
- Sequencing: live read canary moved to Gate 5 by ADR-0009

## Context

Kingdee remains the source of truth for orders, inventory, and finance. An
agent-facing write endpoint could turn an ambiguous extraction or prompt
injection into a formal business transaction.

## Decision

- V1 exposes **read-only** Kingdee metadata/query capabilities only. The
  Gate 0/1 connector is mock/fixture-backed; production credentials and live
  account-set data are disabled.
- No MCP or application tool may create, update, submit, approve, delete, or
  reverse a Kingdee bill. No generic HTTP or SQL escape hatch is permitted.
- Read results are snapshots with source, query time, tenant/account-set
  reference, and evidence status. A snapshot never replaces Kingdee authority.
- A business user may prepare an `AI Draft` in GBOS. Any later formal change
  must follow ADR-0004 and a separately approved integration design; it cannot
  be smuggled through a “read” method.
- Secrets are supplied at runtime by an approved secret mechanism and are
  never committed, copied into fixtures, or returned in logs.

## Consequences

- Users must complete formal ERP actions in Kingdee during V1.
- Reconciliation and stale-read handling are visible workflows, not silent
  retries or local overrides.
- Enabling a live read canary requires consent/data review, least-privilege
  credentials, audit evidence, and a Gate 5 preproduction decision. Gate 2 is
  contract/mapping/mock only; Gate 3/4 must have zero Kingdee network traffic.
  Live access is not a Gate 0/1 completion criterion.

## Verification gate

Tool discovery tests must show an allow-list containing no write operation.
Negative tests must prove write-shaped calls are rejected before network
access. The Gate 5 live read canary, if later approved, must separately prove
startup, authentication, metadata, and a white-listed business query. It does
not authorize writes.
