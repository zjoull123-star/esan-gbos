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
import EvidencePanel from "@/components/data/EvidencePanel.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
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

const gbosButtonStub = {
  name: "GbosButton",
  inheritAttrs: false,
  props: {
    type: String,
    intent: String,
    loading: Boolean,
    disabled: Boolean,
  },
  emits: ["click"],
  template: `
    <button
      v-bind="$attrs"
      :type="type"
      :disabled="disabled || loading"
      :data-intent="intent"
      @click="$emit('click', $event)"
    ><slot /></button>
  `,
};

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
          token_state: "known",
          cost: { currency: "USD", amount: 3.25, state: "known" },
          soft_limit_usd: 50,
          hard_limit_usd: 100,
          state: "normal",
        }),
      );
    const wrapper = mount(IntegrationsView, {
      global: {
        stubs: { GbosButton: gbosButtonStub },
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
    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(OperationalListTemplate).exists()).toBe(true);
    expect(wrapper.findAllComponents(ResourceBoundary)).toHaveLength(2);
    expect(wrapper.text()).toContain("WhatsApp");
    expect(wrapper.text()).toContain("50.00 USD");
    expect(wrapper.text()).toContain("100.00 USD");
    expect(wrapper.text()).not.toMatch(/access[_ -]?token|secret|密钥/iu);
  });

  it("模型用量失败不会隐藏连接器状态", async () => {
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("integration.list_status")) {
        return Promise.resolve(
          okV4({
            connectors: [
              {
                instance_id: "wecom-main",
                channel: "WeCom",
                status: "enabled",
                checkpoint_version: 3,
                backlog: 0,
                last_success_at: null,
                safe_error_code: null,
                freshness: "fresh",
                revision: 2,
              },
            ],
          }),
        );
      }
      return Promise.reject(new TypeError("Failed to fetch"));
    });
    const wrapper = mount(IntegrationsView, {
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

    expect(wrapper.get("[data-integration-resource='connectors']").text()).toContain(
      "WeCom",
    );
    expect(
      wrapper.find("[data-integration-resource='usage'] .state-panel--offline").exists(),
    ).toBe(true);
  });

  it("连接器状态失败不会用模型用量伪装集成可用", async () => {
    const permission = new Response(
      JSON.stringify({
        message: {
          error: {
            code: "permission_denied",
            message: "无权读取连接器状态。",
            request_id: "req-status-denied",
            details: {},
          },
        },
      }),
      { status: 403, headers: { "Content-Type": "application/json" } },
    );
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      if (String(input).includes("integration.list_status")) {
        return Promise.resolve(permission.clone());
      }
      return Promise.resolve(
        okV4({
          model: "deepseek-v4-flash",
          period: "2026-08",
          tokens: 20,
          token_state: "known",
          cost: { currency: "USD", amount: 0.1, state: "known" },
          soft_limit_usd: 50,
          hard_limit_usd: 100,
          state: "normal",
        }),
      );
    });
    const wrapper = mount(IntegrationsView, {
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

    expect(
      wrapper.find("[data-integration-resource='connectors'] .state-panel--permission").exists(),
    ).toBe(true);
    expect(wrapper.get("[data-integration-resource='usage']").text()).toContain(
      "deepseek-v4-flash",
    );
    expect(wrapper.find("[data-integration-resource='connectors'] [data-connector]").exists()).toBe(false);
  });

  it("重放通过 ConfirmDialog 确认、阻止重复点击并在冲突后只刷新状态资源", async () => {
    const connector = {
      instance_id: "wa-main",
      channel: "WhatsApp",
      status: "enabled" as const,
      checkpoint_version: 12,
      backlog: 3,
      last_success_at: "2026-08-07T02:00:00Z",
      safe_error_code: null,
      freshness: "fresh" as const,
      revision: 4,
    };
    let resolveReplay: ((response: Response) => void) | undefined;
    let statusReads = 0;
    let usageReads = 0;
    let replayCalls = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("integration.list_status")) {
        statusReads += 1;
        return Promise.resolve(
          okV4({ connectors: [{ ...connector, revision: statusReads + 3 }] }),
        );
      }
      if (url.includes("model.get_usage")) {
        usageReads += 1;
        return Promise.resolve(
          okV4({
            model: "deepseek-v4-flash",
            period: "2026-08",
            tokens: 1200,
            token_state: "known",
            cost: { currency: "USD", amount: 3.25, state: "known" },
            soft_limit_usd: 50,
            hard_limit_usd: 100,
            state: "normal",
          }),
        );
      }
      replayCalls += 1;
      return new Promise<Response>((resolve) => {
        resolveReplay = resolve;
      });
    });
    const confirmSpy = vi.spyOn(window, "confirm");
    const wrapper = mount(IntegrationsView, {
      global: {
        stubs: { GbosButton: gbosButtonStub },
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

    await wrapper.get("button[data-command='replay']").trigger("click");
    expect(wrapper.findComponent(ConfirmDialog).props()).toMatchObject({
      modelValue: true,
      confirmLabel: "确认重放",
    });
    expect(wrapper.findComponent(ConfirmDialog).text()).toContain("可能重复处理历史消息");
    await wrapper.get("dialog button[data-action='confirm']").trigger("click");
    await wrapper.get("button[data-command='replay']").trigger("click");
    expect(replayCalls).toBe(1);
    expect(confirmSpy).not.toHaveBeenCalled();

    resolveReplay?.(
      new Response(
        JSON.stringify({
          message: {
            error: {
              code: "revision_conflict",
              message: "连接器版本已更新，请刷新。",
              request_id: "req-replay-conflict",
              details: {},
            },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await flushPromises();

    expect(statusReads).toBe(2);
    expect(usageReads).toBe(1);
    expect(replayCalls).toBe(1);
    expect(wrapper.get("[role='alert']").text()).toContain("连接器版本已更新");
    confirmSpy.mockRestore();
  });

  it("沟通列表以紧凑模板保留筛选、cursor 和详情深链", async () => {
    const fetcher = vi.fn<Fetcher>().mockImplementation(() =>
      Promise.resolve(okV4({
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
      })),
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

    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(OperationalListTemplate).exists()).toBe(true);
    expect(wrapper.findComponent(ResourceBoundary).exists()).toBe(true);
    expect(wrapper.findAllComponents(GbosButton)).toHaveLength(3);
    expect(wrapper.text()).toContain("客户询问交期");
    expect(wrapper.get("form").attributes("aria-label")).toBe("沟通筛选");
    expect(wrapper.findAll("thead th").map((cell) => cell.text())).toEqual([
      "渠道",
      "时间",
      "状态",
      "团队",
      "摘要",
    ]);
    expect(wrapper.get("a[href='/gbos/communications/OBS-1']").text()).toContain(
      "客户询问交期",
    );
    const mobileFields = wrapper.findAll("[data-mobile-list] [data-label]");
    expect(mobileFields.map((field) => field.attributes("data-label"))).toEqual([
      "渠道",
      "分类",
      "时间",
      "状态",
      "团队",
      "摘要",
      "证据数",
      "原始语言",
      "详情",
    ]);
    expect(mobileFields.map((field) => field.get("dd").text())).toEqual([
      "WhatsApp",
      "Customer Request",
      "2026-08-07T02:00:00Z",
      "Unreviewed",
      "TEAM-1",
      "客户询问交期。",
      "1",
      "ar",
      "查看详情",
    ]);
    expect(
      wrapper.get("[data-mobile-list] [data-label='详情'] a").attributes("href"),
    ).toBe("/gbos/communications/OBS-1");
    expect(wrapper.text()).toContain("下一页");

    await wrapper.get("select[name='channel']").setValue("WhatsApp");
    await wrapper.get("input[name='classification']").setValue("Customer Request");
    await wrapper.get("select[name='review_status']").setValue("Unreviewed");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    const filteredUrl = new URL(
      String(fetcher.mock.calls.at(-1)?.[0]),
      "https://gbos.invalid",
    );
    expect(Object.fromEntries(filteredUrl.searchParams)).toMatchObject({
      channel: "WhatsApp",
      classification: "Customer Request",
      review_status: "Unreviewed",
    });

    await wrapper.get("button[data-pagination='next']").trigger("click");
    await flushPromises();
    const nextUrl = new URL(
      String(fetcher.mock.calls.at(-1)?.[0]),
      "https://gbos.invalid",
    );
    expect(nextUrl.searchParams.get("cursor")).toBe("cursor-2");
  });

  it("沟通列表筛选请求离线后重试仍保留当前筛选", async () => {
    let requestCount = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation(() => {
      requestCount += 1;
      if (requestCount === 2) {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      return Promise.resolve(
        okV4({ communications: [], next_cursor: null }),
      );
    });
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

    await wrapper.get("select[name='channel']").setValue("WhatsApp");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.find(".state-panel--offline").exists()).toBe(true);

    await wrapper.get(".state-panel button").trigger("click");
    await flushPromises();
    const retryUrl = new URL(
      String(fetcher.mock.calls.at(-1)?.[0]),
      "https://gbos.invalid",
    );
    expect(retryUrl.searchParams.get("channel")).toBe("WhatsApp");
  });

  it("详情先显示中文摘要和证据，Restricted 时绝不泄露原文", async () => {
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
          fact_proposals: [
            {
              status: "Proposal",
              confidence: 0.72,
              type: "Response Trend",
              value_display: "回复速度下降",
            },
          ],
          association_suggestions: [
            { type: "Possible Party", target_ref: "PARTY-1", confidence: 0.61 },
          ],
          model: { name: "deepseek-v4-flash", version: "2026-08-01" },
          raw_access_allowed: false,
          original_text: "RESTRICTED-SOURCE-MUST-NOT-LEAK",
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

    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(DetailCommandTemplate).exists()).toBe(true);
    expect(wrapper.findComponent(EvidencePanel).exists()).toBe(true);
    expect(wrapper.findComponent(ResourceBoundary).exists()).toBe(true);
    expect(wrapper.text()).toContain("基于沟通的非正式观察/非正式指标");
    expect(wrapper.text()).toContain("Restricted：当前角色无权查看原文");
    expect(wrapper.text().indexOf("管理层观察到客户回复速度下降")).toBeLessThan(
      wrapper.text().indexOf("证据定位"),
    );
    expect(wrapper.text()).toContain("事实提案（Proposal）");
    expect(wrapper.text()).toContain("关联建议（Proposal）");
    expect(wrapper.text()).toContain(
      "所有事实与关联均为 Proposal，不构成批准、外发或正式业务修改。",
    );
    expect(wrapper.find("blockquote").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("RESTRICTED-SOURCE-MUST-NOT-LEAK");
    expect(wrapper.findAll("button")).toHaveLength(0);
  });

  it("获授权详情保留阿拉伯语原文的 RTL 方向且不提供业务动作", async () => {
    const original = "نحتاج عينة برائحة الحمضيات";
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      okV4({
        communication: {
          observation_id: "OBS-AR-1",
          channel: "WhatsApp",
          occurred_at: "2026-08-07T02:00:00Z",
          summary_zh: "客户需要柑橘香调样品。",
          original_language: "ar",
          classification: "Customer Request",
          review_status: "Unreviewed",
          team_ref: "TEAM-1",
          party_ref: "PARTY-1",
          evidence_count: 1,
          evidence: [{ ref: "EVID-AR-1", locator: "message:88" }],
          fact_proposals: [],
          association_suggestions: [],
          model: { name: "deepseek-v4-flash", version: "2026-08-01" },
          raw_access_allowed: true,
          original_text: original,
        },
      }),
    );
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-AR-1" },
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

    expect(wrapper.text().indexOf("客户需要柑橘香调样品")).toBeLessThan(
      wrapper.text().indexOf(original),
    );
    expect(wrapper.get("blockquote").attributes()).toMatchObject({
      lang: "ar",
      dir: "rtl",
    });
    expect(wrapper.get("blockquote").text()).toBe(original);
    expect(wrapper.findAll("button")).toHaveLength(0);
  });

  it("复用审核队列经 ConfirmDialog 将 AI Draft 受控送入 Pending并阻止重复提交", async () => {
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
    let resolveSubmit: ((response: Response) => void) | undefined;
    let submitCalls = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("review_case.list")) {
        return Promise.resolve(
          okV1({ cases: [], total: 0, next_cursor: null }),
        );
      }
      if (url.includes("ai_draft.submit_for_review")) {
        submitCalls += 1;
        return new Promise<Response>((resolve) => {
          resolveSubmit = resolve;
        });
      }
      return Promise.resolve(okV4({ drafts: [draft], next_cursor: null }));
    });
    const confirm = vi.spyOn(window, "confirm");
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/gbos/review/:id", component: { template: "<div />" } }],
    });
    const wrapper = mount(ReviewQueueView, {
      global: {
        plugins: [router],
        stubs: { GbosButton: gbosButtonStub },
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
    await wrapper.get("button[data-draft-submit='DRAFT-1']").trigger("click");
    expect(wrapper.findComponent(ConfirmDialog).props("modelValue")).toBe(true);
    await wrapper.get("dialog button[data-action='confirm']").trigger("click");
    await wrapper.get("button[data-draft-submit='DRAFT-1']").trigger("click");
    expect(submitCalls).toBe(1);

    resolveSubmit?.(
      okV4({ draft: { ...draft, status: "Pending", revision: 3 } }),
    );
    await flushPromises();

    expect(wrapper.text()).toContain("已进入 Pending");
    expect(wrapper.get("[role='status']").text()).toContain("已进入 Pending");
    const submitCall = fetcher.mock.calls.find(([url]) =>
      String(url).includes("ai_draft.submit_for_review"),
    );
    expect(Object.fromEntries(new URLSearchParams(String(submitCall?.[1]?.body)))).toMatchObject({
      draft_id: "DRAFT-1",
      expected_revision: "2",
    });

    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
    delete host.frappe;
    refreshSession();
  });

  it("AI Draft 冲突后刷新草稿资源且不自动重放提交", async () => {
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
      draft_id: "DRAFT-CONFLICT",
      kind: "Review Case" as const,
      status: "AI Draft" as const,
      origin: "AI" as const,
      subject: "旧草稿",
      evidence: [],
      model: { name: "deepseek-v4-flash" as const, version: "2026-08-01" },
      revision: 2,
    };
    let draftReads = 0;
    let submitCalls = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("review_case.list")) {
        return Promise.resolve(okV1({ cases: [], total: 0, next_cursor: null }));
      }
      if (url.includes("ai_draft.list")) {
        draftReads += 1;
        return Promise.resolve(
          okV4({
            drafts: [
              draftReads === 1
                ? draft
                : { ...draft, subject: "刷新后的草稿", revision: 3 },
            ],
            next_cursor: null,
          }),
        );
      }
      submitCalls += 1;
      return Promise.resolve(
        new Response(
          JSON.stringify({
            message: {
              error: {
                code: "revision_conflict",
                message: "草稿版本已更新，请重新核对。",
                request_id: "req-draft-conflict",
                details: {},
              },
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    const wrapper = mount(ReviewQueueView, {
      global: {
        stubs: { GbosButton: gbosButtonStub },
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

    await wrapper.get("button[data-draft-submit='DRAFT-CONFLICT']").trigger("click");
    await wrapper.get("dialog button[data-action='confirm']").trigger("click");
    await flushPromises();

    expect(wrapper.get("[role='alert']").text()).toContain("草稿版本已更新");
    expect(wrapper.text()).toContain("刷新后的草稿");
    expect(draftReads).toBe(2);
    expect(submitCalls).toBe(1);

    delete host.frappe;
    refreshSession();
  });
});
