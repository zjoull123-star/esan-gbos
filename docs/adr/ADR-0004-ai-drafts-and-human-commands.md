# ADR-0004: AI Draft 与人工正式命令

- Status: Gate 0/1 decision; runtime evidence is pending
- Date: 2026-08-06
- Extended by: ADR-0009 Agent Runtime, context and decision sequencing

## Context

Extraction and generation can be useful before a person has verified source
evidence. They are not a safe authority for pricing, deal stage, orders,
purchase orders, outbound messages, or other formal state transitions.

## Decision

1. Model output is an evidence-backed proposal. `ExtractedFact` keeps the
   model, prompt version, confidence, evidence references, and status.
2. Any AI mutation must validate against `draft-mutation.schema.json`, have
   `target_review_status: "AI Draft"`, an idempotency key, a policy version,
   and at least one evidence reference.
3. Draft patch paths cannot include formal fields such as deal stage,
   won/lost, formal price, discount, outbound message, sales order, or
   purchase order. The policy may deny additional fields without changing the
   contract.
4. A named human reviewer examines the source and the proposed delta. Only
   that person (or an explicitly delegated approver) can issue an
   `ApprovedCommand` with actor, review case, expected revision, payload hash,
   before/after status, and idempotency key.
5. The command executor is the only formal transition path. It rechecks
   authorization, tenant, revision, policy, and idempotency, then appends an
   audit record. Model services and Observer have no approve or execute tool.
6. Real model calls are disabled by default in Gate 0/1. Fixtures must make
   provenance obvious and must not look like production approvals.
7. Gate 3 may run bounded, tool-free transformations such as transcription,
   language detection, summarization, and evidence-backed fact proposals after
   provider/privacy approval. Confirmation, decision, DraftMutation, tool use,
   and action orchestration belong to Gate 4 Agent Runtime.
8. Gate 4 Agent tasks are durable, budgeted, leased, idempotent and fully
   recorded in an Agent timeline. All proposed actions pass through the common
   Action Guard before review or execution.

## Consequences

- “AI completed” means a draft was produced, never that a business state
  changed.
- Human review adds latency but gives a clear accountability trail and a
  reversible rejection path.
- A future auto-approval exception needs a new ADR, field-level risk review,
  and evidence that it cannot affect formal financial or external messages.

## Verification gate

Tests must reject missing evidence, forbidden patch paths, wrong review status,
stale revisions, replayed idempotency keys, and unauthorized approvers. A
passing test suite does not enable real model traffic.
