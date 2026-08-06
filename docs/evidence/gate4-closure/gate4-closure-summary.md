# Gate 4 synthetic CEO prototype closure

Status: **Technical Local Go** for the narrow Gate 4 closure only. Real model,
live Kingdee, cloud and production remain **No-Go** or
`blocked_external_input`.

## What this closes

This closes only the missing synthetic CEO prototype. Gate 4 now has four
deterministic local profiles: Sales, Purchase, Product and CEO. The CEO profile
can emit only `internal.ai_draft.propose`; its payload is visibly synthetic,
contains no official metric or forecast value, and makes zero network, model,
tool, external-message, Kingdee, cloud or production calls.

The durable Agent Task contract retains the types `sales`, `purchase`,
`product_sample` and `ceo`, while the orchestrator profile kind remains
`product`. They are separate names. This closure does not implement or claim a
durable worker-to-orchestrator dispatcher or a runtime Product →
`product_sample` mapping.

## Governance boundary

Action Guard pre/post checks, exact evidence/fact/Decision lineage, the durable
runtime and the existing human-review command boundary remain required.
`requires_human_review` is metadata on the AI Draft proposal only. It does not
claim that this prototype created a Review Case or issued an ApprovedCommand.
Any later formal internal command must continue through the governed review BFF.

The Gate 5 governed cockpit is not an output of this prototype. `/gbos/ceo`
still renders `MetricCockpit` from the governed Metrics API, remains labelled
Gate 5, and has no `getCeoAgent` or CEO-Agent API path. Formal CEO analytics
remain governed by the Metrics API.

No external or production authorization is created by this closure.

## Current verification

- Repository tests excluding this closure test: `955 passed, 12 skipped`.
- Gate 4 PostgreSQL: `3 passed`; after correcting a test-harness clock split,
  three consecutive exact runs passed, including one after restarting only the
  local synthetic observer PostgreSQL container.
- Ruff check and format check passed; Mypy passed for 61 source files.
- Frontend lint and typecheck passed; Vitest: `77 passed`; production build
  passed; Playwright: `6 passed`.
- Secret scan found no supported secret patterns.
- Closure acceptance: `8 passed`; two compact checksum targets verified.

The implementation is commit
`f341948986d2426abb873940c2d10960cc82ea9b`. The current
`services/agent_runtime/agents.py` SHA-256 is
`6e532884a028e86bba7810393279c1a10b367818647255fda545c0b37b6dbf26`.
Acceptance verifies that the commit exists and that the same source SHA-256 is
present both in that commit and in the current tree.
The historical `docs/evidence/gate4/` snapshot was not modified; its manifest
SHA-256 remains
`2df12cda3e442bbe68880e555583affe7e4f483096fd369e2c37bf34ef843b64`.
