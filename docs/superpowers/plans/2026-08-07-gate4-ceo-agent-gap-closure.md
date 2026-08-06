# Gate 4 Synthetic CEO Agent Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Gate 4 requirement by adding a deterministic, evidence-bound synthetic CEO Agent prototype without introducing a real model, official KPI, arbitrary query, commercial commitment, external side effect, or production capability.

**Architecture:** Extend the existing `AgentKind`/`AgentOrchestrator` profile registry with a `ceo` profile that emits only `internal.ai_draft.propose`. The deterministic provider returns a visibly synthetic internal observation draft whose provenance remains the existing evidence/fact/Decision chain, while the generic durable queue already accepts the contract-defined `ceo` type. Action Guard already allow-lists this proposal type, so no policy, database, Frappe, or PWA contract expansion is required; the existing Gate 5 governed cockpit remains the only CEO UI and this Gate 4 prototype is not presented as an official metric or UI/API feature.

**Tech Stack:** Python 3.14 dataclasses/enums, existing Action Guard and Agent Runtime, pytest, JSON Schema 2020-12, Markdown/JSON evidence.

---

## Chunk 1: Deterministic CEO Agent behavior

### Task 1: Define the CEO Agent contract through failing tests

**Files:**
- Modify: `tests/agent_runtime/test_agents.py`
- Test: `tests/agent_runtime/test_agents.py`

- [ ] **Step 1: Extend the test input factory with an explicit CEO profile**

Add an `AgentKind.CEO` case using:

```python
(
    "metric_reporting",
    "GBOS Synthetic Executive Snapshot",
    "BUSINESS-SNAPSHOT-SYNTH-001",
    "internal.ai_draft.propose",
)
```

The request must use the same exact evidence, verified fact version, Decision,
site and zero-tool boundary as the other Gate 4 agents.

- [ ] **Step 2: Add a failing contract-valid proposal case**

Extend `test_agents_emit_only_contract_valid_internal_proposals` with:

```python
(AgentKind.CEO, "internal.ai_draft.propose")
```

Run:

```bash
uv run --frozen pytest \
  tests/agent_runtime/test_agents.py::test_agents_emit_only_contract_valid_internal_proposals -q
```

Expected: FAIL because `AgentKind.CEO` does not exist.

- [ ] **Step 3: Add a failing CEO safety/content test**

The desired payload must prove all of the following:

```python
assert payload["synthetic"] is True
assert payload["display_label"] == "演示数据"
assert payload["source_mode"] == "synthetic_agent_context"
assert payload["is_official_metric"] is False
assert payload["is_official_forecast"] is False
assert payload["requires_human_review"] is True
assert payload["subject_ref"] == "BUSINESS-SNAPSHOT-SYNTH-001"
assert proposal["site_id"] == request.site_id
assert proposal["decision_ref"] == request.decision_ref
assert proposal["evidence_refs"] == list(request.evidence_refs)
assert proposal["fact_version_refs"] == [
    {"fact_id": "verified-fact-SYNTH-001", "fact_version": 1}
]
assert proposal["target_ref"] == request.subject_ref
assert proposal["target_revision"] == request.subject_revision
assert result.pre_guard.outcome is GuardOutcome.ALLOW
assert result.post_guard.outcome is GuardOutcome.ALLOW
for forbidden in (
    "external.message.send",
    "formal.quotation.publish",
    "order.create",
    "kingdee.",
    "deal.won",
    "selected_supplier",
):
    assert forbidden not in serialized.casefold()
for forbidden_key in (
    "metric_key",
    "metric_value",
    "forecast_value",
    "revenue_value",
    "official_value",
    "currency",
    "unit",
):
    assert forbidden_key not in payload
```

Run the new test directly. Expected: FAIL because the CEO provider branch and
profile are absent.

- [ ] **Step 4: Add a failing hostile-context regression**

Use a CEO request containing instructions to query the database, calculate an
official revenue forecast, send WhatsApp and create a Kingdee order. Assert:

```python
assert result.injection_detected is True
assert result.action_proposal["action_type"] == "internal.ai_draft.propose"
assert result.network_calls == 0
assert result.model_api_calls == 0
assert result.tool_calls == 0
```

Also assert the hostile text and prohibited capabilities do not appear in the
serialized proposal. Expected: FAIL before implementation.

- [ ] **Step 5: Add failing CEO fail-closed and determinism cases**

Verify CEO requests reject unknown evidence, an unknown fact version, an
unknown subject, `requested_tools=("metrics.kpi.get",)`,
`requested_tools=("arbitrary_sql",)` and a mismatched formal action. Add a
byte-stability assertion for identical CEO input/version/time. Expected: the
happy-path tests still fail because the profile is absent; the tool/action
negative cases must fail closed.

Use these exact node IDs for the new direct runs:

```bash
uv run --frozen pytest \
  tests/agent_runtime/test_agents.py::test_ceo_agent_emits_synthetic_internal_observation_without_official_metrics \
  tests/agent_runtime/test_agents.py::test_ceo_agent_detects_hostile_metric_and_commercial_instructions_without_echoing_them \
  tests/agent_runtime/test_agents.py::test_ceo_agent_rejects_unknown_refs_tools_and_formal_actions \
  tests/agent_runtime/test_agents.py::test_ceo_agent_is_byte_stable -q
```

### Task 2: Implement the minimal deterministic CEO profile

**Files:**
- Modify: `services/agent_runtime/agents.py`
- Test: `tests/agent_runtime/test_agents.py`
- Test: `tests/agent_runtime/test_models_and_protocol.py`

- [ ] **Step 1: Add the enum and exact profile**

Add:

```python
class AgentKind(StrEnum):
    SALES = "sales"
    PURCHASE = "purchase"
    PRODUCT = "product"
    CEO = "ceo"
```

And register only:

```python
AgentKind.CEO: (
    "metric_reporting",
    "GBOS Synthetic Executive Snapshot",
    "internal.ai_draft.propose",
),
```

- [ ] **Step 2: Add the bounded provider output**

Return a static, deterministic payload with no values, calculations or
commercial recommendation:

```python
ProviderOutput(
    action_type="internal.ai_draft.propose",
    payload={
        "title": "经营观察草稿（演示）",
        "summary": "根据已确认的合成事实生成内部观察草稿，由负责人复核证据。",
        "synthetic": True,
        "display_label": "演示数据",
        "source_mode": "synthetic_agent_context",
        "is_official_metric": False,
        "is_official_forecast": False,
        "requires_human_review": True,
        "subject_ref": request.subject_ref,
    },
    confidence=0.74,
    injection_detected=injection_detected,
    prompt_version="ceo-agent-prototype-prompt-v1",
)
```

Do not add a tool, network provider, SQL/query field, number, KPI/forecast key,
formal command, new Action Guard capability or schema exception.
`requires_human_review` labels the internal draft artifact only: this prototype
does not auto-create a Review Case or issue an ApprovedCommand. The existing
review BFF remains the sole governed handoff for any later formal internal
command.

- [ ] **Step 3: Extend injection markers narrowly**

Add markers for `query the database` and `official revenue forecast` so the
CEO hostile-context test proves the untrusted text is detected. Detection must
not change the allowed action or echo the hostile input.

- [ ] **Step 4: Run the CEO and full Agent tests**

Run:

```bash
uv run --frozen pytest tests/agent_runtime/test_agents.py -q
```

Expected: all Agent tests pass; generated proposals remain valid against
`contracts/gate4/action-proposal.schema.json`.

- [ ] **Step 5: Run Action Guard regressions**

Run:

```bash
uv run --frozen pytest tests/action_guard tests/agent_runtime -q
```

Expected: all pass, and no policy allow-list beyond the existing
`internal.ai_draft.propose` entry is added.

- [ ] **Step 6: Prove the generic durable queue accepts CEO tasks**

Add a focused test in `tests/agent_runtime/test_models_and_protocol.py` that
creates `valid_submission(agent_type="ceo", processing_purpose="metric_reporting")`,
enqueues it into `InMemoryAgentTaskRepository`, and asserts the stored metadata
preserves `agent_type == "ceo"`. This test documents existing generic queue
behavior; no migration or repository code change is expected because the
database column is text and `contracts/agent-task.schema.json` already lists
`ceo`.

- [ ] **Step 7: Commit implementation**

```bash
git add \
  services/agent_runtime/agents.py \
  tests/agent_runtime/test_agents.py \
  tests/agent_runtime/test_models_and_protocol.py
git commit -m "feat(agent): add synthetic Gate 4 CEO prototype"
```

---

## Chunk 2: Current-state acceptance and closure evidence

### Task 3: Add a current Gate 4 closure acceptance test

**Files:**
- Create: `tests/acceptance/test_gate4_closure.py`
- Create: `docs/evidence/gate4-closure/gate4-closure.json`
- Create: `docs/evidence/gate4-closure/gate4-closure-summary.md`
- Create: `docs/evidence/gate4-closure/SHA256SUMS`

- [ ] **Step 1: Write the failing acceptance test**

Require the closure evidence to state:

- all four orchestrator profiles: `sales`, `purchase`, `product`, `ceo`;
- the durable queue contract types remain `sales`, `purchase`,
  `product_sample`, `ceo`, with an explicit Product profile →
  `product_sample` queue mapping rather than conflating the two names;
- CEO action is exactly `internal.ai_draft.propose`;
- `synthetic = true`, official metric/forecast are false;
- network/model/tool/external/Kingdee/production activity counters are zero;
- Action Guard, evidence/fact/Decision, human review and durable runtime remain
  required;
- real model, live Kingdee, cloud and production remain No-Go;
- compact checksums cover the JSON and summary only.

The test must additionally assert these exact machine-readable fields:

```python
assert evidence["gate"] == 4
assert evidence["closure_id"] == "gate4-synthetic-ceo-agent"
assert evidence["status"] == "technical_local_go"
assert len(evidence["implementation_commit"]) == 40
assert evidence["agent_profiles"] == ["sales", "purchase", "product", "ceo"]
assert evidence["durable_queue_contract_types"] == [
    "sales",
    "purchase",
    "product_sample",
    "ceo",
]
assert evidence["profile_queue_mapping"] == {
    "sales": "sales",
    "purchase": "purchase",
    "product": "product_sample",
    "ceo": "ceo",
}
assert evidence["ceo_prototype"] == {
    "processing_purpose": "metric_reporting",
    "action_type": "internal.ai_draft.propose",
    "source_mode": "synthetic_agent_context",
    "synthetic": True,
    "is_official_metric": False,
    "is_official_forecast": False,
    "requires_human_review": True,
}
assert evidence["go_no_go"] == {
    "gate4_technical_local": "go",
    "real_model": "no_go",
    "kingdee_live": "blocked_external_input",
    "cloud": "no_go",
    "production": "no_go",
}
assert all(value == 0 for value in evidence["external_activity"].values())
assert evidence["historical_evidence"]["sha256sum_manifest_sha256"] \
    == "2df12cda3e442bbe68880e555583affe7e4f483096fd369e2c37bf34ef843b64"
assert evidence["historical_evidence"]["modified"] is False
```

Pin the exact control set:

```python
assert set(evidence["control_results"]) == {
    "action_guard_fail_closed",
    "durable_idempotent_queue",
    "exact_context_decision_trace",
    "human_review_command_boundary",
    "responsive_accessible_review_pwa",
    "site_isolation_and_role_separation",
    "synthetic_ceo_prototype",
}
```

For every control require `status == "pass"`, existing `test_refs`, and
existing `evidence_refs`. Pin the test inventory IDs to:

```python
{
    "repository_pytest",
    "gate4_postgres",
    "python_static",
    "frontend_unit_build",
    "frontend_e2e",
    "secret_scan",
    "closure_acceptance",
    "closure_checksum",
}
```

Every inventory item must contain its exact command, `status == "pass"`,
`failed == 0` and nonnegative `passed`/`skipped` counts. Assert the historical
`docs/evidence/gate4/SHA256SUMS` digest is unchanged.

Pin the zero-activity object exactly:

```python
assert evidence["external_activity"] == {
    "network_calls": 0,
    "model_api_calls": 0,
    "tool_calls": 0,
    "external_messages": 0,
    "kingdee_calls": 0,
    "kingdee_mutations": 0,
    "formal_business_writes": 0,
    "cloud_deployments": 0,
    "production_credentials_loaded": 0,
}
```

Require `implementation_commit` to match `^[0-9a-f]{40}$` and add
`implementation_source_sha256`; the acceptance test recomputes the SHA-256 of
`services/agent_runtime/agents.py`. Before writing evidence, run
`git cat-file -e <implementation_commit>^{commit}` locally and record only the
already committed Task 2 SHA, never the later evidence commit.

The closure control matrix must reference, rather than duplicate, the existing
Gate 4 evidence controls for durable queue/lease recovery, Context Decision
trace, Action Guard pre/post policy, human review/ApprovedCommand, PWA/cache and
site isolation. Add a dedicated `synthetic_ceo_prototype` control referencing
the new agent tests and the unchanged Action Proposal/Agent Task contracts.

Add a source-boundary assertion that `WorkspaceView.vue` still calls only
`client.getMetricDashboard()` for `/gbos/ceo`, renders `MetricCockpit`, and the
frontend source contains no `getCeoAgent`/CEO-Agent API. This proves the gap
closure did not misrepresent the backend prototype as a UI or official metric.

Run:

```bash
uv run --frozen pytest tests/acceptance/test_gate4_closure.py -q
```

Expected: FAIL because the closure evidence does not exist.

- [ ] **Step 2: Run the complete current verification matrix before writing evidence**

Run:

```bash
uv run --frozen pytest --ignore=tests/acceptance/test_gate4_closure.py
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy \
  apps/esan_gbos/esan_gbos/domain \
  services/observer/contract_check.py \
  services/observer/observer \
  services/context/context_service \
  services/action_guard \
  services/agent_runtime \
  services/metrics \
  services/kingdee_adapter \
  fixtures/gate1/generate.py \
  fixtures/kingdee/gate1/mock.py \
  fixtures/kingdee/gate2/adapter.py
scripts/dev/test-gate4-integration
scripts/dev/secret-scan
corepack pnpm --dir apps/esan_gbos/frontend install --frozen-lockfile
corepack pnpm --dir apps/esan_gbos/frontend run lint
corepack pnpm --dir apps/esan_gbos/frontend run typecheck
corepack pnpm --dir apps/esan_gbos/frontend run test:unit
corepack pnpm --dir apps/esan_gbos/frontend run build
corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
```

Expected: zero failures. Record exact counts; do not copy historical counts.

- [ ] **Step 3: Write compact, truthful closure evidence**

Use the implementation commit, exact test counts and current boundary. Do not
rewrite the historical `docs/evidence/gate4/` snapshot. The closure summary
must explain that:

- this closes only the missing synthetic CEO prototype;
- the existing Gate 5 governed cockpit is not an output of this prototype;
- formal CEO analytics remain governed by Metrics API;
- `requires_human_review` is metadata on an AI Draft proposal and does not
  claim that this prototype created a Review Case or ApprovedCommand;
- no external or production authorization is created.

- [ ] **Step 4: Generate and verify checksums**

Cover exactly:

```text
gate4-closure.json
gate4-closure-summary.md
```

Run the acceptance test and `shasum -a 256 -c SHA256SUMS` from the evidence
directory. Expected: all pass.

Before committing, run:

```bash
git diff --exit-code -- docs/evidence/gate4
git diff --check
```

Expected: the checksum-protected historical Gate 4 evidence remains byte-for-byte
unchanged and the new closure files are clean.

- [ ] **Step 5: Update current project status**

Modify `README.md` to state that Gate 4 now includes the deterministic
Sales/Purchase/Product/CEO prototype boundary and link to the closure evidence.
Do not change historical evidence claims or imply a real CEO Agent/KPI.

- [ ] **Step 6: Commit evidence**

```bash
git add README.md docs/evidence/gate4-closure tests/acceptance/test_gate4_closure.py
git commit -m "docs(gate4): close synthetic CEO agent evidence gap"
```

### Task 4: Final integration and completion audit

**Files:**
- Verify only; no expected source changes.

- [ ] **Step 1: Re-run the full repository and focused Gate 4 suites**

Run the complete matrix with the closure test included (no `--ignore`):

```bash
uv run --frozen pytest
uv run --frozen pytest tests/acceptance/test_gate4_closure.py -q
scripts/dev/test-gate4-integration
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy \
  apps/esan_gbos/esan_gbos/domain \
  services/observer/contract_check.py \
  services/observer/observer \
  services/context/context_service \
  services/action_guard \
  services/agent_runtime \
  services/metrics \
  services/kingdee_adapter \
  fixtures/gate1/generate.py \
  fixtures/kingdee/gate1/mock.py \
  fixtures/kingdee/gate2/adapter.py
scripts/dev/secret-scan
(cd docs/evidence/gate4-closure && shasum -a 256 -c SHA256SUMS)
```

Then run the unchanged frontend regressions:

```bash
corepack pnpm --dir apps/esan_gbos/frontend install --frozen-lockfile
corepack pnpm --dir apps/esan_gbos/frontend run lint
corepack pnpm --dir apps/esan_gbos/frontend run typecheck
corepack pnpm --dir apps/esan_gbos/frontend run test:unit
corepack pnpm --dir apps/esan_gbos/frontend run build
corepack pnpm --dir apps/esan_gbos/frontend run test:e2e
```

The frontend is unchanged, but the Gate 4 completion claim requires confirming
its current accessibility, responsive and sensitive-cache invariants.

- [ ] **Step 2: Audit every Gate 4 requirement against direct evidence**

Confirm durable lease/retry/dead-letter/timeline, Context conflict/verified
fact/Decision lineage, Action Guard pre/post checks, DraftMutation/Review Case/
ApprovedCommand, four deterministic agents, budgets/injection tests, review
PWA and zero forbidden activity. Any missing direct evidence keeps the goal
active.

- [ ] **Step 3: Fast-forward local `main` only after all checks pass**

Verify the canonical main worktree is clean, use `git merge --ff-only`, and do
not push or mutate production unless separately requested.
