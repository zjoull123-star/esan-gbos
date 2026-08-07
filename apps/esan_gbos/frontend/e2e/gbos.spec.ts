import axe from "axe-core";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

const liveBaseUrl = process.env.GBOS_E2E_BASE_URL;
const liveStorageState = process.env.GBOS_E2E_STORAGE_STATE;
const harnessEntry = "/assets/esan_gbos/frontend/";

const workspaces = [
  ["/gbos/ceo", "经营总览"],
  ["/gbos/sales", "销售协同"],
  ["/gbos/purchase", "采购协同"],
  ["/gbos/product", "产品与样品"],
  ["/gbos/review", "审核队列"],
] as const;

const navigationHeadings = [
  ...workspaces.map(([, heading]) => heading),
  "集成状态",
  "沟通观察",
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

const v4Envelope = (data: unknown) => ({
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
      { type: "Party", target_ref: "PARTY-E2E", confidence: 0.9 },
    ],
    model: { name: "deepseek-v4-flash", version: "2026-08-01" },
    raw_access_allowed: false,
  },
});

const syntheticAiDraftEnvelope = v4Envelope({
  drafts: [],
  next_cursor: null,
});

const isHarness = (testInfo: TestInfo) =>
  testInfo.project.name === "frontend-harness";

const prepareHarness = async (page: Page) => {
  await page.addInitScript(() => {
    const target = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
        csrf_token: string;
      };
    };
    target.frappe = {
      session: { user: "gbos.admin.synthetic@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
      csrf_token: "synthetic-csrf-not-a-secret",
    };
  });
  await page.route("**/api/method/**", async (route) => {
    const url = route.request().url();
    const envelope = url.includes("api.v3.metrics.dashboard")
      ? syntheticMetricEnvelope
      : url.includes("api.v4.integration.list_status")
        ? syntheticIntegrationEnvelope
        : url.includes("api.v4.model.get_usage")
          ? syntheticUsageEnvelope
          : url.includes("api.v4.communication.list")
            ? syntheticCommunicationListEnvelope
            : url.includes("api.v4.communication.get")
              ? syntheticCommunicationDetailEnvelope
              : url.includes("api.v4.ai_draft.list")
                ? syntheticAiDraftEnvelope
      : url.includes("review_case.list")
      ? syntheticReviewEnvelope
      : url.includes("sourcing.get_board")
        ? syntheticSourcingEnvelope
        : syntheticWorkEnvelope;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(envelope),
    });
  });
};

const openWorkspace = async (
  page: Page,
  testInfo: TestInfo,
  path: string,
  heading: string,
) => {
  if (isHarness(testInfo)) {
    await page.goto(harnessEntry);
    await page.getByRole("link", { name: heading, exact: true }).click();
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
          violations: Array<{ id: string; impact: string | null; nodes: unknown[] }>;
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
    }));
  });
};

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

test("五个角色工作台无 axe 违规", async ({ page }, testInfo) => {
  for (const [path, heading] of workspaces) {
    await openWorkspace(page, testInfo, path, heading);
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
  await available.getByText(/查看来源链路/u).click();
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
  await openWorkspace(page, testInfo, "/gbos/sales", "销售协同");
  await expect(page.getByText(/SALES-ONLY/u)).toBeVisible();

  await page.getByRole("link", { name: "采购协同", exact: true }).click();
  await expect(page).toHaveURL(/\/gbos\/purchase$/u);
  await expect(page.getByRole("heading", { level: 1, name: "采购协同" })).toBeVisible();
  await expect(page.getByText(/PURCHASE-ONLY/u)).toBeVisible();
  await expect(page.getByText(/SALES-ONLY/u)).toHaveCount(0);
});

test("375、768、1440 无横向溢出", async ({ page }, testInfo) => {
  await openWorkspace(page, testInfo, "/gbos/ceo", "经营总览");
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const dimensions = await page.evaluate(() => ({
      viewport: window.innerWidth,
      html: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
    }));
    expect(dimensions.html, `${width}px html overflow`).toBeLessThanOrEqual(
      dimensions.viewport,
    );
    expect(dimensions.body, `${width}px body overflow`).toBeLessThanOrEqual(
      dimensions.viewport,
    );
  }
});

test("键盘顺序从 skip link 到导航与操作", async ({ page }, testInfo) => {
  await openWorkspace(page, testInfo, "/gbos/sales", "销售协同");
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "跳到主要内容" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "ESAN GBOS 首页" })).toBeFocused();
  for (const heading of navigationHeadings) {
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: heading, exact: true })).toBeFocused();
  }
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "刷新" })).toBeFocused();
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
    await expect(page.getByText("客户询问下一轮样品交期。")).toBeVisible();
    await page.getByRole("link", { name: "查看安全详情" }).click();
    await expect(page.getByText("Restricted 原文默认不可打开")).toBeVisible();
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

test("离线关闭且 fixture API 响应不进入持久存储", async (
  { context, page },
  testInfo,
) => {
  const sensitiveText = isHarness(testInfo) ? "客户偏好清新的柑橘香调。" : "20,600";
  await openWorkspace(
    page,
    testInfo,
    isHarness(testInfo) ? "/gbos/sales" : "/gbos/ceo",
    isHarness(testInfo) ? "销售协同" : "经营总览",
  );
  await expect(page.getByText(sensitiveText, { exact: true })).toBeVisible();

  const storage = await page.evaluate(async () => {
    const cacheNames = await caches.keys();
    const cacheUrls = (
      await Promise.all(
        cacheNames.map(async (name) => {
          const cache = await caches.open(name);
          return (await cache.keys()).map((request) => request.url);
        }),
      )
    ).flat();
    const databaseNames =
      "databases" in indexedDB
        ? (await indexedDB.databases()).map((database) => database.name ?? "")
        : [];
    return {
      localKeys: Object.keys(localStorage),
      localValues: Object.values(localStorage),
      sessionKeys: Object.keys(sessionStorage),
      sessionValues: Object.values(sessionStorage),
      cacheUrls,
      databaseNames,
    };
  });
  if (isHarness(testInfo)) {
    expect(storage.localKeys).toEqual([]);
    expect(storage.sessionKeys).toEqual([]);
  } else {
    expect(storage.localKeys.some((key) => /^gbos[-_.:]/iu.test(key))).toBe(false);
    expect(storage.sessionKeys.some((key) => /^gbos[-_.:]/iu.test(key))).toBe(false);
    expect([...storage.localValues, ...storage.sessionValues].join("\n")).not.toContain(
      sensitiveText,
    );
  }
  expect(storage.cacheUrls.some((url) => url.includes("/api/"))).toBe(false);
  expect(
    storage.databaseNames.some((name) => /gbos-(data|api|fixture)/iu.test(name)),
  ).toBe(false);

  await context.setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(page.getByText("需要联网", { exact: true })).toBeVisible();
  await expect(page.getByText(sensitiveText, { exact: true })).toHaveCount(0);
});
