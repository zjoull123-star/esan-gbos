# ADR-0001: 数据权威与模块架构

- Status: Gate 0/1 decision; runtime evidence is pending
- Date: 2026-08-06
- Scope: ESAN GBOS v1

## Context

GBOS combines operational workflow, communication observation, AI-derived
facts, and ERP data. Treating every copy as authoritative would allow an AI
suggestion, a stale import, or an observer replay to become a financial fact.
The contracts already separate observations, evidence, drafts, and approved
commands; the module boundaries must make that separation enforceable.

## Decision

1. **Kingdee is authoritative for formal orders, inventory, and finance.** GBOS
   may read or snapshot those values, but cannot redefine them locally.
2. **Frappe CRM and `esan_gbos` are the operational system of engagement.**
   Frappe CRM owns Organization, Contact, Lead, and Deal. `esan_gbos` owns
   product requirements, samples, sourcing coordination, work items, review
   cases, and GBOS workflow state. ERPNext is installed for ecosystem
   compatibility, but its Sales Order, Purchase Order, Stock, and GL
   transaction DocTypes are hidden or creation-disabled in V1 and never become
   a second ledger.
3. **Observer is a separate data plane.** It owns raw communication objects,
   canonical observation events, connector checkpoints, and evidence pointers.
   Frappe receives references and reviewed derived facts, not an implicit raw
   archive.
4. **AI output is derived, never authoritative.** Extracted facts retain
   model metadata and evidence; mutations are `AI Draft` only until a person
   issues an approved command.
5. **Contracts are the only cross-module write boundary.** Events and
   evidence use `canonical-observation-event.schema.json` and
   `evidence-ref.schema.json`; AI proposals use
   `extracted-fact.schema.json` and `draft-mutation.schema.json`; formal
   transitions use `approved-command.schema.json`.
6. Every object and command carries an explicit `site_id`. Cross-site reads,
   writes, joins, and exports are denied by default.

## Consequences

- A value copied from Kingdee or Observer must display its source, timestamp,
  and evidence/reference status.
- Reconciliation is an explicit workflow; it is not a silent overwrite.
- The module owners can evolve storage independently, but a contract change
  requires the version-freeze process in ADR-0006.
- Gate 0/1 remains fixture/mock-only for production channel ingestion, real
  Kingdee access, and real model calls.

## Verification gate

The disposable compatibility smoke must prove the locked applications install
and migrations pass. Integration tests must also prove that a draft cannot
write a formal field and that an approved command is idempotent. Passing tests
do not imply production data access is enabled.
