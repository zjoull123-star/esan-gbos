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
    const envelope = url.includes("review_case.list")
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
    await expect(page.getByText("演示数据")).toBeVisible();
    expect(await axeViolations(page), `${path} axe violations`).toEqual([]);
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
  await openWorkspace(page, testInfo, "/gbos/sales", "销售协同");
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
  for (const [, heading] of workspaces) {
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: heading, exact: true })).toBeFocused();
  }
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "刷新" })).toBeFocused();
});

test("离线关闭且 fixture API 响应不进入持久存储", async (
  { context, page },
  testInfo,
) => {
  await openWorkspace(page, testInfo, "/gbos/sales", "销售协同");
  await expect(page.getByText("客户偏好清新的柑橘香调。")).toBeVisible();

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
      sessionKeys: Object.keys(sessionStorage),
      cacheUrls,
      databaseNames,
    };
  });
  expect(storage.localKeys).toEqual([]);
  expect(storage.sessionKeys).toEqual([]);
  expect(storage.cacheUrls.some((url) => url.includes("/api/"))).toBe(false);
  expect(
    storage.databaseNames.some((name) => /gbos-(data|api|fixture)/iu.test(name)),
  ).toBe(false);

  await context.setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(page.getByText("需要联网", { exact: true })).toBeVisible();
  await expect(page.getByText("客户偏好清新的柑橘香调。")).toHaveCount(0);
});
