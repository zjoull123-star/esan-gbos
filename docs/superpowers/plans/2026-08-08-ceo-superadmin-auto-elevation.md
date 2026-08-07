# CEO Superadmin Auto-elevation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically grant every current and future CEO user complete GBOS and Frappe System Manager permissions while preserving existing external-action safety gates.

**Architecture:** A dedicated `ceo_access` module owns the closed role bundle and mutates User documents during `before_validate`. Install and migrate hooks backfill existing CEO users. The review BFF retains assignment scope for ordinary Reviewers and adds an explicit GBOS Admin override.

**Tech Stack:** Frappe Framework v16, Python 3.14, pytest, Vue 3/Vitest, OrbStack Compose.

---

## Chunk 1: Role policy and backfill

### Task 1: Specify CEO role synchronization

**Files:**
- Create: `tests/domain/test_ceo_access.py`
- Create: `apps/esan_gbos/esan_gbos/ceo_access.py`
- Modify: `apps/esan_gbos/esan_gbos/hooks.py`
- Modify: `apps/esan_gbos/esan_gbos/install.py`
- Modify: `tests/domain/test_app_metadata.py`

- [ ] Write tests for the exact required role bundle, idempotent User mutation,
      non-CEO no-op, System User promotion, existing-user backfill, and hook wiring.
- [ ] Run the focused tests and confirm they fail because `ceo_access` and User hook
      do not exist.
- [ ] Implement `ensure_ceo_full_access` and `backfill_ceo_full_access` with a
      closed role tuple and no secret handling.
- [ ] Wire User `before_validate`, `after_install`, and `after_migrate`.
- [ ] Run focused tests until green.

## Chunk 2: Full review authority

### Task 2: Preserve Reviewer scope and add admin override

**Files:**
- Modify: `apps/esan_gbos/esan_gbos/api/v2/review_case.py`
- Create: `tests/domain/test_review_admin_scope.py`

- [ ] Write tests proving an ordinary Reviewer remains assignment-scoped while
      a GBOS Admin can list, read, and decide any review case.
- [ ] Run the focused tests and confirm the admin scenario fails.
- [ ] Split read/write role checks and make case scope conditional on GBOS Admin.
- [ ] Keep decision revision, evidence, policy, audit, and idempotency checks unchanged.
- [ ] Run focused tests until green.

## Chunk 3: Regression and local materialization

### Task 3: Verify and install the policy locally

**Files:**
- Modify after image build: `infra/local/images.lock.json`

- [ ] Run domain, governance, fixture, contract and acceptance tests.
- [ ] Run Ruff format/check and mypy for changed Python sources.
- [ ] Run frontend lint, typecheck, unit tests and production build.
- [ ] Commit the feature using exact owned paths.
- [ ] Rebuild the governed local Frappe image from the clean feature commit.
- [ ] Restart the synthetic local stack without deleting volumes.
- [ ] Verify the running image matches `images.lock.json`.
- [ ] Use the real site to confirm the synthetic CEO has the complete role bundle,
      can open all GBOS routes, and retains an individual audit identity.
- [ ] Confirm external send, Kingdee, real channels and model capabilities remain disabled.
