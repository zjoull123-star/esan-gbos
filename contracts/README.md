# ESAN GBOS shared contracts

These JSON Schema 2020-12 documents are the boundary between channel
connectors, the Observer data plane, model/policy services, and the Frappe
business core.

The normative Gate 2–6 evolution is defined by
[`GBOS v4 design`](../docs/superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md)
and
[`ADR-0009`](../docs/adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md).

## Contracts

- `CanonicalObservationEvent`: immutable, channel-neutral ingestion envelope.
- `EvidenceRef`: tamper-evident pointer to a source message, recording span, or
  document page.
- `ExtractedFact`: typed statement with confidence, provenance, and evidence.
- `DraftMutation`: policy-constrained create/update that can only produce an
  internal `AI Draft`.
- `ApprovedCommand`: human-authorized, idempotent internal state transition.
- `ConnectorCheckpoint`: durable cursor, replay window, and connector lease.
- `bff-v1.openapi.json`: the eight frozen Gate 1 BFF methods. Each operation
  declares its concrete response DTO and the real Frappe HTTP wire wrapper
  (`{"message": {"data": ..., "meta": ...}}` or
  `{"message": {"error": ...}}`).

## Cross-record uniqueness

The schemas validate wire shapes. Storage adapters must additionally enforce:

| Domain | Unique key |
|---|---|
| Provider event | `site_id + connector + provider_event_id` |
| Provider event without an ID | `site_id + raw_sha256 + occurred_at bucket` |
| External system mapping | `system + account_set + object_type + external_id` |
| Command replay protection | `site_id + idempotency_key` |

The observation store owns event/checkpoint uniqueness. Frappe owns
idempotency for business commands and External Crosswalk uniqueness.

## Trust boundary

- Evidence and model output are untrusted inputs.
- A `DraftMutation` cannot approve itself or modify formal stage, outcome,
  price, discount, outbound communication, or order fields.
- An `ApprovedCommand` is only valid after the command service verifies the
  authenticated human, Review Case, expected revision, and payload digest.
- No schema or scope in Gate 0/1 enables a Kingdee write.

## Gate 2 planned contract evolution

Gate 2 must add or extend contracts for:

- durable Agent Task, lease/budget/retry and Agent Timeline;
- Evidence Record aggregation, Fact Proposal/version, Conflict and Decision;
- Action Proposal/Approval/Execution/Verification;
- Metrics response, definition version, source, `as_of`, freshness, coverage,
  reconciliation and unavailable reason;
- Kingdee white-listed read projection, query budget and Crosswalk.

Before adding a schema, Gate 2 must map it to the existing six contracts and
prove that it is not a semantic duplicate. These are planned contracts, not
Gate 0/1 runtime capabilities. Gate 2–4 Kingdee adapters remain mock-only and
must make zero real network calls.

Run contract validation with:

```bash
uv run --group dev pytest tests/contracts
```
