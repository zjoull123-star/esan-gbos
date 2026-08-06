# Gate 1 中文 PWA Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the governed Gate 1 Chinese responsive PWA against the frozen BFF.

**Architecture:** Vue Router owns the seven exact routes, an in-memory Frappe
session store owns authorization, and a typed BFF client owns all eight allowed
methods. An inject-manifest service worker treats APIs as `NetworkOnly` and only
versioned shell assets as `CacheFirst`.

**Tech Stack:** Vue 3, TypeScript, Vite, frappe-ui, Vue Router, Workbox, Vitest,
Vue Test Utils, ESLint, vue-tsc.

---

## Chunk 1: Contract and security

### Task 1: Frontend test harness

**Files:**
- Create: `apps/esan_gbos/frontend/package.json`
- Create: `apps/esan_gbos/frontend/vite.config.ts`
- Create: `apps/esan_gbos/frontend/tests/setup.ts`

- [ ] Pin runtime and development dependencies exactly.
- [ ] Generate and retain `package-lock.json`.
- [ ] Configure Vitest, ESLint, vue-tsc, and production output.

### Task 2: Frozen BFF client

**Files:**
- Test: `apps/esan_gbos/frontend/tests/bff.test.ts`
- Create: `apps/esan_gbos/frontend/src/api/bff.ts`
- Create: `apps/esan_gbos/frontend/src/api/types.ts`

- [ ] Write tests for GET encoding, POST CSRF/form payload, schema 1.0, Frappe
  `message` envelope, Chinese errors, request ID, and offline fail-closed.
- [ ] Run tests and verify RED because the client is absent.
- [ ] Implement only the eight frozen methods.
- [ ] Run tests and verify GREEN.

### Task 3: Role and route policy

**Files:**
- Test: `apps/esan_gbos/frontend/tests/navigation.test.ts`
- Create: `apps/esan_gbos/frontend/src/session.ts`
- Create: `apps/esan_gbos/frontend/src/router.ts`
- Create: `apps/esan_gbos/frontend/src/navigation.ts`

- [ ] Write route and role-cropping tests.
- [ ] Verify RED, implement the minimal policy, then verify GREEN.

## Chunk 2: Accessible responsive application

### Task 4: Chinese state and evidence components

**Files:**
- Test: `apps/esan_gbos/frontend/tests/components.test.ts`
- Create: `apps/esan_gbos/frontend/src/components/*.vue`

- [ ] Write tests for loading, empty, permission, error, offline, fixture,
  Chinese summary, original text, and original language.
- [ ] Verify RED, implement minimal semantic components, then verify GREEN.

### Task 5: Workspaces and details

**Files:**
- Test: `apps/esan_gbos/frontend/tests/app.test.ts`
- Create: `apps/esan_gbos/frontend/src/App.vue`
- Create: `apps/esan_gbos/frontend/src/views/*.vue`
- Create: `apps/esan_gbos/frontend/src/styles.css`
- Create: `apps/esan_gbos/frontend/src/main.ts`

- [ ] Write route rendering and keyboard-semantic tests.
- [ ] Verify RED, implement five role workspaces and two detail views, then
  verify GREEN.
- [ ] Keep fixture content visibly labeled and all business data memory-only.

## Chunk 3: PWA and release gates

### Task 6: Static-shell-only service worker

**Files:**
- Test: `apps/esan_gbos/frontend/tests/service-worker.test.ts`
- Create: `apps/esan_gbos/frontend/src/service-worker.ts`
- Create: `apps/esan_gbos/frontend/manifest.webmanifest`

- [ ] Write source-policy tests proving `/api/` is `NetworkOnly` and static
  shell is `CacheFirst`.
- [ ] Verify RED, implement policy, verify GREEN.

### Task 7: Verification

- [ ] Run `npm run lint`.
- [ ] Run `npm run typecheck`.
- [ ] Run `npm test -- --run`.
- [ ] Run `npm run build`.
- [ ] Run `uv run pytest tests/acceptance/test_gate1_structure.py -q`.
- [ ] Inspect the frontend diff and report any Frappe bundle registration gap.
