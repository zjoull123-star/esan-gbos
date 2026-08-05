# ESAN GBOS shared contracts

These JSON Schema 2020-12 documents are the boundary between channel
connectors, the Observer data plane, model/policy services, and the Frappe
business core.

## Contracts

- `CanonicalObservationEvent`: immutable, channel-neutral ingestion envelope.
- `EvidenceRef`: tamper-evident pointer to a source message, recording span, or
  document page.
- `ExtractedFact`: typed statement with confidence, provenance, and evidence.
- `DraftMutation`: policy-constrained create/update that can only produce an
  internal `AI Draft`.
- `ApprovedCommand`: human-authorized, idempotent internal state transition.
- `ConnectorCheckpoint`: durable cursor, replay window, and connector lease.

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

Run contract validation with:

```bash
uv run --group dev pytest tests/contracts
```
