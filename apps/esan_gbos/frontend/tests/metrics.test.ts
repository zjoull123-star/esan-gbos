import { flushPromises, mount } from "@vue/test-utils";
import type { Component } from "vue";
import { describe, expect, it, vi } from "vitest";

import {
  BFF_V3_ENDPOINTS,
  BffError,
  createBffClient,
  type Fetcher,
} from "@/api/bff";
import type { MetricDashboardPayload } from "@/api/types";
import { BFF_CLIENT_KEY } from "@/api/injection";
import { APP_ROUTES } from "@/router";

const loadCeoDashboardView = APP_ROUTES.find(
  (route) => route.name === "ceo",
)?.component as () => Promise<{ default: Component & { __name?: string } }>;

const getCeoDashboardView = async () => (await loadCeoDashboardView()).default;

const lineage = {
  source_system: "synthetic_kingdee_projection",
  source_record_refs: ["sales-order-projection-SYNTH-001"],
  retrieved_at: "2026-08-06T02:30:00Z",
  transformation_version: "metrics-projection-v1",
  evidence_status: "synthetic" as const,
};

const availableMetric = {
  schema_version: "3.0" as const,
  metric_key: "sales.order_value",
  display_name: "销售订单金额",
  definition_version: "0.1.0",
  site_id: "gbos.localhost",
  status: "available" as const,
  value: 125000,
  unit: "CNY",
  as_of: "2026-08-06T02:30:00Z",
  queried_at: "2026-08-06T02:31:00Z",
  window: {
    type: "calendar" as const,
    grain: "month" as const,
    start: "2026-08-01T00:00:00Z",
    end: "2026-09-01T00:00:00Z",
  },
  freshness: { status: "fresh" as const, age_seconds: 60, slo_seconds: 86400 },
  coverage: {
    status: "sufficient" as const,
    ratio: 1,
    included_count: 4,
    total_count: 4,
  },
  reconciliation: {
    status: "passed" as const,
    checked_at: "2026-08-06T02:30:30Z",
    reference: "reconciliation-SYNTH-001",
    variance: 0,
  },
  source_lineage: [lineage],
  source_mode: "synthetic" as const,
  synthetic: true,
  governed_sources: true,
};

const withoutOfficialValue = (metric: typeof availableMetric) => {
  const copy: Partial<typeof availableMetric> = { ...metric };
  delete copy.value;
  delete copy.unit;
  return copy as Omit<typeof availableMetric, "value" | "unit">;
};

const unavailableMetric = {
  ...withoutOfficialValue(availableMetric),
  metric_key: "receivables.balance",
  display_name: "应收余额",
  status: "unavailable" as const,
  unavailable_reason: "reconciliation_failed" as const,
  reconciliation: {
    status: "failed" as const,
    checked_at: "2026-08-06T02:30:30Z",
    reference: "reconciliation-SYNTH-002",
    variance: 10,
  },
};

const dashboard: MetricDashboardPayload = {
  schema_version: "3.0",
  site_id: "gbos.localhost",
  source_mode: "synthetic",
  synthetic: true,
  generated_at: "2026-08-06T02:31:00Z",
  metrics: [availableMetric, unavailableMetric],
};

const response = (data: unknown) =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: "req-metrics", schema_version: "1.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

describe("Gate 5 governed metrics client", () => {
  it("freezes the CEO cockpit to the v3 dashboard GET and disables response caching", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(response(dashboard));
    const client = createBffClient({ fetcher, isOnline: () => true });

    const result = await client.getMetricDashboard();

    expect(result.data).toEqual(dashboard);
    expect(fetcher).toHaveBeenCalledWith(
      BFF_V3_ENDPOINTS.metricsDashboard,
      expect.objectContaining({
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: expect.objectContaining({
          Accept: "application/json",
          "Cache-Control": "no-store",
          Pragma: "no-cache",
        }),
      }),
    );
  });

  it.each([
    [
      "available without a value",
      { ...availableMetric, value: undefined },
    ],
    [
      "available with stale freshness",
      {
        ...availableMetric,
        freshness: { ...availableMetric.freshness, status: "stale" },
      },
    ],
    [
      "available with insufficient coverage",
      {
        ...availableMetric,
        coverage: { ...availableMetric.coverage, status: "insufficient" },
      },
    ],
    [
      "available with failed reconciliation",
      {
        ...availableMetric,
        reconciliation: { ...availableMetric.reconciliation, status: "failed" },
      },
    ],
    [
      "unavailable that leaks an official value",
      { ...unavailableMetric, value: 999, unit: "CNY" },
    ],
    [
      "dashboard source mode disagrees with a metric",
      { ...availableMetric, source_mode: "live", synthetic: false },
    ],
    [
      "available metric has ungoverned sources",
      { ...availableMetric, governed_sources: false },
    ],
    [
      "metric site disagrees with dashboard",
      { ...availableMetric, site_id: "other.invalid" },
    ],
  ])("fails closed for %s", async (_name, metric) => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValue(response({ ...dashboard, metrics: [metric] }));
    const client = createBffClient({ fetcher, isOnline: () => true });

    const promise = client.getMetricDashboard();
    await expect(promise).rejects.toBeInstanceOf(BffError);
    await expect(promise).rejects.toMatchObject({
      code: "invalid_response",
      requestId: "req-metrics",
    });
  });

  it.each([
    ["missing generated timestamp", { ...dashboard, generated_at: undefined }],
    ["wrong dashboard schema", { ...dashboard, schema_version: "2.0" }],
    [
      "duplicate metric keys",
      { ...dashboard, metrics: [availableMetric, { ...availableMetric }] },
    ],
    [
      "window without an end",
      {
        ...dashboard,
        metrics: [
          {
            ...availableMetric,
            window: { ...availableMetric.window, end: undefined },
          },
        ],
      },
    ],
  ])("rejects a dashboard with %s", async (_name, invalidDashboard) => {
    const client = createBffClient({
      fetcher: vi.fn<Fetcher>().mockResolvedValue(response(invalidDashboard)),
      isOnline: () => true,
    });

    await expect(client.getMetricDashboard()).rejects.toMatchObject({
      code: "invalid_response",
      requestId: "req-metrics",
    });
  });
});

describe("CEO governed metrics cockpit", () => {
  it("routes the CEO command center to its dedicated dashboard view", async () => {
    const component = await getCeoDashboardView();

    expect(component.__name).toBe("CeoDashboardView");
  });

  it("distinguishes loading, offline, permission, and empty states", async () => {
    const CeoDashboardView = await getCeoDashboardView();
    let resolveLoading: ((value: Response) => void) | undefined;
    const pendingResponse = new Promise<Response>((resolve) => {
      resolveLoading = resolve;
    });
    const loadingWrapper = mount(CeoDashboardView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher: vi.fn<Fetcher>().mockReturnValue(pendingResponse),
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();
    expect(loadingWrapper.text()).toContain("正在读取最新数据");
    resolveLoading?.(response({ ...dashboard, metrics: [] }));
    await flushPromises();
    loadingWrapper.unmount();

    const offlineWrapper = mount(CeoDashboardView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher: vi.fn<Fetcher>(),
            isOnline: () => false,
          }),
        },
      },
    });
    await flushPromises();
    expect(offlineWrapper.text()).toContain("需要联网");
    offlineWrapper.unmount();

    const permissionResponse = new Response(
      JSON.stringify({
        error: {
          code: "permission_denied",
          message: "无权查看受治理指标",
          request_id: "req-metrics-denied",
          details: {},
        },
      }),
      { status: 403, headers: { "Content-Type": "application/json" } },
    );
    const permissionWrapper = mount(CeoDashboardView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher: vi.fn<Fetcher>().mockResolvedValue(permissionResponse),
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();
    expect(permissionWrapper.text()).toContain("无权查看受治理指标");
    permissionWrapper.unmount();

    const emptyWrapper = mount(CeoDashboardView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher: vi
              .fn<Fetcher>()
              .mockResolvedValue(response({ ...dashboard, metrics: [] })),
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();
    expect(emptyWrapper.text()).toContain("暂无符合条件的数据");
  });

  it("shows synthetic provenance and every governed quality field without an unavailable value", async () => {
    const CeoDashboardView = await getCeoDashboardView();
    const client = createBffClient({
      fetcher: vi.fn<Fetcher>().mockResolvedValue(response(dashboard)),
      isOnline: () => true,
    });
    const wrapper = mount(CeoDashboardView, {
      global: { provide: { [BFF_CLIENT_KEY as symbol]: client } },
    });

    await flushPromises();

    expect(wrapper.get("h1").text()).toBe("经营总览");
    expect(wrapper.findAll(".metrics-source-banner")).toHaveLength(1);
    expect(wrapper.get("[role='status'].metrics-source-banner").text()).toContain(
      "演示 / 合成数据",
    );
    expect(wrapper.findAll(".metric-tile")).toHaveLength(2);
    expect(wrapper.text()).toContain("销售订单金额");
    expect(wrapper.text()).toContain("125,000");
    expect(wrapper.text()).toContain("CNY");
    expect(wrapper.text()).toContain("月");
    expect(wrapper.text()).toContain("2026");
    expect(wrapper.text()).toContain("新鲜");
    expect(wrapper.text()).toContain("100%");
    expect(wrapper.text()).toContain("已通过");
    expect(wrapper.text()).toContain("synthetic_kingdee_projection");
    expect(wrapper.text()).toContain("reconciliation_failed");

    const available = wrapper.get("[data-metric-key='sales.order_value']");
    expect(available.get("[data-official-value]").text()).toContain("125,000");
    expect(available.get(".metric-tile__quality").text()).toContain("新鲜");
    expect(available.get(".metric-tile__quality").text()).toContain("100%");
    expect(available.get(".metric-tile__quality").text()).toContain("已通过");
    expect(available.get("details").text()).toContain("定义版本");
    expect(available.get("details").text()).toContain("0.1.0");
    expect(available.get("details").text()).toContain("gbos.localhost");
    expect(available.get("details").text()).toContain("演示 / 合成");
    expect(available.get("details").text()).toContain("synthetic_kingdee_projection");

    const unavailable = wrapper.get(
      "[data-metric-key='receivables.balance']",
    );
    expect(unavailable.text()).toContain("不可用");
    expect(unavailable.text()).not.toContain("125,000");
    expect(unavailable.find("[data-official-value]").exists()).toBe(false);
  });

  it("invalid metric responses render an error state and no metric card", async () => {
    const CeoDashboardView = await getCeoDashboardView();
    const client = createBffClient({
      fetcher: vi.fn<Fetcher>().mockResolvedValue(
        response({
          ...dashboard,
          metrics: [
            {
              ...availableMetric,
              freshness: { ...availableMetric.freshness, status: "stale" },
            },
          ],
        }),
      ),
      isOnline: () => true,
    });
    const wrapper = mount(CeoDashboardView, {
      global: { provide: { [BFF_CLIENT_KEY as symbol]: client } },
    });

    await flushPromises();

    expect(wrapper.text()).toContain("暂时无法读取数据");
    expect(wrapper.find("[data-metric-key]").exists()).toBe(false);
  });
});
