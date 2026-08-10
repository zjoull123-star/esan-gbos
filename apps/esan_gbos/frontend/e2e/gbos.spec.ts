import axe from "axe-core";
import {
  expect,
  test,
  type Page,
  type Route,
  type TestInfo,
} from "@playwright/test";
import { readFileSync } from "node:fs";

import {
  BFF_ENDPOINTS,
  BFF_V2_ENDPOINTS,
  BFF_V3_ENDPOINTS,
  BFF_V4_ENDPOINTS,
} from "../src/api/bff";

const liveBaseUrl = process.env.GBOS_E2E_BASE_URL;
const liveStorageState = process.env.GBOS_E2E_STORAGE_STATE;
const harnessEntry = "/assets/esan_gbos/frontend/";
const builtHarnessShell = readFileSync(
  new URL("../dist/index.html", import.meta.url),
  "utf8",
);

const workspaces = [
  { path: "/gbos/ceo", navigationLabel: "经营总览", heading: "经营总览" },
  { path: "/gbos/sales", navigationLabel: "销售协同", heading: "销售工作项" },
  {
    path: "/gbos/purchase",
    navigationLabel: "采购协同",
    heading: "采购询源工作台",
  },
  {
    path: "/gbos/product",
    navigationLabel: "产品与样品",
    heading: "产品与样品工作项",
  },
  { path: "/gbos/review", navigationLabel: "审核队列", heading: "人工审核队列" },
] as const;

const navigationHeadings = [
  "经营总览",
  "销售协同",
  "采购协同",
  "产品与样品",
  "审核队列",
  "沟通观察",
  "集成状态",
] as const;

const allNavigationLabels = [
  "经营总览",
  "销售协同",
  "采购协同",
  "产品与样品",
  "审核队列",
  "集成状态",
  "沟通观察",
] as const;

const roleCases = [
  { name: "Guest", roles: [], navigation: [], deniedPath: "/gbos/ceo" },
  {
    name: "Sales User",
    roles: ["Sales User"],
    navigation: ["销售协同", "沟通观察"],
    deniedPath: "/gbos/purchase",
  },
  {
    name: "Sales Manager",
    roles: ["Sales Manager"],
    navigation: ["销售协同", "沟通观察"],
    deniedPath: "/gbos/integrations",
  },
  {
    name: "Buyer",
    roles: ["Buyer"],
    navigation: ["采购协同"],
    deniedPath: "/gbos/sales",
  },
  {
    name: "Purchase Manager",
    roles: ["Purchase Manager"],
    navigation: ["采购协同"],
    deniedPath: "/gbos/review",
  },
  {
    name: "Product/R&D",
    roles: ["Product/R&D"],
    navigation: ["产品与样品"],
    deniedPath: "/gbos/communications",
  },
  {
    name: "Reviewer",
    roles: ["Reviewer"],
    navigation: ["审核队列"],
    deniedPath: "/gbos/party/PARTY-E2E",
  },
  {
    name: "Integration Admin",
    roles: ["Integration Admin"],
    navigation: ["集成状态", "沟通观察"],
    deniedPath: "/gbos/sample/SAMPLE-E2E",
  },
  { name: "CEO", roles: ["CEO"], navigation: [...allNavigationLabels] },
  { name: "GBOS Admin", roles: ["GBOS Admin"], navigation: [...allNavigationLabels] },
] as const;

const allRoutes = [
  { path: "/gbos", heading: "产品总览" },
  { path: "/gbos/ceo", heading: "经营总览" },
  { path: "/gbos/sales", heading: "销售工作项" },
  { path: "/gbos/purchase", heading: "采购询源工作台" },
  { path: "/gbos/product", heading: "产品与样品工作项" },
  { path: "/gbos/review", heading: "人工审核队列" },
  { path: "/gbos/integrations", heading: "集成状态" },
  { path: "/gbos/communications", heading: "沟通观察" },
  {
    path: "/gbos/communications/OBS-E2E-1",
    heading: "沟通观察详情",
  },
  { path: "/gbos/review/REVIEW-E2E-1", heading: "审核案件" },
  { path: "/gbos/party/PARTY-E2E", heading: "客户 360" },
  { path: "/gbos/sample/SAMPLE-E2E", heading: "样品状态" },
] as const;

const syntheticWorkEnvelope = {
  message: {
    data: [
      {
        name: "WORK-E2E-1",
        title: "SALES-ONLY · 确认客户柑橘香调",
        summary_zh: "客户偏好清新的柑橘香调。",
        original_text: "نفضل رائحة حمضيات منعشة",
        original_language: "ar",
        origin: "Fixture",
        business_status: "Open",
        revision: 1,
        reference_doctype: "GBOS Party Profile",
        reference_name: "PARTY-E2E",
      },
      {
        name: "WORK-E2E-2",
        title: "PRODUCT-ONLY · 核对样品状态",
        origin: "Fixture",
        business_status: "Open",
        revision: 1,
        reference_doctype: "GBOS Sample Project",
        reference_name: "SAMPLE-E2E",
      },
    ],
    meta: {
      request_id: "req-e2e-synthetic",
      schema_version: "1.0",
    },
  },
};

const syntheticSourcingEnvelope = {
  message: {
    data: {
      lanes: {
        Draft: [
          {
            name: "SRC-E2E-1",
            title: "PURCHASE-ONLY · 玻璃瓶询源",
            origin: "Fixture",
            business_status: "Draft",
            revision: 1,
            candidates: [],
          },
        ],
        Invited: [],
        Collecting: [],
        Evaluating: [],
        Selected: [],
        Closed: [],
        Cancelled: [],
      },
      total: 1,
    },
    meta: {
      request_id: "req-e2e-sourcing",
      schema_version: "1.0",
    },
  },
};

const syntheticReviewEnvelope = {
  message: {
    data: {
      cases: [
        {
          name: "REVIEW-E2E-1",
          title: "REVIEW-ONLY · 确认客户反馈事实",
          assigned_reviewer: "gbos.admin.synthetic@example.invalid",
          review_status: "Pending",
          case_revision: 1,
          case_payload_hash: "a".repeat(64),
          subject: {
            doctype: "GBOS Sample Feedback",
            name: "FEEDBACK-E2E-1",
            revision: 1,
            payload_hash: "b".repeat(64),
            snapshot: { title: "合成审核主体" },
          },
          evidence: [
            {
              evidence_type: "Evidence",
              reference: "EVIDENCE-E2E-1",
              payload_hash: "c".repeat(64),
            },
          ],
          policy_reference: "gbos-action-policy@1.0.0",
          origin: "Fixture",
        },
      ],
      total: 1,
      page_size: 20,
      next_cursor: null,
    },
    meta: {
      request_id: "req-e2e-review",
      schema_version: "1.0",
    },
  },
};

const syntheticMetricEnvelope = {
  message: {
    data: {
      schema_version: "3.0",
      site_id: "gbos.localhost",
      source_mode: "synthetic",
      synthetic: true,
      generated_at: "2026-08-06T02:31:00Z",
      metrics: [
        {
          schema_version: "3.0",
          metric_key: "sales.order_value",
          display_name: "销售订单金额",
          definition_version: "0.1.0",
          site_id: "gbos.localhost",
          status: "available",
          value: 125000,
          unit: "CNY",
          as_of: "2026-08-06T02:30:00Z",
          queried_at: "2026-08-06T02:31:00Z",
          window: {
            type: "calendar",
            grain: "month",
            start: "2026-08-01T00:00:00Z",
            end: "2026-09-01T00:00:00Z",
          },
          freshness: { status: "fresh", age_seconds: 60, slo_seconds: 86400 },
          coverage: {
            status: "sufficient",
            ratio: 1,
            included_count: 4,
            total_count: 4,
          },
          reconciliation: {
            status: "passed",
            checked_at: "2026-08-06T02:30:30Z",
            reference: "reconciliation-SYNTH-001",
            variance: 0,
          },
          source_lineage: [
            {
              source_system: "synthetic_kingdee_projection",
              source_record_refs: ["sales-order-projection-SYNTH-001"],
              retrieved_at: "2026-08-06T02:30:00Z",
              transformation_version: "metrics-projection-v1",
              evidence_status: "synthetic",
            },
          ],
          source_mode: "synthetic",
          synthetic: true,
          governed_sources: true,
        },
        {
          schema_version: "3.0",
          metric_key: "receivables.balance",
          display_name: "应收余额",
          definition_version: "0.1.0",
          site_id: "gbos.localhost",
          status: "unavailable",
          unavailable_reason: "reconciliation_failed",
          as_of: "2026-08-06T02:30:00Z",
          queried_at: "2026-08-06T02:31:00Z",
          window: {
            type: "point_in_time",
            grain: "instant",
            start: "2026-08-06T02:30:00Z",
            end: "2026-08-06T02:30:00Z",
          },
          freshness: { status: "fresh", age_seconds: 60, slo_seconds: 86400 },
          coverage: {
            status: "sufficient",
            ratio: 1,
            included_count: 3,
            total_count: 3,
          },
          reconciliation: {
            status: "failed",
            checked_at: "2026-08-06T02:30:30Z",
            reference: "reconciliation-SYNTH-002",
            variance: 10,
          },
          source_lineage: [
            {
              source_system: "synthetic_kingdee_projection",
              source_record_refs: ["receivable-projection-SYNTH-001"],
              retrieved_at: "2026-08-06T02:30:00Z",
              transformation_version: "metrics-projection-v1",
              evidence_status: "synthetic",
            },
          ],
          source_mode: "synthetic",
          synthetic: true,
          governed_sources: true,
        },
      ],
    },
    meta: {
      request_id: "req-e2e-metrics",
      schema_version: "1.0",
    },
  },
};

const v4Envelope = <T>(data: T) => ({
  message: {
    data,
    meta: { request_id: "req-e2e-v4", schema_version: "4.0" },
  },
});

const syntheticIntegrationEnvelope = v4Envelope({
  connectors: [
    {
      instance_id: "whatsapp-e2e",
      channel: "WhatsApp",
      status: "enabled",
      checkpoint_version: 4,
      backlog: 2,
      last_success_at: "2026-08-07T02:00:00Z",
      safe_error_code: null,
      freshness: "fresh",
      revision: 3,
    },
  ],
});

const syntheticUsageEnvelope = v4Envelope({
  model: "deepseek-v4-flash",
  period: "2026-08",
  tokens: 1200,
  token_state: "known",
  cost: { currency: "USD", amount: null, state: "unknown" },
  soft_limit_usd: 50,
  hard_limit_usd: 100,
  state: "normal",
});

const communication = {
  observation_id: "OBS-E2E-1",
  channel: "WhatsApp",
  occurred_at: "2026-08-07T02:00:00Z",
  summary_zh: "客户询问下一轮样品交期。",
  original_language: "ar",
  classification: "CEO Informal Observation",
  review_status: "Unreviewed",
  team_ref: "TEAM-E2E",
  party_ref: "PARTY-E2E",
  evidence_count: 1,
};

const syntheticCommunicationListEnvelope = v4Envelope({
  communications: [communication],
  next_cursor: null,
});

const syntheticCommunicationDetailEnvelope = v4Envelope({
  communication: {
    ...communication,
    evidence: [{ ref: "EVID-E2E-1", locator: "message:42" }],
    fact_proposals: [
      {
        status: "Proposed",
        confidence: 0.82,
        type: "Requested Delivery Date",
        value_display: "2026-08-20",
      },
    ],
    association_suggestions: [
      {
        type: "Party",
        confidence: 0.9,
        suggestion_key: `suggestion:v1:${"a".repeat(64)}`,
      },
    ],
    participant_identities: [
      {
        identity_ref: "extid:v1:email:N6juwc4ZaH0TL-KQUdymKdFk4sSVi6FB1fQTOjPwaI8",
        provider: "email",
        status: "unresolved",
      },
    ],
    model: { name: "deepseek-v4-flash", version: "2026-08-01" },
    raw_access_allowed: false,
  },
});

const syntheticAiDraftEnvelope = v4Envelope({
  drafts: [],
  next_cursor: null,
});

const syntheticIdentityStateEnvelope = v4Envelope({
  identities: [
    {
      identity_ref: "extid:v1:email:N6juwc4ZaH0TL-KQUdymKdFk4sSVi6FB1fQTOjPwaI8",
      provider: "email",
      status: "unresolved",
    },
  ],
  connector_account_owner: { display_label: "渠道账号负责人" },
});

const syntheticIdentityCandidateEnvelope = v4Envelope({
  candidates: [
    {
      candidate_type: "Party",
      candidate_ref: "PROTECTED-PARTY-E2E",
      display_label: "海湾香氛客户",
    },
  ],
  eligible_reviewers: [
    { reviewer_ref: "REVIEWER-E2E", display_label: "合格审核人" },
  ],
  has_more: false,
});

const syntheticIdentityReview = {
  review_case_ref: "IDENTITY-REVIEW-E2E",
  review_case_revision: 3,
  status: "pending",
  assigned_reviewer: "REVIEWER-E2E",
  team_ref: "TEAM-E2E",
  mapping_ref: "MAPPING-E2E",
  mapping_revision: 2,
  target: {
    candidate_type: "Party",
    candidate_ref: "PROTECTED-PARTY-E2E",
    display_label: "海湾香氛客户",
  },
  evidence_refs: ["EVID-IDENTITY-E2E"],
  policy_version: "identity-resolution-v1",
};

const syntheticIdentityReviewListEnvelope = v4Envelope({
  reviews: [syntheticIdentityReview],
  has_more: false,
});
const syntheticIdentityReviewDetailEnvelope = v4Envelope({
  review: syntheticIdentityReview,
});
const syntheticIdentityCommandEnvelope = v4Envelope({
  status: "pending",
  mapping_ref: "MAPPING-E2E",
  mapping_revision: 2,
  review_case_ref: "IDENTITY-REVIEW-E2E",
  review_case_revision: 3,
});

const v1Envelope = <T>(data: T) => ({
  message: {
    data,
    meta: { request_id: "req-e2e-v1", schema_version: "1.0" },
  },
});

const syntheticReviewCase = syntheticReviewEnvelope.message.data.cases[0];
const syntheticReviewDetailEnvelope = v1Envelope({ case: syntheticReviewCase });
const syntheticIdentityGenericReviewCase = {
  ...syntheticReviewCase,
  name: "IDENTITY-REVIEW-E2E",
  title: "Identity Resolution",
  team: "TEAM-E2E",
  assigned_reviewer: "REVIEWER-E2E",
  case_revision: 3,
  subject: {
    doctype: "GBOS External Identity",
    name: "protected:identity-subject",
    revision: 2,
    payload_hash: "d".repeat(64),
    snapshot: {},
  },
  evidence: [{ evidence_type: "Evidence", reference: "EVID-IDENTITY-E2E" }],
  policy_reference: "identity-resolution-v1",
  origin: "Manual",
};
const syntheticIdentityGenericReviewDetailEnvelope = v1Envelope({
  case: syntheticIdentityGenericReviewCase,
  decision: null,
});
const syntheticIdentityGenericDecisionEnvelope = v1Envelope({
  case: {
    ...syntheticIdentityGenericReviewCase,
    review_status: "Rejected",
    case_revision: 4,
    decision_note: "当前证据不足。",
  },
  decision: {
    name: "IDENTITY-DECISION-E2E",
    decision: "Rejected",
    subject_doctype: "GBOS External Identity",
    subject_name: "protected:identity-subject",
  },
});
const syntheticPartyEnvelope = v1Envelope({
  profile: {
    name: "PARTY-E2E",
    party_name: "E2E 客户",
    team: "TEAM-E2E",
    origin: "Fixture",
    business_status: "Active",
    revision: 1,
  },
  organization: { name: "ORG-E2E", organization_name: "E2E Organization" },
  contact: { name: "CONTACT-E2E", full_name: "E2E Contact" },
  lead: null,
  deal: null,
  product_briefs: [],
  samples: [{ name: "SAMPLE-E2E", title: "E2E 样品", business_status: "Sent" }],
  demands: [],
});
const syntheticSampleEnvelope = v1Envelope({
  project: {
    name: "SAMPLE-E2E",
    title: "E2E 样品",
    party_profile: "PARTY-E2E",
    team: "TEAM-E2E",
    origin: "Fixture",
    business_status: "Sent",
    review_status: "Confirmed",
    revision: 2,
  },
  iterations: [],
  shipments: [],
  feedback: [],
});
const syntheticConnector = syntheticIntegrationEnvelope.message.data.connectors[0];
const syntheticAiDraftDetailEnvelope = v4Envelope({
  draft: {
    draft_id: "DRAFT-E2E-1",
    kind: "Work Item",
    subject: "E2E AI Draft",
    status: "Pending",
    origin: "AI",
    revision: 2,
    evidence: [],
    model: { name: "deepseek-v4-flash", version: "2026-08-01" },
  },
});

const harnessFixtures = new Map<string, unknown>([
  [`GET ${BFF_ENDPOINTS.party360}`, syntheticPartyEnvelope],
  [`GET ${BFF_ENDPOINTS.workItemList}`, syntheticWorkEnvelope],
  [`GET ${BFF_ENDPOINTS.sampleStatus}`, syntheticSampleEnvelope],
  [`GET ${BFF_ENDPOINTS.sourcingBoard}`, syntheticSourcingEnvelope],
  [`POST ${BFF_ENDPOINTS.sampleCreate}`, v1Envelope({ name: "SAMPLE-E2E" })],
  [`POST ${BFF_ENDPOINTS.sampleFeedback}`, v1Envelope({ name: "FEEDBACK-E2E" })],
  [`POST ${BFF_ENDPOINTS.sourcingCreate}`, v1Envelope({ name: "SRC-E2E-1" })],
  [`POST ${BFF_ENDPOINTS.workItemTransition}`, v1Envelope({ name: "WORK-E2E-1" })],
  [`GET ${BFF_V2_ENDPOINTS.reviewList}`, syntheticReviewEnvelope],
  [`GET ${BFF_V2_ENDPOINTS.reviewGet}`, syntheticReviewDetailEnvelope],
  [
    `POST ${BFF_V2_ENDPOINTS.reviewDecide}`,
    v1Envelope({
      case: { ...syntheticReviewCase, review_status: "Approved", case_revision: 2 },
    }),
  ],
  [`GET ${BFF_V3_ENDPOINTS.metricsDashboard}`, syntheticMetricEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.integrationListStatus}`, syntheticIntegrationEnvelope],
  [
    `POST ${BFF_V4_ENDPOINTS.integrationPause}`,
    v4Envelope({ ...syntheticConnector, status: "paused", revision: 4 }),
  ],
  [
    `POST ${BFF_V4_ENDPOINTS.integrationResume}`,
    v4Envelope({ ...syntheticConnector, status: "enabled", revision: 4 }),
  ],
  [
    `POST ${BFF_V4_ENDPOINTS.integrationReplay}`,
    v4Envelope({ ...syntheticConnector, revision: 4 }),
  ],
  [`GET ${BFF_V4_ENDPOINTS.communicationList}`, syntheticCommunicationListEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.communicationGet}`, syntheticCommunicationDetailEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.modelGetUsage}`, syntheticUsageEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.aiDraftList}`, syntheticAiDraftEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.aiDraftGet}`, syntheticAiDraftDetailEnvelope],
  [`POST ${BFF_V4_ENDPOINTS.aiDraftSubmitForReview}`, syntheticAiDraftDetailEnvelope],
  [`GET ${BFF_V4_ENDPOINTS.identityListStates}`, syntheticIdentityStateEnvelope],
  [
    `GET ${BFF_V4_ENDPOINTS.identityGetState}`,
    v4Envelope({
      identity: syntheticIdentityStateEnvelope.message.data.identities[0],
      connector_account_owner:
        syntheticIdentityStateEnvelope.message.data.connector_account_owner,
    }),
  ],
  [`GET ${BFF_V4_ENDPOINTS.identityListCandidates}`, syntheticIdentityCandidateEnvelope],
  [
    `GET ${BFF_V4_ENDPOINTS.identityListPendingReviews}`,
    syntheticIdentityReviewListEnvelope,
  ],
  [
    `GET ${BFF_V4_ENDPOINTS.identityGetPendingReview}`,
    syntheticIdentityReviewDetailEnvelope,
  ],
  [`POST ${BFF_V4_ENDPOINTS.identitySubmitForReview}`, syntheticIdentityCommandEnvelope],
  [
    `POST ${BFF_V4_ENDPOINTS.identityRevoke}`,
    v4Envelope({ status: "revoked", mapping_ref: "MAPPING-E2E", mapping_revision: 3 }),
  ],
]);

const isHarness = (testInfo: TestInfo) =>
  testInfo.project.name === "frontend-harness";

interface HarnessDiagnostics {
  consoleErrors: string[];
  pageErrors: string[];
  requestFailures: string[];
  unknownApiRequests: string[];
  sessionConfigured: boolean;
}

const diagnosticsByPage = new WeakMap<Page, HarnessDiagnostics>();

const prepareHarness = async (page: Page) => {
  const diagnostics: HarnessDiagnostics = {
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    unknownApiRequests: [],
    sessionConfigured: false,
  };
  diagnosticsByPage.set(page, diagnostics);
  page.on("console", (message) => {
    if (message.type() === "error") {
      const location = message.location().url;
      diagnostics.consoleErrors.push(
        location ? `${message.text()} @ ${location}` : message.text(),
      );
    }
  });
  page.on("pageerror", (error) => diagnostics.pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    diagnostics.requestFailures.push(
      `${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText ?? "unknown"}`,
    );
  });
  await page.route("**/api/method/**", async (route) => {
    const request = route.request();
    const key = `${request.method()} ${new URL(request.url()).pathname}`;
    const envelope = harnessFixtures.get(key);
    if (!envelope) {
      diagnostics.unknownApiRequests.push(key);
      await route.fulfill({
        status: 501,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "unknown_harness_endpoint", key } }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(envelope),
    });
  });
};

const setHarnessSession = async (
  page: Page,
  roles: readonly string[],
  user = roles.length ? "role.synthetic@example.invalid" : "Guest",
) => {
  const diagnostics = diagnosticsByPage.get(page);
  if (!diagnostics) {
    throw new Error("harness diagnostics 尚未初始化");
  }
  if (diagnostics.sessionConfigured) {
    throw new Error("同一页面不得切换合成角色");
  }
  diagnostics.sessionConfigured = true;
  await page.addInitScript(
    ({ sessionRoles, sessionUser }) => {
      const target = globalThis as typeof globalThis & {
        frappe?: {
          session: { user: string };
          boot: { user: { roles: string[] } };
          csrf_token: string;
        };
      };
      target.frappe = {
        session: { user: sessionUser },
        boot: { user: { roles: sessionRoles } },
        csrf_token: "synthetic-csrf-not-a-secret",
      };
    },
    { sessionRoles: [...roles], sessionUser: user },
  );
};

const ensureHarnessSession = async (page: Page) => {
  const diagnostics = diagnosticsByPage.get(page);
  if (diagnostics && !diagnostics.sessionConfigured) {
    await setHarnessSession(page, ["GBOS Admin"]);
  }
};

const navigateHarnessRoute = async (page: Page, path: string) => {
  await page.evaluate((targetPath) => {
    window.history.pushState({}, "", targetPath);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
};

const consumeExpectedUnknownRequest = (page: Page, key: string) => {
  const diagnostics = diagnosticsByPage.get(page);
  expect(diagnostics?.unknownApiRequests).toContain(key);
  if (diagnostics) {
    diagnostics.unknownApiRequests = diagnostics.unknownApiRequests.filter(
      (candidate) => candidate !== key,
    );
  }
};

const consumeExpectedConsoleError = (page: Page, expectedFragment: string) => {
  const diagnostics = diagnosticsByPage.get(page);
  const index =
    diagnostics?.consoleErrors.findIndex((candidate) =>
      candidate.includes(expectedFragment),
    ) ?? -1;
  expect(index, `缺少预期 console error: ${expectedFragment}`).toBeGreaterThanOrEqual(0);
  diagnostics?.consoleErrors.splice(index, 1);
};

const openWorkspace = async (
  page: Page,
  testInfo: TestInfo,
  path: string,
  heading: string,
  navigationLabel = heading,
) => {
  if (isHarness(testInfo)) {
    await ensureHarnessSession(page);
    await page.goto(harnessEntry);
    await page.getByRole("link", { name: navigationLabel, exact: true }).click();
  } else {
    await page.goto(path);
  }
  await expect(page).toHaveURL(new RegExp(`${path.replaceAll("/", "\\/")}$`, "u"));
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
};

const axeViolations = async (page: Page) => {
  return page.evaluate(async () => {
    const host = globalThis as typeof globalThis & {
      axe?: {
        run: (root: Document) => Promise<{
          violations: Array<{
            id: string;
            impact: string | null;
            nodes: Array<{ target: string[] }>;
          }>;
        }>;
      };
    };
    if (!host.axe) {
      throw new Error("axe 未加载");
    }
    return (await host.axe.run(document)).violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      nodes: violation.nodes.length,
      targets: violation.nodes.map((node) => node.target),
    }));
  });
};

const duplicateIds = async (page: Page) =>
  page.evaluate(() => {
    const counts = new Map<string, number>();
    for (const element of document.querySelectorAll<HTMLElement>("[id]")) {
      counts.set(element.id, (counts.get(element.id) ?? 0) + 1);
    }
    return [...counts.entries()]
      .filter(([, count]) => count > 1)
      .map(([id]) => id);
  });

const responsiveDiagnostics = async (page: Page) =>
  page.evaluate(() => ({
    viewport: window.innerWidth,
    html: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    offscreenButtons: [...document.querySelectorAll<HTMLButtonElement>("button")]
      .filter((button) => {
        const style = getComputedStyle(button);
        if (
          style.display === "none" ||
          style.visibility === "hidden" ||
          button.getClientRects().length === 0
        ) {
          return false;
        }
        const rectangle = button.getBoundingClientRect();
        return rectangle.left < -1 || rectangle.right > window.innerWidth + 1;
      })
      .map((button) => button.getAttribute("aria-label") ?? button.textContent?.trim()),
  }));

test.beforeEach(async ({ page }, testInfo) => {
  await page.addInitScript({ content: axe.source });
  if (isHarness(testInfo)) {
    await prepareHarness(page);
  } else {
    test.skip(
      !liveBaseUrl || !liveStorageState,
      "frappe-site 需要 GBOS_E2E_BASE_URL 与 synthetic 用户 storage state",
    );
  }
});

test.afterEach(async ({ page }, testInfo) => {
  if (!isHarness(testInfo)) {
    return;
  }
  const diagnostics = diagnosticsByPage.get(page);
  expect(diagnostics?.unknownApiRequests, "未识别的 harness API").toEqual([]);
  expect(diagnostics?.consoleErrors, "浏览器 console errors").toEqual([]);
  expect(diagnostics?.pageErrors, "浏览器 page errors").toEqual([]);
  expect(diagnostics?.requestFailures, "浏览器 request failures").toEqual([]);
});

test("未知 harness API 会被明确拒绝", async ({ page }, testInfo) => {
  test.skip(!isHarness(testInfo), "仅验证本地严格 harness");
  await page.goto(harnessEntry);

  const status = await page.evaluate(async () => {
    const response = await fetch("/api/method/esan_gbos.api.v9.unknown");
    return response.status;
  });

  expect(status).toBe(501);
  consumeExpectedUnknownRequest(page, "GET /api/method/esan_gbos.api.v9.unknown");
  consumeExpectedConsoleError(page, "501 (Not Implemented)");
});

for (const roleCase of roleCases) {
  test(`${roleCase.name} 只看到获授权的一级菜单`, async ({ page }, testInfo) => {
    test.skip(!isHarness(testInfo), "角色合成矩阵仅用于前端 harness");
    await setHarnessSession(page, roleCase.roles);
    await page.goto(harnessEntry);

    if (roleCase.roles.length === 0) {
      await expect(
        page.getByRole("heading", { name: "当前角色无权查看此页面" }),
      ).toBeVisible();
    } else {
      await expect(page.getByRole("heading", { level: 1, name: "产品总览" })).toBeVisible();
    }

    const sidebar = page.locator(".workspace-sidebar");
    for (const label of allNavigationLabels) {
      const link = sidebar.getByRole("link", { name: label, exact: true });
      if ((roleCase.navigation as readonly string[]).includes(label)) {
        await expect(link).toBeVisible();
      } else {
        await expect(link).toHaveCount(0);
      }
    }

    if ("deniedPath" in roleCase && roleCase.deniedPath) {
      await navigateHarnessRoute(page, roleCase.deniedPath);
      await expect(
        page.getByRole("heading", { name: "当前角色无权查看此页面" }),
      ).toBeVisible();
    }
  });
}

test("GBOS Admin 的全部真实路由可读且无严重无障碍问题", async ({
  page,
}, testInfo) => {
  test.skip(!isHarness(testInfo), "完整合成路由矩阵仅用于前端 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  await page.goto(harnessEntry);

  for (const route of allRoutes) {
    await navigateHarnessRoute(page, route.path);
    await expect(page).toHaveURL(new RegExp(`${route.path.replaceAll("/", "\\/")}$`, "u"));
    await expect(
      page.getByRole("heading", { level: 1, name: route.heading }),
    ).toBeVisible();
    expect(await axeViolations(page), `${route.path} axe violations`).toEqual([]);
    expect(await duplicateIds(page), `${route.path} duplicate IDs`).toEqual([]);
  }
});

test("五个角色工作台无 axe 违规", async ({ page }, testInfo) => {
  for (const { path, heading, navigationLabel } of workspaces) {
    await openWorkspace(page, testInfo, path, heading, navigationLabel);
    if (isHarness(testInfo) || path === "/gbos/ceo") {
      await expect(page.getByText(/演示/u).first()).toBeVisible();
    }
    expect(await axeViolations(page), `${path} axe violations`).toEqual([]);
  }
});

test("CEO cockpit 显示治理质量与来源且不可用指标没有正式数值", async ({
  page,
}, testInfo) => {
  await openWorkspace(page, testInfo, "/gbos/ceo", "经营总览");
  await expect(page.getByText("演示 / 合成数据", { exact: true })).toBeVisible();

  const available = page.locator("[data-metric-key='sales.order_value']");
  await expect(
    available.getByText(isHarness(testInfo) ? "125,000" : "20,600", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(available.getByText("CNY", { exact: true })).toBeVisible();
  await expect(available.getByText(/^新鲜 ·/u)).toBeVisible();
  await expect(available.getByText(/100%/u)).toBeVisible();
  await expect(available.getByText(/已通过/u)).toBeVisible();
  await available.getByText(/查看定义与来源链路/u).click();
  await expect(
    available.getByText(
      isHarness(testInfo)
        ? "synthetic_kingdee_projection"
        : "kingdee-gate5-synthetic",
    ),
  ).toBeVisible();

  const receivables = page.locator("[data-metric-key='receivables.balance']");
  if (isHarness(testInfo)) {
    await expect(
      receivables.getByText("不显示正式数值", { exact: true }),
    ).toBeVisible();
    await expect(receivables.locator("[data-official-value]")).toHaveCount(0);
    await expect(receivables.getByText(/reconciliation_failed/u)).toBeVisible();
  } else {
    await expect(receivables.getByText("6,000", { exact: true })).toBeVisible();
    await expect(receivables.getByText("CNY", { exact: true })).toBeVisible();
  }
});

test("SPA 内销售切换采购会重新读取采购数据", async ({ page }, testInfo) => {
  test.skip(!isHarness(testInfo), "合成哨兵仅用于前端 SPA 路由回归");
  await openWorkspace(page, testInfo, "/gbos/sales", "销售工作项", "销售协同");
  await expect(page.getByText(/SALES-ONLY/u).first()).toBeVisible();

  await page.getByRole("link", { name: "采购协同", exact: true }).click();
  await expect(page).toHaveURL(/\/gbos\/purchase$/u);
  await expect(
    page.getByRole("heading", { level: 1, name: "采购询源工作台" }),
  ).toBeVisible();
  await expect(page.getByText(/PURCHASE-ONLY/u)).toBeVisible();
  await expect(page.getByText(/SALES-ONLY/u)).toHaveCount(0);
});

test("全部真实路由在 320、375、768、1024、1440 与 200% 等效视口无溢出", async ({
  page,
}, testInfo) => {
  test.skip(!isHarness(testInfo), "完整响应式矩阵仅用于前端 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  await page.goto(harnessEntry);

  for (const width of [320, 375, 720, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of allRoutes) {
      await navigateHarnessRoute(page, route.path);
      await expect(
        page.getByRole("heading", { level: 1, name: route.heading }),
      ).toBeVisible();
      const diagnostics = await responsiveDiagnostics(page);
      expect(
        diagnostics.html,
        `${route.path} ${width}px html overflow`,
      ).toBeLessThanOrEqual(diagnostics.viewport);
      expect(
        diagnostics.body,
        `${route.path} ${width}px body overflow`,
      ).toBeLessThanOrEqual(diagnostics.viewport);
      expect(
        diagnostics.offscreenButtons,
        `${route.path} ${width}px offscreen commands`,
      ).toEqual([]);
    }
  }
});

test("键盘顺序从 skip link 到导航与操作", async ({ page }, testInfo) => {
  if (isHarness(testInfo)) {
    await setHarnessSession(page, ["GBOS Admin"]);
    await page.goto(harnessEntry);
    await navigateHarnessRoute(page, "/gbos/sales");
  } else {
    await page.goto("/gbos/sales");
  }
  await expect(
    page.getByRole("heading", { level: 1, name: "销售工作项" }),
  ).toBeVisible();
  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "跳到主要内容" });
  await expect(skipLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  await skipLink.focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "ESAN GBOS 产品首页" }),
  ).toBeFocused();
  for (const heading of navigationHeadings) {
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: heading, exact: true })).toBeFocused();
  }
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "刷新" })).toBeFocused();
});

test("移动端抽屉锁定焦点并在 Escape 后归还菜单按钮", async ({
  page,
}, testInfo) => {
  test.skip(!isHarness(testInfo), "移动抽屉矩阵仅用于前端 harness");
  await page.setViewportSize({ width: 375, height: 812 });
  await setHarnessSession(page, ["CEO"]);
  await page.goto(harnessEntry);

  const menuButton = page.getByRole("button", { name: "打开导航菜单" });
  await menuButton.click();
  const drawer = page.getByRole("dialog", { name: "工作区导航" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("link", { name: "产品首页" })).toBeFocused();

  await page.keyboard.press("Shift+Tab");
  await expect(drawer.getByRole("button", { name: "关闭导航菜单" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(drawer).toHaveCount(0);
  await expect(menuButton).toBeFocused();
});

test("接口错误显示中文可操作反馈且不残留旧数据", async ({ page }, testInfo) => {
  test.skip(!isHarness(testInfo), "错误注入仅用于前端 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  await page.route(`**${BFF_ENDPOINTS.workItemList}*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        message: { malformed: true },
      }),
    });
  });
  await page.goto(harnessEntry);
  await page.getByRole("link", { name: "销售协同", exact: true }).click();

  const alert = page.getByRole("alert");
  await expect(alert.getByRole("heading", { name: "暂时无法读取数据" })).toBeVisible();
  await expect(alert.getByRole("button", { name: "重新读取" })).toBeVisible();
  await expect(page.getByText(/SALES-ONLY/u)).toHaveCount(0);
});

test("集成与沟通切片通过 axe、Restricted 和三视口检查", async ({
  page,
}, testInfo) => {
  await openWorkspace(page, testInfo, "/gbos/integrations", "集成状态");
  if (isHarness(testInfo)) {
    await expect(page.getByText("deepseek-v4-flash")).toBeVisible();
    await expect(page.getByText("WhatsApp", { exact: true })).toBeVisible();
  } else {
    await expect(page.getByText("暂无符合条件的数据")).toBeVisible();
  }
  expect(await axeViolations(page)).toEqual([]);

  await page.getByRole("link", { name: "沟通观察", exact: true }).click();
  if (isHarness(testInfo)) {
    const summaryLink = page.getByRole("link", {
      name: "客户询问下一轮样品交期。",
      exact: true,
    });
    await expect(summaryLink).toBeVisible();
    await summaryLink.click();
    await expect(
      page.getByText(/Restricted：当前角色无权查看原文/u),
    ).toBeVisible();
    await expect(
      page.getByText("基于沟通的非正式观察/非正式指标"),
    ).toBeVisible();
  } else {
    await expect(page.getByText("暂无符合条件的数据")).toBeVisible();
  }
  await expect(page.locator("blockquote")).toHaveCount(0);
  expect(await axeViolations(page)).toEqual([]);

  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const dimensions = await page.evaluate(() => ({
      viewport: window.innerWidth,
      html: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
    }));
    expect(dimensions.html).toBeLessThanOrEqual(dimensions.viewport);
    expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
  }
});

test("销售身份候选只有 Party/Contact，不发出 User 请求", async ({ page }, testInfo) => {
  test.skip(!isHarness(testInfo), "身份角色裁剪使用前端严格 harness");
  await setHarnessSession(page, ["Sales User"]);
  const candidateTypes: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === BFF_V4_ENDPOINTS.identityListCandidates) {
      candidateTypes.push(new URL(request.url()).searchParams.get("candidate_type") ?? "");
    }
  });
  await page.goto(harnessEntry);
  await navigateHarnessRoute(page, "/gbos/communications/OBS-E2E-1");

  const candidateType = page.getByLabel("候选类型");
  await expect(candidateType.locator("option")).toHaveText(["客户主体", "联系人"]);
  await expect(candidateType.locator("option[value='User']")).toHaveCount(0);
  await expect(page.getByText("系统用户", { exact: true })).toHaveCount(0);
  await candidateType.selectOption("Contact");
  await expect.poll(() => candidateTypes.at(-1)).toBe("Contact");
  expect(candidateTypes).not.toContain("User");
  expect(await axeViolations(page)).toEqual([]);
});

test("管理员撤回经二次确认，stale 409 刷新后可成功重试", async ({
  page,
}, testInfo) => {
  test.skip(!isHarness(testInfo), "身份撤回使用前端严格 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  const identityRef = "extid:v1:email:p5N7ZLjKpY8Dchu2us9ceMsjX-vg5wsbhM2ZVBRhoI4";
  const mappingRef = "MAPPING-REVOKE-E2E-MUST-NOT-RENDER";
  let stateReads = 0;
  let revokeCalls = 0;
  const revokePosts: string[] = [];
  await page.route(`**${BFF_V4_ENDPOINTS.identityListStates}**`, async (route) => {
    stateReads += 1;
    const revoked = stateReads >= 3;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(v4Envelope({
        identities: [{
          identity_ref: identityRef,
          provider: "email",
          status: revoked ? "revoked" : "confirmed",
          mapping_ref: mappingRef,
          mapping_revision: stateReads === 1 ? 4 : stateReads === 2 ? 5 : 6,
          target_type: "Party",
          display_label: "海湾香氛客户",
        }],
        connector_account_owner: { display_label: "渠道账号负责人" },
      })),
    });
  });
  await page.route(`**${BFF_V4_ENDPOINTS.identityRevoke}`, async (route) => {
    revokeCalls += 1;
    revokePosts.push(route.request().postData() ?? "");
    if (revokeCalls === 1) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          message: {
            error: {
              code: "revision_conflict",
              message: "映射版本已更新，请重新确认。",
              request_id: "req-revoke-stale-e2e",
              details: {},
            },
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(v4Envelope({
        status: "revoked",
        mapping_ref: mappingRef,
        mapping_revision: 6,
      })),
    });
  });
  await page.goto(harnessEntry);
  await navigateHarnessRoute(page, "/gbos/communications/OBS-E2E-1");

  await expect(page.getByText(identityRef, { exact: true })).toHaveCount(0);
  await expect(page.getByText(mappingRef, { exact: true })).toHaveCount(0);
  const revoke = page.getByRole("button", { name: "撤回已确认映射" });
  await revoke.click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("alert")).toContainText("映射版本已更新");
  consumeExpectedConsoleError(page, "409 (Conflict)");
  expect(stateReads).toBe(2);
  expect(revokeCalls).toBe(1);

  await revoke.click();
  await page.getByRole("button", { name: "确认撤回" }).dblclick();
  await expect(page.getByText("已撤回身份映射。", { exact: true })).toBeVisible();
  expect(stateReads).toBe(3);
  expect(revokeCalls).toBe(2);
  for (const post of revokePosts) {
    const body = new URLSearchParams(post);
    expect(body.get("idempotency_key")).toMatch(/\S/u);
    expect(body.get("identity_ref")).toBe(identityRef);
    expect(body.get("mapping_ref")).toBe(mappingRef);
  }
  await expect(page.getByText(identityRef, { exact: true })).toHaveCount(0);
  await expect(page.getByText(mappingRef, { exact: true })).toHaveCount(0);
  expect(await axeViolations(page)).toEqual([]);
});

test("身份安全详情 permission deny 时不提供无目标决定", async ({
  page,
}, testInfo) => {
  test.skip(!isHarness(testInfo), "身份审核失败关闭使用前端严格 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  await page.route(`**${BFF_V2_ENDPOINTS.reviewGet}**`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticIdentityGenericReviewDetailEnvelope),
    });
  });
  await page.route(`**${BFF_V4_ENDPOINTS.identityGetPendingReview}**`, async (route) => {
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        message: {
          error: {
            code: "permission_denied",
            message: "当前审核人无权读取身份详情。",
            request_id: "req-identity-permission-e2e",
            details: {},
          },
        },
      }),
    });
  });
  await page.goto(harnessEntry);
  await navigateHarnessRoute(page, "/gbos/review/IDENTITY-REVIEW-E2E");

  await expect(page.getByRole("alert")).toContainText("身份审核详情不可用");
  await expect(page.getByRole("button", { name: "刷新安全详情" })).toBeVisible();
  await expect(page.getByLabel("审核说明")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /批准案件|拒绝案件/u })).toHaveCount(0);
  await expect(page.getByText("MAPPING-E2E", { exact: true })).toHaveCount(0);
  await expect(page.getByText("PROTECTED-PARTY-E2E", { exact: true })).toHaveCount(0);
  consumeExpectedConsoleError(page, "403 (Forbidden)");
  expect(await axeViolations(page)).toEqual([]);
});

test("身份解析只显示安全标签并通过服务端审核筛选", async ({ page }, testInfo) => {
  test.skip(!isHarness(testInfo), "身份解析交互使用前端严格 harness");
  await setHarnessSession(page, ["GBOS Admin"]);
  const identityPosts: string[] = [];
  const decisionPosts: string[] = [];
  expect(JSON.stringify(syntheticIdentityGenericReviewDetailEnvelope)).not.toMatch(
    /external_subject|subject_snapshot|identity\.user@example\.invalid|PARTY-RAW-TARGET|MODEL-RAW/u,
  );
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === BFF_V4_ENDPOINTS.identitySubmitForReview
    ) {
      identityPosts.push(request.postData() ?? "");
    }
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === BFF_V2_ENDPOINTS.reviewDecide
    ) {
      decisionPosts.push(request.postData() ?? "");
    }
  });
  await page.route(`**${BFF_V4_ENDPOINTS.identitySubmitForReview}`, async (route) => {
    await new Promise<void>((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticIdentityCommandEnvelope),
    });
  });
  await page.route(`**${BFF_V2_ENDPOINTS.reviewGet}**`, async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.searchParams.get("name") !== "IDENTITY-REVIEW-E2E") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticIdentityGenericReviewDetailEnvelope),
    });
  });
  await page.route(`**${BFF_V2_ENDPOINTS.reviewDecide}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(syntheticIdentityGenericDecisionEnvelope),
    });
  });
  await page.goto(harnessEntry);
  await navigateHarnessRoute(page, "/gbos/communications/OBS-E2E-1");

  await expect(page.getByText("渠道账号负责人", { exact: true })).toBeVisible();
  await expect(page.getByText("消息参与者 1", { exact: true })).toBeVisible();
  await expect(page.getByText(/Email · 未解析/u)).toBeVisible();
  await expect(page.getByRole("radio", { name: /海湾香氛客户/u })).toBeVisible();
  await expect(page.getByLabel("合格审核人")).toContainText("合格审核人");
  await expect(page.getByText(/opaque-e2e-participant/u)).toHaveCount(0);
  await expect(page.getByText("PROTECTED-PARTY-E2E", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /确认身份|直接确认/u })).toHaveCount(0);

  await page.getByRole("radio", { name: /海湾香氛客户/u }).check();
  await page.getByLabel("合格审核人").selectOption({ label: "合格审核人" });
  const submit = page.getByRole("button", { name: "提交审核" });
  await submit.dblclick();
  await expect(page.getByText(/已提交人工审核/u)).toBeVisible();
  expect(identityPosts).toHaveLength(1);
  expect(identityPosts[0]).toContain("expected_revision=0");
  expect(identityPosts[0]).toContain("idempotency_key=");

  await navigateHarnessRoute(page, "/gbos/review");
  await page.getByLabel("审核类型", { exact: true }).selectOption("identity");
  await expect(page.getByRole("heading", { name: "Identity Resolution" })).toBeVisible();
  await expect(page.getByText("EVID-IDENTITY-E2E", { exact: true })).toBeVisible();
  await expect(page.getByText("identity-resolution-v1", { exact: true })).toBeVisible();
  await expect(page.getByText("PROTECTED-PARTY-E2E", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "进入治理审核" }),
  ).toHaveAttribute("href", "/gbos/review/IDENTITY-REVIEW-E2E");
  await page.getByRole("button", { name: "查看固定详情" }).click();
  await expect(page.getByRole("heading", { name: "身份解析固定详情" })).toBeVisible();
  expect(await axeViolations(page)).toEqual([]);

  await page.getByRole("link", { name: "进入治理审核" }).click();
  await expect(page).toHaveURL(/\/gbos\/review\/IDENTITY-REVIEW-E2E$/u);
  await expect(page.getByRole("heading", { name: "身份解析案件" })).toBeVisible();
  await expect(page.getByText("海湾香氛客户", { exact: true })).toBeVisible();
  await expect(page.getByText("审核版本：3", { exact: true })).toBeVisible();
  await expect(page.getByText("映射版本：2", { exact: true })).toBeVisible();
  await expect(page.getByText("protected:identity-subject", { exact: true })).toHaveCount(0);
  await expect(page.getByText("MAPPING-E2E", { exact: true })).toHaveCount(0);
  await expect(page.getByText("PROTECTED-PARTY-E2E", { exact: true })).toHaveCount(0);
  await page.getByLabel("审核说明").fill("当前证据不足。");
  await page.getByRole("button", { name: "拒绝案件" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("审核决定已记录。", { exact: true })).toBeVisible();
  expect(decisionPosts).toHaveLength(1);
  const decisionBody = new URLSearchParams(decisionPosts[0]);
  expect(decisionBody.get("name")).toBe("IDENTITY-REVIEW-E2E");
  expect(decisionBody.get("decision")).toBe("Rejected");
  expect(decisionBody.get("expected_revision")).toBe("3");
  expect(decisionBody.get("expected_subject_revision")).toBe("2");
  expect(decisionBody.get("idempotency_key")).toMatch(/\S/u);

  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const diagnostics = await responsiveDiagnostics(page);
    expect(diagnostics.html).toBeLessThanOrEqual(diagnostics.viewport);
    expect(diagnostics.body).toBeLessThanOrEqual(diagnostics.viewport);
    expect(diagnostics.offscreenButtons).toEqual([]);
  }
  await page.setViewportSize({ width: 768, height: 900 });
  await page.evaluate(() => {
    document.documentElement.style.zoom = "200%";
  });
  const zoomedDiagnostics = await responsiveDiagnostics(page);
  expect(zoomedDiagnostics.html).toBeLessThanOrEqual(zoomedDiagnostics.viewport);
  expect(zoomedDiagnostics.body).toBeLessThanOrEqual(zoomedDiagnostics.viewport);
  expect(zoomedDiagnostics.offscreenButtons).toEqual([]);
  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
  });
});

test("离线关闭且 fixture API 响应不进入持久存储", async (
  { context, page },
  testInfo,
) => {
  test.skip(!isHarness(testInfo), "深链接离线缓存审计仅用于可控构建产物");
  const sensitiveText = "客户询问下一轮样品交期。";
  const sensitiveSentinels = [
    sensitiveText,
    "EVID-E2E-1",
    "REVIEW-ONLY · 确认客户反馈事实",
    "DRAFT-E2E-1",
  ];
  await setHarnessSession(page, ["GBOS Admin"]);

  const deepLink = "/gbos/communications/OBS-E2E-1";
  const serveBuiltShell = async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/html; charset=utf-8",
      headers: { "Service-Worker-Allowed": "/gbos/" },
      body: builtHarnessShell,
    });
  };
  await page.route(`**${deepLink}`, serveBuiltShell);
  await page.goto(deepLink);
  await expect(page.getByRole("heading", { name: "沟通观察详情" })).toBeVisible();
  await expect(page.getByText(sensitiveText, { exact: true })).toBeVisible();

  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise<void>((resolve) => {
        navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), {
          once: true,
        });
      });
    }
  });
  expect(await page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(
    true,
  );

  const storage = await page.evaluate(async () => {
    const cacheNames = await caches.keys();
    const cacheEntries = (
      await Promise.all(
        cacheNames.map(async (name) => {
          const cache = await caches.open(name);
          return Promise.all(
            (await cache.keys()).map(async (request) => ({
              url: request.url,
              body: await (await cache.match(request))?.clone().text(),
            })),
          );
        }),
      )
    ).flat();
    const databaseNames =
      "databases" in indexedDB
        ? (await indexedDB.databases()).map((database) => database.name ?? "")
        : [];
    const indexedDbValues = (
      await Promise.all(
        databaseNames.filter(Boolean).map(
          (name) =>
            new Promise<string[]>((resolve, reject) => {
              const request = indexedDB.open(name);
              request.onerror = () => reject(request.error);
              request.onsuccess = () => {
                const database = request.result;
                const stores = [...database.objectStoreNames];
                if (stores.length === 0) {
                  database.close();
                  resolve([]);
                  return;
                }
                const values: string[] = [];
                const transaction = database.transaction(stores, "readonly");
                transaction.onerror = () => reject(transaction.error);
                transaction.oncomplete = () => {
                  database.close();
                  resolve(values);
                };
                for (const storeName of stores) {
                  const getAll = transaction.objectStore(storeName).getAll();
                  getAll.onerror = () => reject(getAll.error);
                  getAll.onsuccess = () => {
                    values.push(JSON.stringify(getAll.result));
                  };
                }
              };
            }),
        ),
      )
    ).flat();
    return {
      localKeys: Object.keys(localStorage),
      localValues: Object.values(localStorage),
      sessionKeys: Object.keys(sessionStorage),
      sessionValues: Object.values(sessionStorage),
      cacheEntries,
      databaseNames,
      indexedDbValues,
    };
  });
  expect(storage.localKeys).toEqual([]);
  expect(storage.sessionKeys).toEqual([]);
  expect(storage.cacheEntries.some(({ url }) => url.includes("/api/"))).toBe(false);
  const persistedText = [
    ...storage.localValues,
    ...storage.sessionValues,
    ...storage.cacheEntries.map(({ body }) => body ?? ""),
    ...storage.indexedDbValues,
  ].join("\n");
  for (const sentinel of sensitiveSentinels) {
    expect(persistedText).not.toContain(sentinel);
  }
  expect(
    storage.databaseNames.some((name) => /gbos-(data|api|fixture)/iu.test(name)),
  ).toBe(false);

  await page.unroute(`**${deepLink}`, serveBuiltShell);
  await page.evaluate(async () => {
    const controller = navigator.serviceWorker.controller;
    if (!controller) {
      throw new Error("离线验证前 Service Worker 必须已控制页面");
    }
    await new Promise<void>((resolve) => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => resolve();
      controller.postMessage(
        { type: "GBOS_NETWORK_STATE", online: false },
        [channel.port2],
      );
    });
  });
  await context.setOffline(true);
  expect(
    diagnosticsByPage.get(page)?.consoleErrors,
    "切换离线状态时不应触发新的网络读取",
  ).toEqual([]);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText("需要联网", { exact: true })).toBeVisible();
  await expect(page.getByText(sensitiveText, { exact: true })).toHaveCount(0);
  await expect(page.getByText(/EVID-E2E-1/u)).toHaveCount(0);
});
