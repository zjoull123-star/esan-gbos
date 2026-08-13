# ESAN GBOS shared contracts

These JSON Schema 2020-12 documents are the boundary between channel
connectors, the Observer data plane, model/policy services, and the Frappe
business core.

The normative Gate 2–6 evolution is defined by
[`GBOS v4 design`](../docs/superpowers/specs/2026-08-06-gbos-v4-agent-context-roadmap-design.md)
and
[`ADR-0009`](../docs/adr/ADR-0009-four-truths-agent-context-and-gate-sequencing.md).

## Contracts

### Frozen Gate 0/1 primitives

- `CanonicalObservationEvent`: immutable, channel-neutral ingestion envelope.
- `EvidenceRef`: tamper-evident pointer to a source message, recording span, or
  document page.
- `ExtractedFact`: typed Fact Proposal with confidence, provenance, and
  evidence. It remains a proposal and is not a verified business fact.
- `DraftMutation`: policy-constrained create/update that can only produce an
  internal `AI Draft`.
- `ApprovedCommand`: human-authorized, idempotent internal state transition.
- `ConnectorCheckpoint`: durable cursor, replay window, and connector lease.
- `bff-v1.openapi.json`: the eight frozen Gate 1 BFF methods. Each operation
  declares its concrete response DTO and the real Frappe HTTP wire wrapper
  (`{"message": {"data": ..., "meta": ...}}` or
  `{"message": {"error": ...}}`).

The six `*.schema.json` primitives above remain at schema version `1.0`.
Gate 2 does not add required fields to them.

### Local channel/model pilot contracts

Additive local-pilot contracts are isolated under [`local_pilot/`](./local_pilot/).
They do not modify or activate the frozen Gate 0/1 contracts:

- `CanonicalObservationEvent v1.1` and `ConnectorCheckpoint v1.1` add
  connector-instance identity; the checkpoint also adds an optimistic version.
- `InboundDelivery v1.0`, `ModelInvocation v1.0`, and
  `TokenizationReceipt v1.0` are content-minimized receipts. They exclude raw
  bodies, prompts/responses, secrets, PII, and plaintext token mappings.
- Sales, Purchase, Product, and CEO proposal schemas are closed,
  internal-only output shapes. They cannot add external sends, formal
  commercial commitments, orders, Won/Lost outcomes, or official KPI fields.
- `External Identity Resolution v1.0` is a closed, minimum projection of a
  confirmed or revoked Frappe external-identity mapping. Its provider subject
  is an opaque `extid:v1:<provider>:<43-char unpadded base64url SHA-256 digest>`
  reference; `target_ref` remains a
  permission-protected Frappe authority reference and may be email-shaped when
  it is a Frappe `User.name`.
- Canonical valid and intentionally invalid examples live under
  `local_pilot/examples/valid/` and `local_pilot/examples/invalid/`.

These documents define provider-neutral pilot boundaries only. They do not
enable a connector, model provider, network call, external send, or storage
migration.

### Email Gateway contracts

The additive JSON Schema 2020-12 contracts under
[`email_gateway/`](./email_gateway/) freeze the provider-neutral boundaries
between Observer, the independent Email Gateway, and Frappe authority:

- `EmailMessagePublication v1.0` is Observer's content-minimized durable
  publication. It carries only opaque role-tagged participants, bounded subject
  projection or digest, header digests, EvidenceRefs, and revision-pinned
  Observer/mailbox references. It never carries a raw address, provider ID,
  header value, body, attachment, EML, cursor, or secret.
- `MailboxConnectorProjection v1.0` is the Gateway-to-Observer connector
  configuration boundary. Its activation watermark owns the exact mailbox ID
  and positive mailbox config revision that introduced the lower bound;
  candidates before that bound are not eligible for delivery or CAS intake.
- `FrappeIdentityProjection v1.0` is a minimum, versioned projection of the
  existing Frappe external-identity authority. It reuses the governed GBOS
  processing-purpose and opaque `extid:v1:email:*` vocabularies, admits only
  `User` or `Party` mappings, and exposes no raw address, provider subject,
  target ref, or display content.
- `FrappeRouteAuthority v1.0` is a closed `oneOf`: either a complete,
  revision-pinned assigned route or an unassigned result with one fixed safe
  reason code. Partial and hybrid routes are invalid.
- `EmailAddressMatchAttestation v1.0` records only an opaque address ref,
  opaque candidate target, EvidenceRef, frozen normalization version, boolean
  match, bounded timestamps, and digest. Neither compared address is returned.

Named synthetic valid and intentionally invalid cases for all five schemas are
in [`email_gateway/examples/provider-neutral-v1.json`](./email_gateway/examples/provider-neutral-v1.json).
At each internal API boundary, the consumer must additionally bind the schema's
site, team, and processing-purpose fields to the authenticated request scope;
wire-shape validation alone is not cross-request authorization.
These contracts do not enable a provider adapter, connect to Frappe, prove a
live mailbox, send email, or claim end-to-end integration.

### Gate 2 aggregate contracts

- `common.schema.json`: shared definitions for identifiers, temporal fields,
  evidence, lineage, metrics and the internal Action allow-list.
- `EvidenceRecord`: governed aggregate around a nested v1 `EvidenceRef`.
  `EvidenceRef` remains the immutable pointer.
- `VerifiedBusinessFact`: confirmed, versioned fact tied to evidence and the
  decision that confirmed it.
- `ConflictRecord`: retained fact-version conflict with explicit resolution
  metadata.
- `DecisionRecord`: versioned human/rule decision over named fact versions.
- `ActionProposal`, `ActionApproval`, `ActionExecution`, and
  `ActionVerification`: distinct records in the internal Action lifecycle.
  `ActionApproval` is a review outcome, not an `ApprovedCommand`; execution
  must reference both.
- `AgentTask` and `AgentTimelineEvent`: durable budget/lease/retry work and its
  append-only audit timeline.
- `MetricDefinition` and `MetricResponse`: governed KPI definition and
  available/unavailable response with freshness, coverage, reconciliation and
  source lineage.
- `KingdeeReadProjection`: separately owned synthetic read-only projection
  envelope. Gate 2 performs no real Kingdee query.

Gate 2 semantic manifests are under `contracts/gate2/`:

- `contract-evolution-matrix.json` freezes reuse/extend/new decisions.
- `context-ontology-v0.json` freezes Context node/relation allow-lists and
  provenance constraints for a PostgreSQL projection.
- `metrics-registry-v0.json` contains governed synthetic metric definitions.
- `services-v1.openapi.json` describes typed, versioned design-only service
  operations. It is not a running service.

Canonical synthetic examples are under `contracts/examples/gate2/`.

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
Agent Timeline monotonicity, aggregate uniqueness, Action stage ordering, and
`attempt <= max_attempts` are cross-record or cross-field service/storage
invariants exercised by Python tests; the individual JSON Schemas do not claim
to enforce them.

## Trust boundary

- Evidence and model output are untrusted inputs.
- A `DraftMutation` cannot approve itself or modify formal stage, outcome,
  price, discount, outbound communication, or order fields.
- An `ApprovedCommand` is only valid after the command service verifies the
  authenticated human, Review Case, expected revision, and payload digest.
- No schema or scope in Gate 0/1 enables a Kingdee write.

## Gate 2 capability boundary

These frozen documents are design contracts, not Gate 0/1 runtime
capabilities. They do not start Agent, Context, Decision, Metrics, MCP or
Kingdee services. Gate 2–4 Kingdee adapters remain mock-only and must make zero
real network or credential calls. No Action contract admits an external side
effect or Kingdee mutation.

Run contract validation with:

```bash
uv run --group dev pytest tests/contracts
```

The validators build a local `referencing.Registry` from repository
`*.schema.json` files. Remote schema retrieval is not configured or permitted.
