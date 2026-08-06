# ADR-0005: Observer 与 Frappe 数据分离

- Status: Gate 0/1 decision; Gate 3 service boundary is a placeholder
- Date: 2026-08-06

## Context

Communication content is high-volume, often personal, and may arrive through
providers with different consent and replay semantics. Frappe is the workflow
surface and should not become an unbounded raw-message archive.

## Decision

- Observer is a separate service/data plane for connector intake, raw objects,
  canonical observation events, evidence references, and connector
  checkpoints. It owns ingestion lifecycle and quarantine decisions.
- Frappe stores tenant-scoped business workflow, reviewed facts, drafts,
  review cases, and references to Observer evidence. It does not directly
  reach into raw provider storage as an ordinary ORM operation.
- The boundary is contract-driven: use
  `canonical-observation-event.schema.json`, `connector-checkpoint.schema.json`,
  `evidence-ref.schema.json`, and `extracted-fact.schema.json`.
- Observer may publish an event or a proposed fact; it may not approve a
  command or mutate a formal Frappe document. Frappe may request a permitted
  evidence view, subject to role, consent, retention, and tenant checks.
- Gate 0/1 has no production connector, raw-message store, or live model path.
  `services/observer/README.md` records only the future Gate 3 boundary.

## Consequences

- Evidence lookup can be asynchronous and may fail closed rather than leak
  data into an unrelated site.
- Retention, deletion, legal hold, and export require coordinated receipts
  across both data planes.
- Operational monitoring must distinguish connector health from Frappe
  workflow health; one cannot be inferred from the other.

## Verification gate

Before Gate 3, prove tenant partitioning, replay handling, consent basis,
retention enforcement, evidence hash checking, and deny-by-default access from
Frappe to raw content. No service implementation is implied by this ADR.
