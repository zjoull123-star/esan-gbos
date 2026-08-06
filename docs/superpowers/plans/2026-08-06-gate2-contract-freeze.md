# ESAN GBOS Gate 2 Contract Freeze Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze and automatically validate the Agent, Context/Decision, Metrics, MCP, and Kingdee read-only design package without making any real external connection.

**Architecture:** Existing Gate 0 contracts remain compatible primitives. Gate 2 adds independently versioned JSON Schema contracts, synthetic examples, semantic manifests, a network-free Kingdee projection adapter, and a checksummed evidence package. No Frappe DocType, live service, credential, channel, model, or Kingdee call is introduced.

**Tech Stack:** JSON Schema 2020-12, OpenAPI 3.1, Python 3.14, `jsonschema`, pytest, deterministic synthetic fixtures, Markdown ADR/governance.

---

## File ownership

### Contract owner

- Create: `contracts/common.schema.json`
- Create: `contracts/evidence-record.schema.json`
- Create: `contracts/verified-business-fact.schema.json`
- Create: `contracts/conflict-record.schema.json`
- Create: `contracts/decision-record.schema.json`
- Create: `contracts/action-proposal.schema.json`
- Create: `contracts/action-approval.schema.json`
- Create: `contracts/action-execution.schema.json`
- Create: `contracts/action-verification.schema.json`
- Create: `contracts/agent-task.schema.json`
- Create: `contracts/agent-timeline-event.schema.json`
- Create: `contracts/metric-definition.schema.json`
- Create: `contracts/metric-response.schema.json`
- Create: non-Kingdee files under `contracts/examples/gate2/*.json`
- Create: `contracts/gate2/contract-evolution-matrix.json`
- Create: `contracts/gate2/context-ontology-v0.json`
- Create: `contracts/gate2/metrics-registry-v0.json`
- Create: `contracts/gate2/services-v1.openapi.json`
- Modify: `contracts/README.md`
- Modify: `tests/contracts/test_schemas.py`
- Create: `tests/contracts/test_gate2_contracts.py`
- Create: `tests/contracts/test_gate2_openapi.py`
- Create: `tests/contracts/test_gate2_semantics.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

### Kingdee mock owner

- Create: `contracts/kingdee-read-projection.schema.json`
- Create: Kingdee projection files under `contracts/examples/gate2/*.json`
- Create: `contracts/gate2/kingdee-field-dictionary-v0.json`
- Create: `contracts/gate2/kingdee-query-allowlist-v0.json`
- Create: `contracts/gate2/mcp-tool-manifest-v0.json`
- Create: `fixtures/kingdee/gate2/__init__.py`
- Create: `fixtures/kingdee/gate2/adapter.py`
- Create: `fixtures/kingdee/gate2/README.md`
- Create: `tests/fixtures/test_gate2_kingdee.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`

### Governance/evidence owner

- Create: `docs/governance/gate2-data-flow-and-capacity.md`
- Create: `docs/governance/gate2-test-strategy.md`
- Modify: `docs/external-deps.md`
- Modify: `docs/governance/README.md`
- Modify: `docs/governance/mcp-authorization.md`
- Modify: `docs/governance/threat-model.md`
- Create: `docs/evidence/gate2/gate2-summary.md`
- Create: `docs/evidence/gate2/gate2-evidence.json`
- Create: `docs/evidence/gate2/contract-validation.json`
- Create: `docs/evidence/gate2/security-review.json`
- Create: `docs/evidence/gate2/SHA256SUMS`
- Create: `tests/governance/test_gate2_design.py`
- Create: `tests/acceptance/test_gate2_evidence.py`

No owner may modify `docs/evidence/SHA256SUMS` or any Gate 0/1 evidence file.
No owner may modify `contracts/bff-v1.openapi.json`,
`services/observer/contract_check.py`, `fixtures/kingdee/gate1/**`, or any
Frappe DocType. Cross-record timeline/action ordering is enforced by Python
tests; schemas only define individual record shapes.

## Task 1: Contract registry and evidence/fact/decision/action schemas

- [ ] **Step 1: Write failing schema-discovery and canonical-example tests**

Extend `tests/contracts/test_schemas.py` so every `*.schema.json` is discovered,
registered by `$id`, checked as Draft 2020-12, and rejects unresolved references.
Create `tests/contracts/test_gate2_contracts.py` with canonical examples for
Evidence Record, Verified Business Fact, Conflict, Decision, and the four Action
stages.

Use a local `referencing.Registry` built from repository schemas; remote
reference retrieval is forbidden. Add `referencing` as a direct dev dependency
and update the lockfile if the tests import it directly.

- [ ] **Step 2: Run RED**

Run:

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest \
  tests/contracts/test_schemas.py \
  tests/contracts/test_gate2_contracts.py -q
```

Expected: FAIL because the new schema files and examples do not exist.

- [ ] **Step 3: Add the common definitions and minimal schemas**

Use strict `additionalProperties: false`, stable IDs, explicit `site_id`,
timestamps, evidence/lineage, review status, and version fields. Reuse
`ExtractedFact` as the proposal contract; do not create a duplicate Fact
Proposal schema. Do not add new required fields to the six frozen v1 schemas;
new aggregate schemas carry the Gate 2 requirements.

- [ ] **Step 4: Add negative tests**

Reject:

- verified facts without evidence or decision;
- resolved conflicts without resolver/basis;
- decisions without input fact versions;
- action approval without a human reviewer;
- action execution before approval;
- Kingdee or external write action types;
- action verification without execution reference.

- [ ] **Step 5: Run GREEN**

Run the Task 1 test command and require all tests to pass.

## Task 2: Agent, timeline, metrics, ontology and service contracts

- [ ] **Step 1: Write failing tests**

Add tests for:

- Agent Task lease pairing, budget bounds, attempts, terminal states, and
  causation/correlation;
- Agent Timeline monotonically ordered event shape;
- Metric Definition owner/window/unit/source/exclusion requirements;
- Metric Response available/unavailable branches, definition version,
  freshness, coverage, reconciliation, and source lineage;
- Context ontology node/relation allow-lists and mandatory relation provenance;
- contract evolution matrix mapping each concept to reuse/extend/new;
- OpenAPI paths containing only versioned, typed service endpoints.

Monotonic timeline sequence, aggregate uniqueness, and Action stage ordering
must be exercised as cross-record Python invariants rather than advertised as
schema-only guarantees.

- [ ] **Step 2: Run RED**

Run:

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest \
  tests/contracts/test_gate2_contracts.py -q
```

Expected: FAIL because the Agent/Metrics/manifests/OpenAPI files are missing.

- [ ] **Step 3: Implement minimal contracts and examples**

The OpenAPI design may expose typed Agent, Context, Decision, Action, Metrics,
and Kingdee read-projection operations. It must not expose generic SQL,
DocType/Form forwarding, database writes, or external mutations.

- [ ] **Step 4: Run GREEN**

Run Task 2 tests and all `tests/contracts`.

## Task 3: Kingdee Gate 2 zero-network design and projection adapter

- [ ] **Step 1: Write failing tests**

Create `tests/fixtures/test_gate2_kingdee.py` requiring:

- a field dictionary covering material, customer, supplier, sales, purchase,
  inventory, and receivable logical objects;
- every unverified real form/field explicitly marked
  `gate5_metadata_required`;
- a query allow-list with `network_allowed: false` and no mutation verbs;
- an MCP manifest where `kingdee-read` is earliest Gate 5 and all Gate 2 tools
  are disabled/mock-only;
- an adapter that returns deterministic synthetic
  `KingdeeReadProjection` envelopes;
- rejection of unknown object, form, field, filter, excessive rows, and every
  writer-shaped operation;
- no imports/calls for HTTP clients, sockets, subprocesses, credential stores,
  or environment secrets.

The public mock surface is the exact seven-object read tool set. It rejects
raw `form_id`, `field_keys`, SQL, DocType, URL/host/method, raw filters/order
expressions, unknown fields, and all mutation-shaped operations before any
transport could be attempted. Every projection carries explicit `site_id`,
synthetic account-set reference, dictionary/allow-list versions, Crosswalk and
evidence status, query time, and measured zero-network/zero-credential flags.

- [ ] **Step 2: Run RED**

Run:

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest \
  tests/fixtures/test_gate2_kingdee.py -q
```

Expected: FAIL because Gate 2 manifests and adapter do not exist.

- [ ] **Step 3: Implement the deterministic adapter**

Use synthetic identifiers and timestamps only. Either reuse safe Gate 1
fixtures through an explicit adapter boundary or define Gate 2 synthetic rows;
do not copy credentials or claim real field verification.

- [ ] **Step 4: Run GREEN and type checks**

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest \
  tests/fixtures/test_gate2_kingdee.py -q
/Users/ericesan/Documents/GBOS/.venv/bin/mypy \
  fixtures/kingdee/gate2/adapter.py
```

## Task 4: Governance, test strategy, and Gate 2 evidence

- [ ] **Step 1: Write failing governance/evidence tests**

Require:

- four-truth data flow and service boundaries;
- capacity assumptions and failure-closed behavior;
- Gate 3/4/5 test ownership;
- MCP scope activation by Gate;
- Gate 2 explicit zero network/credential/channel/model declaration;
- evidence JSON containing commit placeholder, test inventory, known limits,
  zero-network result, unchanged Gate 0/1 checksum result, and Go/No-Go;
- a Gate 2-local `SHA256SUMS` that does not alter the historical checksum file.
- the historical Gate 0/1 manifest digest remains
  `a6a86c5dcb39d5d57b27e3cf7b444f71700bd74db362a74af6b2816186982cea`;
- Gate 2 evidence contains separate contract-validation and security-review
  machine summaries, and never hashes or rewrites a Gate 0/1 artifact.

- [ ] **Step 2: Run RED**

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest \
  tests/governance/test_gate2_design.py \
  tests/acceptance/test_gate2_evidence.py -q
```

Expected: FAIL because the governance and evidence files are missing.

- [ ] **Step 3: Write governance and evidence files**

Evidence must distinguish design verification from runtime capability. A
Gate 2 Go authorizes Gate 3 implementation only; it does not authorize a live
connector, model, Kingdee call, MCP service, cloud deployment, or production.
Severity disposition requires owner, status, evidence/test reference, and a
human review field; a mere string-presence test is not evidence that a risk is
closed.

- [ ] **Step 4: Generate and verify Gate 2-local checksums**

Checksums cover only Gate 2 evidence files and referenced static manifests.
Re-run the original `docs/evidence/SHA256SUMS` verification separately.

- [ ] **Step 5: Run GREEN**

Run Task 4 tests.

## Task 5: Full Gate 2 verification and evidence finalization

- [ ] **Step 1: Run repository tests**

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/pytest -q
```

- [ ] **Step 2: Run static checks**

```bash
/Users/ericesan/Documents/GBOS/.venv/bin/ruff check .
/Users/ericesan/Documents/GBOS/.venv/bin/ruff format --check .
/Users/ericesan/Documents/GBOS/.venv/bin/mypy \
  apps/esan_gbos/esan_gbos/domain \
  services/observer/contract_check.py \
  fixtures/gate1/generate.py \
  fixtures/kingdee/gate1/mock.py \
  fixtures/kingdee/gate2/adapter.py
```

- [ ] **Step 3: Run security and evidence checks**

```bash
scripts/dev/secret-scan
(cd docs/evidence && shasum -a 256 -c SHA256SUMS)
(cd docs/evidence/gate2 && shasum -a 256 -c SHA256SUMS)
git diff --check
```

- [ ] **Step 4: Verify Gate 2 invariants**

Search and tests must prove:

- no credentials or real account-set identifiers;
- no live network/HTTP/client code in Gate 2 adapter;
- no Kingdee writer or generic SQL/DocType/Form tool;
- no new Frappe DocType;
- no modification to Gate 0/1 evidence;
- all examples validate against their schema;
- all claimed Gate 2 evidence has a reproducible command.

- [ ] **Step 5: Finalize evidence with the exact commit**

Use a two-commit evidence pattern if the evidence records the implementation
commit. The second commit may update only Gate 2 evidence metadata/checksum and
must re-run the evidence tests.

## Exit decision

Gate 2 is `Go` only when every checkbox above is satisfied and the evidence
package explicitly says that Gate 3 may begin while all real external
capabilities remain disabled.
