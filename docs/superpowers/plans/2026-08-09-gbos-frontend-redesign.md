# ESAN GBOS Frontend Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Gate prototype shell and generic card grid with the approved dark-sidebar, desktop/mobile-equal GBOS application while preserving every existing permission, BFF, evidence, no-store, and fail-closed boundary.

**Architecture:** Introduce a semantic design-token layer, a role-aware responsive shell, and three reusable page templates. Migrate each existing route onto a page-specific view without adding fake business data or new business-write APIs; keep missing product-blueprint capabilities visibly pending until safe backend read contracts exist.

**Tech Stack:** Vue 3.5, Vue Router 4, TypeScript 6, Frappe UI 0.1.278 through local wrappers, Tailwind/PostCSS, Vitest, Playwright, axe-core, Frappe v16.

---

## Preconditions and invariants

- Start from commit `ad78323` or a descendant containing the approved design:
  `docs/superpowers/specs/2026-08-09-gbos-frontend-redesign-design.md`.
- Work only in the isolated frontend branch/worktree; do not modify `main`, external
  channels, DeepSeek, Kingdee, cloud, or production configuration.
- Preserve all current BFF paths and response envelopes. This plan does not authorize a
  new business-write endpoint.
- Keep all API requests same-origin and `cache: no-store`; keep Service Worker business
  API handling `NetworkOnly`.
- Keep synthetic and fixture provenance visible. Never create client-side KPI, pipeline,
  quotation, supplier score, or AI fact values.
- Use `apply_patch` for edits and stage only paths owned by the active task.
- Run every focused RED command before implementation and record the expected failure.
- At each commit, verify `git diff --cached --check` and the exact staged path list.

## Execution ownership and ordering

- The primary agent owns shared coordination files: `router.ts`, `navigation.ts`,
  `App.vue`, `main.ts`, global styles, Playwright configuration, evidence, and final
  integration.
- Tasks 1–4 run sequentially because they establish the route, token, shell, and shared
  component contracts used by every page.
- After Task 4 is green, Tasks 5–9 may run in parallel only when each worker owns its exact
  view/component/test paths. A worker must not edit another task's tests or shared router;
  required router changes are handed back to the primary agent for integration.
- Tasks 10–12 run sequentially after every page migration is integrated. Obsolete files are
  deleted only after a repository-wide zero-import check.
- Workers never stage directories or use broad globs. Every commit uses exact paths and is
  independently reviewed by the primary agent before the next dependency starts.

## Planned file structure

```text
apps/esan_gbos/frontend/src/
  design/
    tokens.css                 semantic color, typography, spacing, radius tokens
    base.css                   reset, focus, typography and global canvas rules
  components/
    shell/
      AppShell.vue             full-height application layout
      WorkspaceSidebar.vue     grouped role-aware desktop navigation
      AppTopbar.vue            breadcrumb, search shell, user/session status
      MobileNavDrawer.vue      accessible complete navigation on small screens
      MobileBottomNav.vue      five primary mobile actions
    feedback/
      ResourceBoundary.vue     loading/offline/permission/error/empty state switch
    layout/
      PageHeader.vue           compact title and page actions
      DashboardTemplate.vue    KPI/trend/action layout slots
      OperationalListTemplate.vue filter/list/pagination layout slots
      DetailCommandTemplate.vue facts/evidence/command layout slots
    ui/
      GbosButton.vue           local Frappe UI Button wrapper
      GbosField.vue            local form control wrapper
      StatusBadge.vue          semantic state badge
      ConfirmDialog.vue        accessible replacement for window.confirm
    data/
      MetricTile.vue           compact governed metric summary
      OperationalList.vue      aligned desktop rows/mobile labeled rows
      ObjectSummary.vue        object identity, owner, revision and state
      Timeline.vue             chronological business events
      EvidencePanel.vue        summary/original/evidence with lang and dir
  views/
    OverviewView.vue
    CeoDashboardView.vue
    SalesWorkspaceView.vue
    PurchaseWorkspaceView.vue
    ProductWorkspaceView.vue
    ...existing detail/control views migrated in place
```

`WorkspaceView.vue`, `RecordGrid.vue`, and obsolete global CSS are removed only after all
routes have migrated and the full suite is green.

---

## Chunk 1: Role contract, design system, and responsive shell

### Task 1: Freeze the role-aware `/gbos` entry contract

**Files:**

- Modify: `apps/esan_gbos/frontend/tests/navigation.test.ts`
- Modify: `tests/domain/test_app_metadata.py`
- Create: `apps/esan_gbos/frontend/src/views/OverviewView.vue`
- Modify: `apps/esan_gbos/frontend/src/navigation.ts`
- Modify: `apps/esan_gbos/frontend/src/router.ts`
- Modify: `apps/esan_gbos/frontend/src/App.vue`
- Modify: `apps/esan_gbos/frontend/src/main.ts`
- Modify: `apps/esan_gbos/frontend/public/manifest.webmanifest`
- Modify: `apps/esan_gbos/esan_gbos/www/gbos.py`

- [ ] **Step 1: Write the failing route and shell-role tests**

Add focused expectations before changing production code:

```ts
it("uses a role-aware product entry before the existing business routes", () => {
  expect(APP_ROUTES.map((route) => route.path)).toEqual([
    "/gbos",
    "/gbos/ceo",
    "/gbos/sales",
    "/gbos/purchase",
    "/gbos/product",
    "/gbos/review",
    "/gbos/integrations",
    "/gbos/communications",
    "/gbos/communications/:id",
    "/gbos/review/:id",
    "/gbos/party/:id",
    "/gbos/sample/:id",
  ]);
});

it("sends each authenticated role to its first authorized workspace", () => {
  expect(defaultWorkspaceForRoles(["CEO"])).toBe("/gbos/ceo");
  expect(defaultWorkspaceForRoles(["Sales User"])).toBe("/gbos/sales");
  expect(defaultWorkspaceForRoles(["Integration Admin"])).toBe("/gbos/integrations");
  expect(defaultWorkspaceForRoles([])).toBeUndefined();
});
```

Extend `test_gbos_shell_uses_manifest_assets_and_non_executable_bootstrap_json` or add a
new source-level test asserting that `Integration Admin` is a PWA shell role and that
`Finance Readonly` remains explicitly excluded for this release.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest -q tests/domain/test_app_metadata.py
cd apps/esan_gbos/frontend
pnpm test:unit -- tests/navigation.test.ts
```

Expected: failures because `/gbos`, `defaultWorkspaceForRoles`, and the Integration Admin
shell contract are not implemented.

- [ ] **Step 3: Implement the minimum entry contract**

Define grouped navigation separately from route authorization:

```ts
export type NavigationGroupId =
  | "management"
  | "operations"
  | "intelligence"
  | "system";

export interface NavigationItem {
  id: string;
  label: string;
  to: string;
  icon: string;
  group: NavigationGroupId;
  roles: readonly string[];
}

export const defaultWorkspaceForRoles = (roles: readonly string[]) =>
  navigationForRoles(roles)[0]?.to;
```

Add `/gbos` as a real, role-authorized overview route. Change brand links, login redirect,
unknown-route fallback, and manifest `start_url` to `/gbos`. Add `Integration Admin` to
`_PWA_ROLES`; do not add `Finance Readonly` in this task.

The first `OverviewView.vue` contains only authorized module links and runtime labels. It
must not call a business API or display numeric business data.

- [ ] **Step 4: Run focused GREEN verification**

Run the two commands from Step 2.

Expected: all selected tests pass; existing detail-route authorization expectations remain
unchanged.

- [ ] **Step 5: Commit the entry contract**

```bash
git add apps/esan_gbos/frontend/tests/navigation.test.ts \
  tests/domain/test_app_metadata.py \
  apps/esan_gbos/frontend/src/views/OverviewView.vue \
  apps/esan_gbos/frontend/src/navigation.ts \
  apps/esan_gbos/frontend/src/router.ts \
  apps/esan_gbos/frontend/src/App.vue \
  apps/esan_gbos/frontend/src/main.ts \
  apps/esan_gbos/frontend/public/manifest.webmanifest \
  apps/esan_gbos/esan_gbos/www/gbos.py
git diff --cached --check
git commit -m "feat(pwa): add role-aware GBOS entry"
```

### Task 2: Add semantic tokens and the desktop shell

**Files:**

- Create: `apps/esan_gbos/frontend/src/design/tokens.css`
- Create: `apps/esan_gbos/frontend/src/design/base.css`
- Create: `apps/esan_gbos/frontend/src/components/shell/AppShell.vue`
- Create: `apps/esan_gbos/frontend/src/components/shell/WorkspaceSidebar.vue`
- Create: `apps/esan_gbos/frontend/src/components/shell/AppTopbar.vue`
- Create: `apps/esan_gbos/frontend/tests/shell.test.ts`
- Modify: `apps/esan_gbos/frontend/src/App.vue`
- Modify: `apps/esan_gbos/frontend/src/main.ts`

- [ ] **Step 1: Write failing shell and token tests**

Create `shell.test.ts` with at least these assertions:

```ts
expect(tokens).toContain("--gbos-canvas: #f6f8fb");
expect(tokens).toContain("--gbos-sidebar: #0b1220");
expect(tokens).toContain("--gbos-primary: #6c5ce7");
expect(tokens).toContain("--gbos-accent: #0f9f8f");

const wrapper = mount(AppShell, {
  props: {
    navigation: navigationGroupsForRoles(["CEO"]),
    sessionLabel: "ceo@example.invalid",
  },
  global: {
    stubs: {
      RouterLink: { template: "<a><slot /></a>" },
    },
  },
});
expect(wrapper.get("nav[aria-label='工作区导航']")).toBeTruthy();
expect(wrapper.get("#main-content").attributes("tabindex")).toBe("-1");
expect(wrapper.text()).toContain("经营管理");
expect(wrapper.text()).toContain("业务协同");
```

Import token CSS as raw text with Vite's `?raw` support for the source assertions.

- [ ] **Step 2: Run the new test and verify RED**

```bash
cd apps/esan_gbos/frontend
pnpm test:unit -- tests/shell.test.ts
```

Expected: module-not-found failures for the new design and shell files.

- [ ] **Step 3: Implement semantic tokens and desktop shell**

`tokens.css` must define the approved values and semantic aliases. `base.css` owns only
reset, typography, canvas, focus, skip-link, and reduced-motion behavior.

`AppShell.vue` accepts presentational `navigation` and `sessionLabel` props and owns the
full-height grid and slots:

```vue
<div class="app-shell">
  <WorkspaceSidebar :groups="navigation" />
  <div class="app-shell__workspace">
    <AppTopbar />
    <main id="main-content" tabindex="-1"><slot /></main>
  </div>
</div>
```

Keep authorization and online/session boundaries in `App.vue`; the shell must remain a
presentational boundary. Use 240px desktop sidebar, 64px topbar, 24px content padding,
compact grouped navigation, and no fixed Gate footer.

- [ ] **Step 4: Run focused tests, typecheck, and build**

```bash
pnpm test:unit -- tests/shell.test.ts tests/navigation.test.ts tests/app.test.ts
pnpm typecheck
pnpm build
```

Expected: PASS, with no Tailwind/Vite missing-style warnings.

- [ ] **Step 5: Commit the desktop shell**

```bash
git add apps/esan_gbos/frontend/src/design \
  apps/esan_gbos/frontend/src/components/shell/AppShell.vue \
  apps/esan_gbos/frontend/src/components/shell/WorkspaceSidebar.vue \
  apps/esan_gbos/frontend/src/components/shell/AppTopbar.vue \
  apps/esan_gbos/frontend/tests/shell.test.ts \
  apps/esan_gbos/frontend/src/App.vue \
  apps/esan_gbos/frontend/src/main.ts
git diff --cached --check
git commit -m "feat(pwa): add GBOS desktop application shell"
```

### Task 3: Add the tablet rail and mobile navigation

**Files:**

- Create: `apps/esan_gbos/frontend/src/components/shell/MobileNavDrawer.vue`
- Create: `apps/esan_gbos/frontend/src/components/shell/MobileBottomNav.vue`
- Modify: `apps/esan_gbos/frontend/src/components/shell/AppShell.vue`
- Modify: `apps/esan_gbos/frontend/src/components/shell/WorkspaceSidebar.vue`
- Modify: `apps/esan_gbos/frontend/src/components/shell/AppTopbar.vue`
- Modify: `apps/esan_gbos/frontend/tests/shell.test.ts`

- [ ] **Step 1: Add failing keyboard and ARIA tests**

Cover:

```ts
expect(menuButton.attributes("aria-expanded")).toBe("false");
await menuButton.trigger("click");
expect(menuButton.attributes("aria-expanded")).toBe("true");
expect(document.activeElement).toBe(drawerFirstLink.element);
await drawer.trigger("keydown", { key: "Escape" });
expect(menuButton.attributes("aria-expanded")).toBe("false");
expect(document.activeElement).toBe(menuButton.element);
```

Assert the bottom navigation contains 首页、销售、沟通、审核、更多 for a CEO, while a
non-CEO never receives unauthorized links.

- [ ] **Step 2: Run and verify RED**

```bash
pnpm test:unit -- tests/shell.test.ts
```

Expected: missing mobile navigation components and failing focus behavior.

- [ ] **Step 3: Implement responsive navigation**

- `>=1200px`: full 240px sidebar.
- `768–1199px`: 72px icon rail with accessible labels/tooltips.
- `<768px`: topbar + bottom navigation + complete modal drawer.
- Drawer: `aria-modal`, `aria-labelledby`, focus trap, Escape close, backdrop close, route
  close, and focus return.
- Body scrolling is locked only while the drawer is open and restored in `onBeforeUnmount`.

- [ ] **Step 4: Run focused GREEN and a production build**

```bash
pnpm test:unit -- tests/shell.test.ts tests/navigation.test.ts
pnpm typecheck
pnpm build
```

Expected: PASS.

- [ ] **Step 5: Commit responsive navigation**

```bash
git add apps/esan_gbos/frontend/src/components/shell/AppShell.vue \
  apps/esan_gbos/frontend/src/components/shell/WorkspaceSidebar.vue \
  apps/esan_gbos/frontend/src/components/shell/AppTopbar.vue \
  apps/esan_gbos/frontend/src/components/shell/MobileNavDrawer.vue \
  apps/esan_gbos/frontend/src/components/shell/MobileBottomNav.vue \
  apps/esan_gbos/frontend/tests/shell.test.ts
git diff --cached --check
git commit -m "feat(pwa): add responsive GBOS navigation"
```

### Task 4: Add shared resource, layout, UI, and evidence components

**Files:**

- Create: `apps/esan_gbos/frontend/src/components/feedback/ResourceBoundary.vue`
- Create: `apps/esan_gbos/frontend/src/components/layout/PageHeader.vue`
- Create: `apps/esan_gbos/frontend/src/components/layout/DashboardTemplate.vue`
- Create: `apps/esan_gbos/frontend/src/components/layout/OperationalListTemplate.vue`
- Create: `apps/esan_gbos/frontend/src/components/layout/DetailCommandTemplate.vue`
- Create: `apps/esan_gbos/frontend/src/components/ui/GbosButton.vue`
- Create: `apps/esan_gbos/frontend/src/components/ui/GbosField.vue`
- Create: `apps/esan_gbos/frontend/src/components/ui/StatusBadge.vue`
- Create: `apps/esan_gbos/frontend/src/components/ui/ConfirmDialog.vue`
- Create: `apps/esan_gbos/frontend/src/components/data/OperationalList.vue`
- Create: `apps/esan_gbos/frontend/src/components/data/EvidencePanel.vue`
- Create: `apps/esan_gbos/frontend/tests/templates.test.ts`
- Modify: `apps/esan_gbos/frontend/src/frappe-ui.d.ts`
- Modify: `apps/esan_gbos/frontend/vite.config.ts`
- Modify: `apps/esan_gbos/frontend/tailwind.config.js`

- [ ] **Step 1: Write failing component-contract tests**

Test every ResourceBoundary state, named template slot, StatusBadge label, desktop/mobile
OperationalList markup, and two EvidencePanel instances with unique ARIA IDs.

```ts
const wrappers = mount({
  template: "<EvidencePanel title='A' /><EvidencePanel title='B' />",
  components: { EvidencePanel },
});
const ids = wrappers.findAll("[aria-labelledby]").map((node) => node.attributes("aria-labelledby"));
expect(new Set(ids).size).toBe(ids.length);
```

Also assert Arabic original text has `lang="ar"` and `dir="rtl"`.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
pnpm test:unit -- tests/templates.test.ts
```

Expected: module-not-found failures.

- [ ] **Step 3: Implement minimal reusable components**

`ResourceBoundary` accepts the existing `ResourceState`, message, request ID and retry
event, and exposes a default slot only in ready state. The three templates contain layout
slots only; no template may fetch data or infer permissions.

`GbosButton` and `GbosField` import only public `frappe-ui` exports. Remove the internal
source alias after the wrapper test proves the public import builds. `ConfirmDialog` must
use native dialog semantics or an accessible Frappe UI Dialog and must never call
`window.confirm`.

- [ ] **Step 4: Run focused GREEN and frontend static gates**

```bash
pnpm test:unit -- tests/templates.test.ts tests/components.test.ts
pnpm lint
pnpm typecheck
pnpm build
```

Expected: PASS.

- [ ] **Step 5: Commit the shared component system**

```bash
git add apps/esan_gbos/frontend/src/components/feedback/ResourceBoundary.vue \
  apps/esan_gbos/frontend/src/components/layout/PageHeader.vue \
  apps/esan_gbos/frontend/src/components/layout/DashboardTemplate.vue \
  apps/esan_gbos/frontend/src/components/layout/OperationalListTemplate.vue \
  apps/esan_gbos/frontend/src/components/layout/DetailCommandTemplate.vue \
  apps/esan_gbos/frontend/src/components/ui/GbosButton.vue \
  apps/esan_gbos/frontend/src/components/ui/GbosField.vue \
  apps/esan_gbos/frontend/src/components/ui/StatusBadge.vue \
  apps/esan_gbos/frontend/src/components/ui/ConfirmDialog.vue \
  apps/esan_gbos/frontend/src/components/data/OperationalList.vue \
  apps/esan_gbos/frontend/src/components/data/EvidencePanel.vue \
  apps/esan_gbos/frontend/tests/templates.test.ts \
  apps/esan_gbos/frontend/src/frappe-ui.d.ts \
  apps/esan_gbos/frontend/vite.config.ts \
  apps/esan_gbos/frontend/tailwind.config.js
git diff --cached --check
git commit -m "feat(pwa): add GBOS page templates"
```

## Chunk 2: Migrate every real page without inventing data

### Task 5: Build the product overview and compact CEO dashboard

**Files:**

- Modify: `apps/esan_gbos/frontend/src/views/OverviewView.vue`
- Create: `apps/esan_gbos/frontend/src/views/CeoDashboardView.vue`
- Create: `apps/esan_gbos/frontend/src/components/data/MetricTile.vue`
- Modify: `apps/esan_gbos/frontend/src/components/MetricCockpit.vue`
- Modify: `apps/esan_gbos/frontend/src/router.ts`
- Modify: `apps/esan_gbos/frontend/tests/metrics.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/app.test.ts`

- [ ] **Step 1: Add failing overview and metric-safety tests**

Assert:

- Overview renders only authorized modules and no `data-official-value` elements.
- Synthetic dashboard renders one compact source strip.
- Available metrics show a value; unavailable metrics never render a formal value.
- Freshness, coverage, reconciliation remain available in the tile/detail disclosure.
- CEO non-official observations, when absent, are not fabricated by the view.

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/metrics.test.ts tests/app.test.ts
```

Expected: failures for the new compact hierarchy and route component.

- [ ] **Step 3: Implement the approved dashboard hierarchy**

Use `DashboardTemplate` and `MetricTile`. Keep the current v3 Metrics API untouched.
Move definition version and full source lineage into a disclosure while keeping data mode,
freshness, coverage, and reconciliation visible without opening the disclosure.

- [ ] **Step 4: Run focused GREEN**

```bash
pnpm test:unit -- tests/metrics.test.ts tests/app.test.ts
pnpm typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit overview and CEO dashboard**

```bash
git add apps/esan_gbos/frontend/src/views/OverviewView.vue \
  apps/esan_gbos/frontend/src/views/CeoDashboardView.vue \
  apps/esan_gbos/frontend/src/components/data/MetricTile.vue \
  apps/esan_gbos/frontend/src/components/MetricCockpit.vue \
  apps/esan_gbos/frontend/src/router.ts \
  apps/esan_gbos/frontend/tests/metrics.test.ts \
  apps/esan_gbos/frontend/tests/app.test.ts
git diff --cached --check
git commit -m "feat(pwa): redesign the GBOS command center"
```

### Task 6: Replace the generic workspace with sales, purchase, and product views

**Files:**

- Create: `apps/esan_gbos/frontend/src/views/SalesWorkspaceView.vue`
- Create: `apps/esan_gbos/frontend/src/views/PurchaseWorkspaceView.vue`
- Create: `apps/esan_gbos/frontend/src/views/ProductWorkspaceView.vue`
- Create: `apps/esan_gbos/frontend/src/components/data/SourcingComparison.vue`
- Create: `apps/esan_gbos/frontend/tests/workspaces.test.ts`
- Modify: `apps/esan_gbos/frontend/src/router.ts`
- Modify: `apps/esan_gbos/frontend/src/presentation.ts`
- Delete after GREEN: `apps/esan_gbos/frontend/src/views/WorkspaceView.vue`

- [ ] **Step 1: Write failing page-specific tests**

Sales assertions: title, owner, status, due date, next action, related Party/Sample link, and
cursor pagination. Purchase assertions: sourcing lane, candidate status, quoted-price
snapshot label, currency, lead time, and no invented supplier score. Product assertions:
honest “产品与样品工作项” label and no Product Brief/Sample index claim.

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/workspaces.test.ts
```

Expected: missing page components.

- [ ] **Step 3: Implement page-specific views**

Use existing `listWorkItems` and `getSourcingBoard` calls only. Preserve cursor values and
append/replace data according to explicit pagination controls; do not infer missing totals.
Use `SourcingComparison` only with actual candidate fields.

- [ ] **Step 4: Run focused GREEN and route regression**

```bash
pnpm test:unit -- tests/workspaces.test.ts tests/navigation.test.ts tests/app.test.ts
pnpm typecheck
```

Expected: PASS and no import of `WorkspaceView.vue` remains.

- [ ] **Step 5: Delete the obsolete generic view and commit**

Delete `WorkspaceView.vue` with `apply_patch`, then:

```bash
git add apps/esan_gbos/frontend/src/views/SalesWorkspaceView.vue \
  apps/esan_gbos/frontend/src/views/PurchaseWorkspaceView.vue \
  apps/esan_gbos/frontend/src/views/ProductWorkspaceView.vue \
  apps/esan_gbos/frontend/src/views/WorkspaceView.vue \
  apps/esan_gbos/frontend/src/components/data/SourcingComparison.vue \
  apps/esan_gbos/frontend/tests/workspaces.test.ts \
  apps/esan_gbos/frontend/src/router.ts \
  apps/esan_gbos/frontend/src/presentation.ts
git diff --cached --check
git commit -m "feat(pwa): add domain-specific GBOS workspaces"
```

### Task 7: Redesign communication list and detail around evidence

**Files:**

- Modify: `apps/esan_gbos/frontend/src/views/CommunicationsView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/CommunicationDetailView.vue`
- Modify: `apps/esan_gbos/frontend/tests/v4.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/components.test.ts`

- [ ] **Step 1: Add failing communication layout tests**

Assert aligned channel/time/status/team columns, cursor pagination, compact filters, summary
before evidence, Restricted raw hidden by default, and `dir="rtl"` for Arabic original text.
Assert fact proposals and association suggestions remain proposals.

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/v4.test.ts tests/components.test.ts
```

Expected: failures for the new list and EvidencePanel structure.

- [ ] **Step 3: Migrate both pages**

Use `OperationalListTemplate` for the list and `DetailCommandTemplate` plus
`EvidencePanel` for detail. Do not add confirm/reject fact buttons because those commands
do not exist in the current safe BFF.

- [ ] **Step 4: Run focused GREEN**

```bash
pnpm test:unit -- tests/v4.test.ts tests/components.test.ts
pnpm typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit communication redesign**

```bash
git add apps/esan_gbos/frontend/src/views/CommunicationsView.vue \
  apps/esan_gbos/frontend/src/views/CommunicationDetailView.vue \
  apps/esan_gbos/frontend/tests/v4.test.ts \
  apps/esan_gbos/frontend/tests/components.test.ts
git diff --cached --check
git commit -m "feat(pwa): redesign communication intelligence views"
```

### Task 8: Make AI Draft, review, and integrations independently resilient

**Files:**

- Modify: `apps/esan_gbos/frontend/src/views/ReviewQueueView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/ReviewDetailView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/IntegrationsView.vue`
- Modify: `apps/esan_gbos/frontend/src/components/ReviewDecisionForm.vue`
- Modify: `apps/esan_gbos/frontend/tests/review.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/v4.test.ts`

- [ ] **Step 1: Write failing partial-outage and command tests**

Cover:

- Agent draft enrichment failure does not hide Frappe Review Cases.
- Model-usage failure does not hide connector status.
- Review pagination keeps `next_cursor`.
- Review reason has persistent help text, character feedback and `aria-describedby`.
- Integration controls use ConfirmDialog, block duplicate submission, and refresh revision.
- Command errors use `role="alert"`; successful outcomes use `role="status"`.

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/review.test.ts tests/v4.test.ts
```

Expected: failures for independent resource handling and form semantics.

- [ ] **Step 3: Split resources and migrate templates**

Each remote resource receives its own `useOnlineResource` and ResourceBoundary. A failure
in one optional service must not convert the whole page to an error state. Keep existing
revision/hash/policy/idempotency requests unchanged.

- [ ] **Step 4: Run focused GREEN**

```bash
pnpm test:unit -- tests/review.test.ts tests/v4.test.ts
pnpm typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit AI/review/integration redesign**

```bash
git add apps/esan_gbos/frontend/src/views/ReviewQueueView.vue \
  apps/esan_gbos/frontend/src/views/ReviewDetailView.vue \
  apps/esan_gbos/frontend/src/views/IntegrationsView.vue \
  apps/esan_gbos/frontend/src/components/ReviewDecisionForm.vue \
  apps/esan_gbos/frontend/tests/review.test.ts \
  apps/esan_gbos/frontend/tests/v4.test.ts
git diff --cached --check
git commit -m "feat(pwa): harden AI review and integration workspaces"
```

### Task 9: Migrate Customer 360 and Sample Detail

**Files:**

- Create: `apps/esan_gbos/frontend/src/components/data/ObjectSummary.vue`
- Create: `apps/esan_gbos/frontend/src/components/data/Timeline.vue`
- Modify: `apps/esan_gbos/frontend/src/views/PartyDetailView.vue`
- Modify: `apps/esan_gbos/frontend/src/views/SampleDetailView.vue`
- Modify: `apps/esan_gbos/frontend/tests/app.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/components.test.ts`

- [ ] **Step 1: Add failing detail-template tests**

Assert Customer 360 keeps profile, CRM organization/contact/lead/deal, briefs, samples and
demands distinguishable. Assert Sample Detail keeps project revision, iterations,
shipments, feedback, authorized `Sent` command, late-response protection, and no stale
revision after route change.

Add separate success/error semantics for sample feedback.

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/app.test.ts tests/components.test.ts
```

Expected: failures for the new ObjectSummary/Timeline hierarchy and error alert.

- [ ] **Step 3: Implement detail templates without flattening meaning**

Use typed sections instead of the generic RecordGrid. Preserve the existing BFF payload;
do not create a Party list, quotation timeline, or supplier data from missing fields.

- [ ] **Step 4: Run focused GREEN**

```bash
pnpm test:unit -- tests/app.test.ts tests/components.test.ts
pnpm typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit detail migration**

```bash
git add apps/esan_gbos/frontend/src/components/data/ObjectSummary.vue \
  apps/esan_gbos/frontend/src/components/data/Timeline.vue \
  apps/esan_gbos/frontend/src/views/PartyDetailView.vue \
  apps/esan_gbos/frontend/src/views/SampleDetailView.vue \
  apps/esan_gbos/frontend/tests/app.test.ts \
  apps/esan_gbos/frontend/tests/components.test.ts
git diff --cached --check
git commit -m "feat(pwa): redesign customer and sample details"
```

## Chunk 3: Theme cleanup, full validation, and local-site certification

### Task 10: Replace the legacy theme and remove obsolete generic components

**Files:**

- Modify: `apps/esan_gbos/frontend/src/styles.css`
- Modify: `apps/esan_gbos/frontend/index.html`
- Modify: `apps/esan_gbos/frontend/public/manifest.webmanifest`
- Modify: `apps/esan_gbos/frontend/public/icon.svg`
- Modify: `apps/esan_gbos/esan_gbos/www/gbos.html`
- Create: `apps/esan_gbos/frontend/tests/theme.test.ts`
- Delete after zero imports: `apps/esan_gbos/frontend/src/components/RecordGrid.vue`
- Delete or replace after zero imports: `apps/esan_gbos/frontend/src/components/EvidenceCard.vue`

- [ ] **Step 1: Write a failing theme/source-boundary test**

Add to `templates.test.ts` or a new `theme.test.ts`:

```ts
expect(manifest.theme_color).toBe("#0B1220");
expect(manifest.background_color).toBe("#F6F8FB");
expect(shellTemplate).toContain('content="#0B1220"');
expect(legacyCss).not.toContain("--forest:");
expect(legacyCss).not.toContain("font-family: Georgia");
```

- [ ] **Step 2: Run RED**

```bash
pnpm test:unit -- tests/theme.test.ts
```

Expected: old green/gold theme assertions fail.

- [ ] **Step 3: Reduce global CSS and update PWA assets**

Keep `styles.css` as the Tailwind directives plus any explicitly documented compatibility
rules. Delete obsolete broad `nav`, `footer`, `blockquote`, card-grid and old theme rules
only after `rg` confirms no remaining class users.

Update theme meta, manifest, and icon to approved purple/sidebar colors. The icon remains
local SVG and includes a maskable safe area.

- [ ] **Step 4: Verify zero obsolete imports and GREEN**

```bash
rg -n "WorkspaceView|RecordGrid|EvidenceCard|--forest|Georgia" \
  apps/esan_gbos/frontend/src apps/esan_gbos/frontend/tests
pnpm test:unit -- tests/theme.test.ts tests/templates.test.ts
pnpm lint
pnpm typecheck
pnpm build
```

Expected: `rg` returns no obsolete component/theme references; all gates pass.

- [ ] **Step 5: Commit theme cleanup**

```bash
git add apps/esan_gbos/frontend/src/styles.css \
  apps/esan_gbos/frontend/index.html \
  apps/esan_gbos/frontend/public/manifest.webmanifest \
  apps/esan_gbos/frontend/public/icon.svg \
  apps/esan_gbos/esan_gbos/www/gbos.html \
  apps/esan_gbos/frontend/tests/theme.test.ts \
  apps/esan_gbos/frontend/src/components/RecordGrid.vue \
  apps/esan_gbos/frontend/src/components/EvidenceCard.vue
git diff --cached --check
git commit -m "refactor(pwa): retire the Gate prototype theme"
```

### Task 11: Expand strict unit and harness E2E coverage

**Files:**

- Modify: `apps/esan_gbos/frontend/e2e/gbos.spec.ts`
- Modify: `apps/esan_gbos/frontend/playwright.config.ts`
- Modify: `apps/esan_gbos/frontend/tests/service-worker.test.ts`
- Modify: `apps/esan_gbos/frontend/tests/navigation.test.ts`
- Modify: `apps/esan_gbos/frontend/package.json` only if a deterministic test script is needed

- [ ] **Step 1: Make the harness fail on unknown endpoints**

Replace the fallback work-item envelope with an exact method/path dispatch map. Add a test
route that intentionally requests an unknown endpoint and prove the harness fails before
removing that intentional probe.

- [ ] **Step 2: Add failing route/role/viewport expectations**

Cover Guest, Sales User/Manager, Buyer/Purchase Manager, Product/R&D, Reviewer,
Integration Admin, CEO, and GBOS Admin. Cover all real routes at 320, 375, 768, 1024,
and 1440px. Add drawer keyboard path, skip-link activation, focus return, no-overflow,
and all ready/error/permission/offline states.

- [ ] **Step 3: Run RED**

```bash
pnpm test:e2e
```

Expected: failures until shell/page selectors and mock whitelist are complete.

- [ ] **Step 4: Complete the harness and no-persistence checks**

Wait for Service Worker control, load a sensitive detail route, go offline, reload the deep
link, and assert only shell + “需要联网” appears. Inspect Cache Storage request/response
bodies, local/session storage, and every IndexedDB database. Assert no business payload,
draft, review reason, or Restricted content persists.

Add `console`, `pageerror`, `requestfailed`, axe Critical/Serious, heading, unique ID, and
keyboard assertions.

- [ ] **Step 5: Run the full frontend matrix**

```bash
pnpm lint
pnpm typecheck
pnpm test:unit
pnpm build
pnpm test:e2e
```

Expected: all commands pass; Vitest count is at least the previous 88 tests; harness covers
all current routes with no unknown API fallback.

- [ ] **Step 6: Commit strict frontend verification**

```bash
git add apps/esan_gbos/frontend/e2e/gbos.spec.ts \
  apps/esan_gbos/frontend/playwright.config.ts \
  apps/esan_gbos/frontend/tests/service-worker.test.ts \
  apps/esan_gbos/frontend/tests/navigation.test.ts \
  apps/esan_gbos/frontend/package.json
git diff --cached --check
git commit -m "test(pwa): certify the responsive GBOS interface"
```

### Task 12: Rebuild and certify the current local Frappe site

**Files:**

- Modify only if results require it: `docs/evidence/local-pilot/local-pilot-evidence.json`
- Modify only if results require it: `docs/evidence/local-pilot/local-pilot-summary.md`
- Modify only if evidence files change: `docs/evidence/local-pilot/SHA256SUMS`

- [ ] **Step 1: Verify the exact source and working tree before image build**

```bash
git status --short --branch
git rev-parse HEAD
```

Expected: clean worktree on the frontend redesign branch.

- [ ] **Step 2: Build and record the local Frappe image through the governed helper**

Use the existing local-pilot image build command documented in `infra/local/README.md`.
Do not substitute an unrecorded image, do not push an image, and do not enable formal
`local_pilot_go`.

Expected: image lock contains the exact local image ID/digest and current source commit.

- [ ] **Step 3: Run migrations twice and restart only the local synthetic core**

Use the existing `start-synthetic --acknowledge-synthetic` workflow. Do not enable email,
WeCom, WhatsApp, DeepSeek, media, Kingdee, tunnel, or cloud profiles.

Expected: Frappe site healthy, apps installed, two consecutive migrations successful.

- [ ] **Step 4: Run real-site role smoke tests**

Prepare ephemeral storage states for CEO, Sales User, Purchase Manager/Buyer,
Product/R&D, Reviewer, and Integration Admin. Run:

```bash
GBOS_E2E_BASE_URL=http://127.0.0.1:58080 \
GBOS_E2E_STORAGE_STATE=/absolute/path/to/role-state.json \
pnpm test:e2e:site
```

Run the appropriate role state for its route matrix. Expected:

- CEO opens all authorized domains and details under its own identity.
- Each normal role sees only its domain.
- Integration Admin opens integrations through the real Frappe shell.
- No route stores business responses in browser persistence.
- Console/page errors and failed unexpected requests are zero.

- [ ] **Step 5: Run backend and frontend final regression**

From repository root:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy services
```

From frontend:

```bash
pnpm lint
pnpm typecheck
pnpm test:unit
pnpm build
pnpm test:e2e
```

Expected: all green. Report skipped opt-in integrations separately; do not call them
passed.

- [ ] **Step 6: Update current-HEAD evidence only after observed success**

Record exact HEAD, image digest, commands, counts, browser roles, viewports, console state,
and limitations. Preserve:

- `local_pilot_go=false`
- `production_go=false`
- real channels No-Go
- DeepSeek No-Go unless separately canaried
- Kingdee/cloud/external-send No-Go

Refresh both evidence checksums and verify them from the evidence directory.

- [ ] **Step 7: Commit truthful evidence separately**

```bash
git add docs/evidence/local-pilot/local-pilot-evidence.json \
  docs/evidence/local-pilot/local-pilot-summary.md \
  docs/evidence/local-pilot/SHA256SUMS
git diff --cached --check
git commit -m "docs(pwa): certify the responsive local interface"
```

If real-site certification does not pass, do not update or commit evidence. Report the
exact blocker and leave the formal local-pilot gate closed.

## Final acceptance checklist

- [ ] Approved dark sidebar, tablet rail, mobile drawer, and bottom navigation are present.
- [ ] Desktop and mobile use the same authorization model but purpose-built layouts.
- [ ] Main pages use compact aligned fields; explanatory details are progressively disclosed.
- [ ] `/gbos` is a truthful role-aware entry without fake business values.
- [ ] Existing business routes and BFF contracts remain compatible.
- [ ] Synthetic and unavailable metric states cannot be mistaken for official values.
- [ ] Sales, purchase, product, communication, review, integration, party, and sample pages
      no longer depend on the generic WorkspaceView/RecordGrid abstraction.
- [ ] Integration Admin shell, navigation, route, and API contracts agree.
- [ ] All API, CSRF, revision, idempotency, evidence, and no-store boundaries remain intact.
- [ ] 320/375/768/1024/1440px and 200% zoom have no horizontal overflow or hidden commands.
- [ ] Critical/Serious axe findings, duplicate IDs, console errors, page errors, and unknown
      harness API requests are zero.
- [ ] Unit, build, harness E2E, and real Frappe-site smoke tests pass on the exact final HEAD.
- [ ] Formal local-pilot, production, channels, model, Kingdee, cloud, and external-send
      gates remain closed unless separately authorized and evidenced.
