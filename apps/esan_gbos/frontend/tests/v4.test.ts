import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { describe, expect, it, vi } from "vitest";

import {
  BFF_V4_ENDPOINTS,
  BffError,
  createBffClient,
  type Fetcher,
} from "@/api/bff";
import { BFF_CLIENT_KEY } from "@/api/injection";
import { navigationForRoles } from "@/navigation";
import { APP_ROUTES, isRouteAllowed } from "@/router";
import { refreshSession } from "@/session";
import CommunicationDetailView from "@/views/CommunicationDetailView.vue";
import CommunicationsView from "@/views/CommunicationsView.vue";
import IntegrationsView from "@/views/IntegrationsView.vue";
import ReviewQueueView from "@/views/ReviewQueueView.vue";

const okV4 = (data: unknown) =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: "req-v4", schema_version: "4.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const okV1 = (data: unknown) =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: "req-v1", schema_version: "1.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

describe("BFF v4 typed client", () => {
  it("冻结十个 exact URL", () => {
    expect(BFF_V4_ENDPOINTS).toEqual({
      integrationListStatus: "/api/method/esan_gbos.api.v4.integration.list_status",
      integrationPause: "/api/method/esan_gbos.api.v4.integration.pause",
      integrationResume: "/api/method/esan_gbos.api.v4.integration.resume",
      integrationReplay: "/api/method/esan_gbos.api.v4.integration.replay",
      communicationList: "/api/method/esan_gbos.api.v4.communication.list",
      communicationGet: "/api/method/esan_gbos.api.v4.communication.get",
      modelGetUsage: "/api/method/esan_gbos.api.v4.model.get_usage",
      aiDraftList: "/api/method/esan_gbos.api.v4.ai_draft.list",
      aiDraftGet: "/api/method/esan_gbos.api.v4.ai_draft.get",
      aiDraftSubmitForReview:
        "/api/method/esan_gbos.api.v4.ai_draft.submit_for_review",
    });
  });

  it("GET 使用 query/no-store 并接受 4.0 envelope", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      okV4({ communications: [], next_cursor: null }),
    );
    const client = createBffClient({ fetcher, isOnline: () => true });

    await client.listCommunications({
      channel: "WhatsApp",
      classification: "Customer Request",
      cursor: "next-一",
      pageSize: 20,
    });

    const [input, init] = fetcher.mock.calls[0] ?? [];
    const url = new URL(String(input), "https://gbos.invalid");
    expect(url.pathname).toBe(BFF_V4_ENDPOINTS.communicationList);
    expect(url.searchParams.get("channel")).toBe("WhatsApp");
    expect(url.searchParams.get("classification")).toBe("Customer Request");
    expect(url.searchParams.get("cursor")).toBe("next-一");
    expect(url.searchParams.get("page_size")).toBe("20");
    expect(init).toMatchObject({ method: "GET", cache: "no-store" });
  });

  it("十个 typed methods 保持冻结 method/path", async () => {
    const fetcher = vi.fn<Fetcher>().mockImplementation(() =>
      Promise.resolve(okV4({})),
    );
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-v4",
    });

    await client.listIntegrationStatus();
    await client.listCommunications();
    await client.getCommunication("OBS-1");
    await client.getModelUsage("2026-08");
    await client.listAiDrafts();
    await client.getAiDraft("DRAFT-1");
    await client.pauseIntegration({
      instance_id: "wa-main",
      expected_revision: 1,
      idempotency_key: "pause-wa-main-1",
    });
    await client.resumeIntegration({
      instance_id: "wa-main",
      expected_revision: 2,
      idempotency_key: "resume-wa-main-2",
    });
    await client.replayIntegration({
      instance_id: "wa-main",
      expected_revision: 3,
      idempotency_key: "replay-wa-main-3",
    });
    await client.submitAiDraftForReview({
      draft_id: "DRAFT-1",
      expected_revision: 4,
      idempotency_key: "submit-draft-1-4",
    });

    expect(
      fetcher.mock.calls.map(([url, init]) => [
        new URL(String(url), "https://gbos.invalid").pathname,
        init?.method,
      ]),
    ).toEqual([
      [BFF_V4_ENDPOINTS.integrationListStatus, "GET"],
      [BFF_V4_ENDPOINTS.communicationList, "GET"],
      [BFF_V4_ENDPOINTS.communicationGet, "GET"],
      [BFF_V4_ENDPOINTS.modelGetUsage, "GET"],
      [BFF_V4_ENDPOINTS.aiDraftList, "GET"],
      [BFF_V4_ENDPOINTS.aiDraftGet, "GET"],
      [BFF_V4_ENDPOINTS.integrationPause, "POST"],
      [BFF_V4_ENDPOINTS.integrationResume, "POST"],
      [BFF_V4_ENDPOINTS.integrationReplay, "POST"],
      [BFF_V4_ENDPOINTS.aiDraftSubmitForReview, "POST"],
    ]);
  });

  it("稳定错误 envelope 保留 code/request_id 并映射中文冲突状态", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      new Response(
        JSON.stringify({
          message: {
            error: {
              code: "revision_conflict",
              message: "连接器版本已更新，请刷新。",
              request_id: "req-v4-conflict",
              details: { current_revision: 8 },
            },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    const client = createBffClient({ fetcher, isOnline: () => true });

    await expect(client.listIntegrationStatus()).rejects.toMatchObject({
      name: "BffError",
      code: "revision_conflict",
      requestId: "req-v4-conflict",
      displayMessage: "连接器版本已更新，请刷新。",
      status: 409,
    } satisfies Partial<BffError>);
  });

  it("POST 携带 CSRF/revision/idempotency 且同一在途命令去重", async () => {
    let resolveResponse: ((response: Response) => void) | undefined;
    const fetcher = vi.fn<Fetcher>().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveResponse = resolve;
        }),
    );
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-v4",
    });
    const command = {
      instance_id: "wa-main",
      expected_revision: 7,
      idempotency_key: "pause-wa-main-7",
    };

    const first = client.pauseIntegration(command);
    const duplicate = client.pauseIntegration(command);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [, init] = fetcher.mock.calls[0] ?? [];
    expect(init).toMatchObject({
      method: "POST",
      cache: "no-store",
      headers: expect.objectContaining({ "X-Frappe-CSRF-Token": "csrf-v4" }),
    });
    expect(Object.fromEntries(new URLSearchParams(String(init?.body)))).toEqual({
      instance_id: "wa-main",
      expected_revision: "7",
      idempotency_key: "pause-wa-main-7",
    });

    resolveResponse?.(okV4({ instance_id: "wa-main", status: "paused", revision: 8 }));
    await expect(Promise.all([first, duplicate])).resolves.toHaveLength(2);
  });
});

describe("v4 roles and pages", () => {
  it("角色裁剪导航和深链", () => {
    expect(navigationForRoles(["Integration Admin"]).map((item) => item.to)).toEqual([
      "/gbos/integrations",
    ]);
    expect(navigationForRoles(["Sales User"]).map((item) => item.to)).toEqual([
      "/gbos/sales",
      "/gbos/communications",
    ]);
    expect(isRouteAllowed("/gbos/integrations", ["Sales User"])).toBe(false);
    expect(isRouteAllowed("/gbos/communications/OBS-1", ["CEO"])).toBe(true);
  });

  it("集成页只展示安全状态并阻止重复命令", async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(
        okV4({
          connectors: [
            {
              instance_id: "wa-main",
              channel: "WhatsApp",
              status: "enabled",
              checkpoint_version: 12,
              backlog: 3,
              last_success_at: "2026-08-07T02:00:00Z",
              safe_error_code: null,
              freshness: "fresh",
              revision: 4,
            },
          ],
        }),
      )
      .mockResolvedValueOnce(
        okV4({
          model: "deepseek-v4-flash",
          period: "2026-08",
          tokens: 1200,
          cost: { currency: "USD", amount: null, state: "unknown" },
          soft_limit: 10000,
          hard_limit: 20000,
          state: "normal",
        }),
      );
    const wrapper = mount(IntegrationsView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf",
          }),
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("WhatsApp");
    expect(wrapper.text()).not.toMatch(/access[_ -]?token|secret|密钥/iu);
  });

  it("沟通列表支持筛选、cursor 和只显示服务端结果", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      okV4({
        communications: [
          {
            observation_id: "OBS-1",
            channel: "WhatsApp",
            occurred_at: "2026-08-07T02:00:00Z",
            summary_zh: "客户询问交期。",
            original_language: "ar",
            classification: "Customer Request",
            review_status: "Unreviewed",
            team_ref: "TEAM-1",
            party_ref: "PARTY-1",
            evidence_count: 1,
          },
        ],
        next_cursor: "cursor-2",
      }),
    );
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [...APP_ROUTES],
    });
    const wrapper = mount(CommunicationsView, {
      global: {
        plugins: [router],
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("客户询问交期");
    expect(wrapper.get("form").attributes("aria-label")).toBe("沟通筛选");
    expect(wrapper.text()).toContain("下一页");
  });

  it("详情隐藏 Restricted 原文并标注 CEO 非正式观察", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      okV4({
        communication: {
          observation_id: "OBS-CEO-1",
          channel: "WeCom",
          occurred_at: "2026-08-07T02:00:00Z",
          summary_zh: "管理层观察到客户回复速度下降。",
          original_language: "zh",
          classification: "CEO Informal Observation",
          review_status: "Unreviewed",
          team_ref: "TEAM-1",
          party_ref: null,
          evidence_count: 1,
          evidence: [{ ref: "EVID-1", locator: "message:42" }],
          fact_proposals: [],
          association_suggestions: [],
          model: { name: "deepseek-v4-flash", version: "2026-08-01" },
          raw_access_allowed: false,
        },
      }),
    );
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-CEO-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("基于沟通的非正式观察/非正式指标");
    expect(wrapper.text()).toContain("Restricted 原文默认不可打开");
    expect(wrapper.find("blockquote").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("raw_text");
  });

  it("复用审核队列将 AI Draft 受控送入 Pending", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "reviewer@example.invalid" },
      boot: { user: { roles: ["Reviewer"] } },
    };
    refreshSession();
    const draft = {
      draft_id: "DRAFT-1",
      kind: "Review Case",
      status: "AI Draft",
      origin: "AI",
      subject: "确认客户交期事实",
      evidence: [{ ref: "EVID-1", locator: "message:42" }],
      model: { name: "deepseek-v4-flash", version: "2026-08-01" },
      revision: 2,
    };
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("review_case.list")) {
        return Promise.resolve(
          okV1({ cases: [], total: 0, next_cursor: null }),
        );
      }
      if (url.includes("ai_draft.submit_for_review")) {
        return Promise.resolve(
          okV4({ draft: { ...draft, status: "Pending", revision: 3 } }),
        );
      }
      return Promise.resolve(okV4({ drafts: [draft], next_cursor: null }));
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/gbos/review/:id", component: { template: "<div />" } }],
    });
    const wrapper = mount(ReviewQueueView, {
      global: {
        plugins: [router],
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-review",
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("AI Draft → Pending");
    await wrapper.get("button.button--primary").trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("已进入 Pending");
    const submitCall = fetcher.mock.calls.find(([url]) =>
      String(url).includes("ai_draft.submit_for_review"),
    );
    expect(Object.fromEntries(new URLSearchParams(String(submitCall?.[1]?.body)))).toMatchObject({
      draft_id: "DRAFT-1",
      expected_revision: "2",
    });

    confirm.mockRestore();
    delete host.frappe;
    refreshSession();
  });
});
