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

const fixtureIdentityRef = (
  label: string,
  provider: "email" | "wecom" | "whatsapp" | "phone" | "manual_import" = "email",
) => {
  const tail = `${label.replace(/[^A-Za-z0-9_-]/gu, "_")}${"0".repeat(43)}`.slice(0, 43);
  return `extid:v1:${provider}:${tail}`;
};

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
  it("冻结包含身份解析在内的 exact URL", () => {
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
      identityListStates: "/api/method/esan_gbos.api.v4.identity.list_states",
      identityGetState: "/api/method/esan_gbos.api.v4.identity.get_state",
      identityListCandidates: "/api/method/esan_gbos.api.v4.identity.list_candidates",
      identityListPendingReviews:
        "/api/method/esan_gbos.api.v4.identity.list_pending_reviews",
      identityGetPendingReview:
        "/api/method/esan_gbos.api.v4.identity.get_pending_review",
      identitySubmitForReview:
        "/api/method/esan_gbos.api.v4.identity.submit_for_review",
      identityRevoke: "/api/method/esan_gbos.api.v4.identity.revoke",
    });
  });

  it("身份解析只使用五个 GET 与两个 POST，并封闭校验查询、revision 与幂等字段", async () => {
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const path = new URL(String(input), "https://gbos.invalid").pathname;
      if (path === BFF_V4_ENDPOINTS.identityListStates) {
        return Promise.resolve(okV4({ identities: [], connector_account_owner: null }));
      }
      if (path === BFF_V4_ENDPOINTS.identityGetState) {
        return Promise.resolve(
          okV4({
            identity: {
              identity_ref: fixtureIdentityRef("opaque-participant"),
              provider: "email",
              status: "unresolved",
            },
            connector_account_owner: null,
          }),
        );
      }
      if (path === BFF_V4_ENDPOINTS.identityListCandidates) {
        return Promise.resolve(
          okV4({ candidates: [], eligible_reviewers: [], has_more: false }),
        );
      }
      if (path === BFF_V4_ENDPOINTS.identityListPendingReviews) {
        return Promise.resolve(okV4({ reviews: [], has_more: false }));
      }
      if (path === BFF_V4_ENDPOINTS.identityGetPendingReview) {
        return Promise.resolve(
          okV4({
            review: {
              review_case_ref: "REV-1",
              review_case_revision: 1,
              status: "pending",
              assigned_reviewer: "REVIEWER-1",
              team_ref: "TEAM-1",
              mapping_ref: "MAP-1",
              mapping_revision: 1,
              target: {
                candidate_type: "Party",
                candidate_ref: "PARTY-1",
                display_label: "安全客户标签",
              },
              evidence_refs: [],
              policy_version: "identity-resolution-v1",
            },
          }),
        );
      }
      return Promise.resolve(
        okV4({ status: path.endsWith("revoke") ? "revoked" : "pending", mapping_ref: "MAP-1", mapping_revision: 1 }),
      );
    });
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-identity",
    });
    const identityRef = fixtureIdentityRef("opaque-participant");

    await client.listIdentityStates("OBS-1");
    await client.getIdentityState("OBS-1", identityRef);
    await client.listIdentityCandidates({
      observationId: "OBS-1",
      identityRef,
      candidateType: "Party",
      search: "海湾",
      page: 2,
      pageSize: 20,
    });
    await client.listPendingIdentityReviews({ page: 2, pageSize: 20 });
    await client.getPendingIdentityReview("REV-1");
    const submit = {
      observation_id: "OBS-1",
      identity_ref: identityRef,
      suggestion_key: `suggestion:v1:${"a".repeat(64)}`,
      selected_candidate_type: "Party" as const,
      selected_candidate_ref: "PARTY-1",
      assigned_reviewer: "REVIEWER-1",
      expected_state: "unresolved" as const,
      expected_revision: 0 as const,
      idempotency_key: "identity-submit-1",
    };
    await Promise.all([
      client.submitIdentityForReview(submit),
      client.submitIdentityForReview(submit),
    ]);
    await client.revokeIdentity({
      observation_id: "OBS-1",
      identity_ref: identityRef,
      mapping_ref: "MAP-1",
      expected_revision: 3,
      idempotency_key: "identity-revoke-1",
    });

    expect(fetcher).toHaveBeenCalledTimes(7);
    expect(
      fetcher.mock.calls.map(([input, init]) => [
        new URL(String(input), "https://gbos.invalid").pathname,
        init?.method,
      ]),
    ).toEqual([
      [BFF_V4_ENDPOINTS.identityListStates, "GET"],
      [BFF_V4_ENDPOINTS.identityGetState, "GET"],
      [BFF_V4_ENDPOINTS.identityListCandidates, "GET"],
      [BFF_V4_ENDPOINTS.identityListPendingReviews, "GET"],
      [BFF_V4_ENDPOINTS.identityGetPendingReview, "GET"],
      [BFF_V4_ENDPOINTS.identitySubmitForReview, "POST"],
      [BFF_V4_ENDPOINTS.identityRevoke, "POST"],
    ]);
    const candidateUrl = new URL(
      String(fetcher.mock.calls[2]?.[0]),
      "https://gbos.invalid",
    );
    expect(Object.fromEntries(candidateUrl.searchParams)).toEqual({
      observation_id: "OBS-1",
      identity_ref: identityRef,
      candidate_type: "Party",
      search: "海湾",
      page: "2",
      page_size: "20",
    });
    const submitBody = Object.fromEntries(
      new URLSearchParams(String(fetcher.mock.calls[5]?.[1]?.body)),
    );
    expect(submitBody).toEqual({
      observation_id: submit.observation_id,
      identity_ref: submit.identity_ref,
      suggestion_key: submit.suggestion_key,
      selected_candidate_type: submit.selected_candidate_type,
      selected_candidate_ref: submit.selected_candidate_ref,
      assigned_reviewer: submit.assigned_reviewer,
      expected_state: submit.expected_state,
      expected_revision: "0",
      idempotency_key: submit.idempotency_key,
    });
    expect(fetcher.mock.calls[5]?.[1]).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
  });

  it("身份响应遇到额外字段时失败关闭", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      okV4({
        identities: [],
        connector_account_owner: null,
        external_subject: "RAW-SUBJECT-MUST-NOT-PASS",
      }),
    );
    const client = createBffClient({ fetcher, isOnline: () => true });

    await expect(client.listIdentityStates("OBS-1")).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it.each([
    ["空引用", ""],
    ["原始邮箱", "extid:v1:email:person@example.invalid"],
    ["原始电话", "extid:v1:phone:+8613800000000"],
    ["未知 provider", "extid:v1:telegram:opaque-token"],
  ])("身份读取与命令拒绝%s", async (_label, identityRef) => {
    const fetcher = vi.fn<Fetcher>();
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-identity",
    });
    const query = {
      observationId: "OBS-1",
      identityRef,
      candidateType: "Party" as const,
    };

    await expect(client.listIdentityCandidates(query)).rejects.toMatchObject({
      code: "validation_error",
    });
    await expect(
      client.submitIdentityForReview({
        observation_id: "OBS-1",
        identity_ref: identityRef,
        suggestion_key: `suggestion:v1:${"a".repeat(64)}`,
        selected_candidate_type: "Party",
        selected_candidate_ref: "PARTY-1",
        assigned_reviewer: "REVIEWER-1",
        expected_state: "unresolved",
        expected_revision: 0,
        idempotency_key: "identity-submit-invalid",
      }),
    ).rejects.toMatchObject({ code: "validation_error" });
    await expect(
      client.revokeIdentity({
        observation_id: "OBS-1",
        identity_ref: identityRef,
        mapping_ref: "MAP-1",
        expected_revision: 1,
        idempotency_key: "identity-revoke-invalid",
      }),
    ).rejects.toMatchObject({ code: "validation_error" });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it.each([
    [
      "provider 与引用前缀不一致",
      {
        identities: [
          {
            identity_ref: "extid:v1:phone:opaque-token",
            provider: "email",
            status: "unresolved",
          },
        ],
        connector_account_owner: null,
      },
    ],
    [
      "原始 subject",
      {
        identities: [
          {
            identity_ref: "extid:v1:email:person@example.invalid",
            provider: "email",
            status: "unresolved",
          },
        ],
        connector_account_owner: null,
      },
    ],
    [
      "空展示标签",
      {
        identities: [],
        connector_account_owner: { display_label: "" },
      },
    ],
  ])("身份状态响应对%s失败关闭", async (_label, data) => {
    const client = createBffClient({
      fetcher: vi.fn<Fetcher>().mockResolvedValue(okV4(data)),
      isOnline: () => true,
    });

    await expect(client.listIdentityStates("OBS-1")).rejects.toMatchObject({
      code: "invalid_response",
    });
  });

  it("候选响应拒绝空引用、空标签和超长标签", async () => {
    const client = createBffClient({
      fetcher: vi.fn<Fetcher>().mockResolvedValue(
        okV4({
          candidates: [
            {
              candidate_type: "Party",
              candidate_ref: "",
              display_label: "x".repeat(257),
            },
          ],
          eligible_reviewers: [
            { reviewer_ref: "REVIEWER-1", display_label: "" },
          ],
          has_more: false,
        }),
      ),
      isOnline: () => true,
    });

    await expect(
      client.listIdentityCandidates({
        observationId: "OBS-1",
        identityRef: fixtureIdentityRef("opaque-token"),
        candidateType: "Party",
      }),
    ).rejects.toMatchObject({ code: "invalid_response" });
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
      "/gbos/communications",
      "/gbos/email-gateway",
    ]);
    expect(navigationForRoles(["Sales User"]).map((item) => item.to)).toEqual([
      "/gbos/sales",
      "/gbos/communications",
      "/gbos/email",
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
            {
              type: "Possible Party",
              confidence: 0.61,
              suggestion_key: `suggestion:v1:${"a".repeat(64)}`,
            },
          ],
          participant_identities: [
            {
              identity_ref: fixtureIdentityRef("opaque-participant"),
              provider: "email",
              status: "unresolved",
            },
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
    expect(wrapper.text()).not.toContain("opaque-participant");
    expect(wrapper.text()).not.toContain("PARTY-1");
    expect(wrapper.text()).not.toContain("suggestion:v1:");
    expect(wrapper.find("blockquote").exists()).toBe(false);
  });

  it("身份状态使用安全中文标签，送审锁定重复点击并在 409 后刷新", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "admin@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
    };
    refreshSession();
    const identityRef = fixtureIdentityRef("opaque-form-participant");
    let stateReads = 0;
    let candidateReads = 0;
    let submitCalls = 0;
    let resolveSubmit: ((response: Response) => void) | undefined;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const path = new URL(String(input), "https://gbos.invalid").pathname;
      if (path === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(
          okV4({
            communication: {
              observation_id: "OBS-IDENTITY-1",
              channel: "Email",
              occurred_at: "2026-08-10T02:00:00Z",
              summary_zh: "客户询问样品。",
              original_language: "en",
              classification: "Customer Request",
              review_status: "Unreviewed",
              team_ref: "TEAM-1",
              party_ref: null,
              evidence_count: 1,
              evidence: [{ ref: "EVID-1", locator: "message:1" }],
              fact_proposals: [],
              association_suggestions: [
                {
                  type: "Party",
                  confidence: 0.8,
                  suggestion_key: `suggestion:v1:${"b".repeat(64)}`,
                },
              ],
              participant_identities: [
                { identity_ref: identityRef, provider: "email", status: "unresolved" },
              ],
              model: { name: "deepseek-v4-flash", version: "2026-08-01" },
              raw_access_allowed: false,
            },
          }),
        );
      }
      if (path === BFF_V4_ENDPOINTS.identityListStates) {
        stateReads += 1;
        const statuses = stateReads === 1
          ? ["unresolved", "proposed", "pending", "confirmed", "revoked"]
          : ["pending"];
        return Promise.resolve(
          okV4({
            identities: statuses.map((status, index) => ({
              identity_ref: index === 0 ? identityRef : fixtureIdentityRef(`opaque-${index}`),
              provider: "email",
              status,
              ...(status === "confirmed"
                ? { display_label: "已确认客户", target_type: "Party", mapping_revision: 2 }
                : {}),
            })),
            connector_account_owner: { display_label: "渠道账号负责人" },
          }),
        );
      }
      if (path === BFF_V4_ENDPOINTS.identityListCandidates) {
        candidateReads += 1;
        return Promise.resolve(
          okV4({
            candidates: [
              {
                candidate_type: "Party",
                candidate_ref: "PROTECTED-CANDIDATE",
                display_label: "安全客户标签",
              },
            ],
            eligible_reviewers: [
              { reviewer_ref: "REVIEWER-1", display_label: "审核人甲" },
            ],
            has_more: false,
          }),
        );
      }
      submitCalls += 1;
      return new Promise<Response>((resolve) => {
        resolveSubmit = resolve;
      });
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-IDENTITY-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-identity",
          }),
        },
      },
    });
    await flushPromises();

    for (const label of ["未解析", "已建议", "待审核", "已确认", "已撤回"]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("渠道账号负责人");
    expect(wrapper.text()).toContain("安全客户标签");
    expect(wrapper.text()).not.toContain(identityRef);
    expect(wrapper.text()).not.toContain("PROTECTED-CANDIDATE");
    expect(wrapper.text()).not.toContain("suggestion:v1:");
    expect(wrapper.find("button[data-action='confirm-identity']").exists()).toBe(false);

    await wrapper.get("input[name='candidate']").setValue();
    await wrapper.get("select[name='assigned_reviewer']").setValue("0");
    const form = wrapper.get("form[aria-label='身份关联送审']");
    await form.trigger("submit");
    await form.trigger("submit");
    expect(submitCalls).toBe(1);

    resolveSubmit?.(
      new Response(
        JSON.stringify({
          message: {
            error: {
              code: "revision_conflict",
              message: "身份状态已更新，请重新核对。",
              request_id: "req-identity-stale",
              details: {},
            },
          },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );
    await flushPromises();
    expect(wrapper.text()).toContain("身份状态已更新，请重新核对");
    expect(stateReads).toBe(2);
    expect(candidateReads).toBeGreaterThanOrEqual(1);
    expect(submitCalls).toBe(1);

    delete host.frappe;
    refreshSession();
  });

  it("销售角色只能请求 Party/Contact，角色漂移后立即退回 Party", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "admin@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
    };
    refreshSession();
    const identityRef = fixtureIdentityRef("opaque-role-guard");
    const candidateTypes: string[] = [];
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = new URL(String(input), "https://gbos.invalid");
      if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(okV4({
          communication: {
            observation_id: "OBS-ROLE-GUARD",
            channel: "Email",
            occurred_at: "2026-08-10T02:00:00Z",
            summary_zh: "客户询问样品。",
            original_language: "zh",
            classification: "Customer Request",
            review_status: "Unreviewed",
            team_ref: "TEAM-1",
            party_ref: null,
            evidence_count: 0,
            evidence: [],
            fact_proposals: [],
            association_suggestions: [{
              type: "Party",
              confidence: 0.8,
              suggestion_key: `suggestion:v1:${"e".repeat(64)}`,
            }],
            participant_identities: [{ identity_ref: identityRef, provider: "email", status: "unresolved" }],
            model: { name: "deepseek-v4-flash", version: "2026-08-01" },
            raw_access_allowed: false,
          },
        }));
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListStates) {
        return Promise.resolve(okV4({
          identities: [{ identity_ref: identityRef, provider: "email", status: "unresolved" }],
          connector_account_owner: null,
        }));
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListCandidates) {
        candidateTypes.push(url.searchParams.get("candidate_type") ?? "");
        return Promise.resolve(okV4({
          candidates: [
            { candidate_type: "User", candidate_ref: "USER-SECRET", display_label: "系统用户" },
            { candidate_type: "Party", candidate_ref: "PARTY-SECRET", display_label: "客户主体" },
            { candidate_type: "Contact", candidate_ref: "CONTACT-SECRET", display_label: "客户联系人" },
          ],
          eligible_reviewers: [{ reviewer_ref: "REVIEWER-1", display_label: "审核人甲" }],
          has_more: false,
        }));
      }
      return Promise.resolve(okV4({ status: "pending", mapping_ref: "MAP-1", mapping_revision: 1 }));
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-ROLE-GUARD" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({ fetcher, isOnline: () => true }),
        },
      },
    });
    await flushPromises();

    const typeSelect = wrapper.get("select[name='candidate_type']");
    expect(typeSelect.findAll("option").map((option) => option.attributes("value"))).toEqual([
      "User",
      "Party",
      "Contact",
    ]);
    await typeSelect.setValue("User");
    await flushPromises();
    expect(candidateTypes).toContain("User");

    host.frappe.boot.user.roles = ["Sales User"];
    refreshSession();
    await flushPromises();

    expect(typeSelect.findAll("option").map((option) => option.attributes("value"))).toEqual([
      "Party",
      "Contact",
    ]);
    expect((typeSelect.element as HTMLSelectElement).value).toBe("Party");
    expect(candidateTypes.at(-1)).toBe("Party");
    expect(wrapper.text()).not.toContain("系统用户");
    expect(wrapper.html()).not.toMatch(/USER-SECRET|PARTY-SECRET|CONTACT-SECRET/u);

    delete host.frappe;
    refreshSession();
  });

  it("管理员撤回已确认映射前二次确认，重复操作共用一个幂等请求", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "admin@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
    };
    refreshSession();
    const identityRef = fixtureIdentityRef("opaque-revoke");
    const mappingRef = "MAPPING-RAW-MUST-NOT-RENDER";
    let stateReads = 0;
    let revokeCalls = 0;
    let resolveRevoke: ((response: Response) => void) | undefined;
    const revokeBodies: URLSearchParams[] = [];
    const fetcher = vi.fn<Fetcher>().mockImplementation((input, init) => {
      const url = new URL(String(input), "https://gbos.invalid");
      if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(okV4({
          communication: {
            observation_id: "OBS-REVOKE",
            channel: "Email",
            occurred_at: "2026-08-10T02:00:00Z",
            summary_zh: "已确认参与者。",
            original_language: "zh",
            classification: "Customer Request",
            review_status: "Reviewed",
            team_ref: "TEAM-1",
            party_ref: null,
            evidence_count: 0,
            evidence: [],
            fact_proposals: [],
            association_suggestions: [],
            participant_identities: [],
            model: { name: "deepseek-v4-flash", version: "2026-08-01" },
            raw_access_allowed: false,
          },
        }));
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListStates) {
        stateReads += 1;
        return Promise.resolve(okV4({
          identities: [{
            identity_ref: identityRef,
            provider: "email",
            status: stateReads > 1 ? "revoked" : "confirmed",
            mapping_ref: mappingRef,
            mapping_revision: stateReads > 1 ? 5 : 4,
            target_type: "Party",
            display_label: "海湾香氛客户",
          }],
          connector_account_owner: null,
        }));
      }
      revokeCalls += 1;
      revokeBodies.push(new URLSearchParams(String(init?.body)));
      return new Promise<Response>((resolve) => {
        resolveRevoke = resolve;
      });
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-REVOKE" },
      global: {
        stubs: { GbosButton: gbosButtonStub },
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-identity",
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.html()).not.toContain(identityRef);
    expect(wrapper.html()).not.toContain(mappingRef);
    await wrapper.get("button[data-action='revoke-identity']").trigger("click");
    expect(revokeCalls).toBe(0);
    expect(wrapper.get("dialog").attributes("open")).toBeDefined();
    expect(wrapper.get("dialog").text()).toContain("撤回后该参与者将不再关联");
    expect(wrapper.get("dialog button[data-action='confirm']").text()).toBe("确认撤回");
    await wrapper.get("dialog button[data-action='confirm']").trigger("click");
    await wrapper.get("button[data-action='revoke-identity']").trigger("click");
    expect(revokeCalls).toBe(1);
    expect(revokeBodies[0]?.get("expected_revision")).toBe("4");
    expect(revokeBodies[0]?.get("mapping_ref")).toBe(mappingRef);
    expect(revokeBodies[0]?.get("identity_ref")).toBe(identityRef);
    expect(revokeBodies[0]?.get("idempotency_key")).toMatch(/\S/u);

    resolveRevoke?.(okV4({ status: "revoked", mapping_ref: mappingRef, mapping_revision: 5 }));
    await flushPromises();
    expect(stateReads).toBe(2);
    expect(wrapper.text()).toContain("已撤回身份映射");
    expect(wrapper.find("button[data-action='revoke-identity']").exists()).toBe(false);

    delete host.frappe;
    refreshSession();
  });

  it("已拒绝映射可重新选择候选，送审携带 rejected 和服务端映射版本", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "admin@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
    };
    refreshSession();
    const identityRef = fixtureIdentityRef("opaque-rejected");
    const mappingRef = "MAPPING-REJECTED-MUST-NOT-RENDER";
    let submitBody: URLSearchParams | undefined;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input, init) => {
      const url = new URL(String(input), "https://gbos.invalid");
      if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(okV4({
          communication: {
            observation_id: "OBS-REJECTED",
            channel: "Email",
            occurred_at: "2026-08-11T02:00:00Z",
            summary_zh: "重新核对参与者。",
            original_language: "zh",
            classification: "Customer Request",
            review_status: "Reviewed",
            team_ref: "TEAM-1",
            party_ref: null,
            evidence_count: 0,
            evidence: [],
            fact_proposals: [],
            association_suggestions: [{
              type: "Party",
              confidence: 0.9,
              suggestion_key: `suggestion:v1:${"f".repeat(64)}`,
            }],
            participant_identities: [{
              identity_ref: identityRef,
              provider: "email",
              status: "rejected",
              mapping_ref: mappingRef,
              mapping_revision: 7,
            }],
            model: { name: "deepseek-v4-flash", version: "2026-08-01" },
            raw_access_allowed: false,
          },
        }));
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListStates) {
        return Promise.resolve(okV4({
          identities: [{
            identity_ref: identityRef,
            provider: "email",
            status: "rejected",
            mapping_ref: mappingRef,
            mapping_revision: 7,
          }],
          connector_account_owner: null,
        }));
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListCandidates) {
        return Promise.resolve(okV4({
          candidates: [{
            candidate_type: "Party",
            candidate_ref: "PARTY-RESELECTED",
            display_label: "重选客户主体",
          }],
          eligible_reviewers: [{ reviewer_ref: "REVIEWER-1", display_label: "审核人甲" }],
          has_more: false,
        }));
      }
      submitBody = new URLSearchParams(String(init?.body));
      return Promise.resolve(okV4({
        status: "pending",
        mapping_ref: mappingRef,
        mapping_revision: 8,
      }));
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-REJECTED" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-identity",
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("已拒绝");
    expect(wrapper.find("form[aria-label='身份关联送审']").exists()).toBe(true);
    expect(wrapper.html()).not.toMatch(/opaque-rejected|MAPPING-REJECTED-MUST-NOT-RENDER|PARTY-RESELECTED/u);
    await wrapper.get("input[name='candidate']").setValue();
    await wrapper.get("select[name='assigned_reviewer']").setValue("0");
    await wrapper.get("form[aria-label='身份关联送审']").trigger("submit");
    await flushPromises();

    expect(submitBody?.get("expected_state")).toBe("rejected");
    expect(submitBody?.get("expected_revision")).toBe("7");
    expect(submitBody?.get("identity_ref")).toBe(identityRef);

    delete host.frappe;
    refreshSession();
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
          participant_identities: [],
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

    expect(wrapper.text()).not.toContain(original);
    const reveal = wrapper.get("button[data-action='reveal-original']");
    expect(reveal.text()).toContain("显示受保护原文");
    await reveal.trigger("click");
    expect(wrapper.text().indexOf("客户需要柑橘香调样品")).toBeLessThan(
      wrapper.text().indexOf(original),
    );
    expect(wrapper.get("blockquote").attributes()).toMatchObject({
      lang: "ar",
      dir: "rtl",
    });
    expect(wrapper.get("blockquote").text()).toBe(original);
    await reveal.trigger("click");
    expect(wrapper.text()).not.toContain(original);
  });

  it("观察路由变化后丢弃迟到的旧沟通与身份数据", async () => {
    let resolveOldCommunication: ((response: Response) => void) | undefined;
    let resolveOldIdentity: ((response: Response) => void) | undefined;
    const communicationPayload = (observationId: string, summary: string) => ({
      communication: {
        observation_id: observationId,
        channel: "Email",
        occurred_at: "2026-08-10T02:00:00Z",
        summary_zh: summary,
        original_language: "zh",
        classification: "Customer Request",
        review_status: "Unreviewed",
        team_ref: "TEAM-1",
        party_ref: null,
        evidence_count: 0,
        evidence: [],
        fact_proposals: [],
        association_suggestions: [],
        participant_identities: [],
        model: { name: "deepseek-v4-flash", version: "2026-08-01" },
        raw_access_allowed: false,
      },
    });
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = new URL(String(input), "https://gbos.invalid");
      const observationId = url.searchParams.get("observation_id");
      if (observationId === "OBS-OLD") {
        return new Promise<Response>((resolve) => {
          if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
            resolveOldCommunication = resolve;
          } else {
            resolveOldIdentity = resolve;
          }
        });
      }
      if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(okV4(communicationPayload("OBS-NEW", "新观察摘要")));
      }
      return Promise.resolve(
        okV4({
          identities: [],
          connector_account_owner: { display_label: "新渠道负责人" },
        }),
      );
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-OLD" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
          }),
        },
      },
    });
    await Promise.resolve();
    await wrapper.setProps({ id: "OBS-NEW" });
    await flushPromises();
    expect(wrapper.text()).toContain("新观察摘要");
    expect(wrapper.text()).toContain("新渠道负责人");

    resolveOldCommunication?.(okV4(communicationPayload("OBS-OLD", "旧观察不得残留")));
    resolveOldIdentity?.(
      okV4({
        identities: [],
        connector_account_owner: { display_label: "旧渠道负责人不得残留" },
      }),
    );
    await flushPromises();
    expect(wrapper.text()).toContain("新观察摘要");
    expect(wrapper.text()).not.toContain("旧观察不得残留");
    expect(wrapper.text()).not.toContain("旧渠道负责人不得残留");
  });

  it("多参与者与多建议必须显式选择安全序号并绑定同一候选查询和送审", async () => {
    const host = globalThis as typeof globalThis & {
      frappe?: {
        session: { user: string };
        boot: { user: { roles: string[] } };
      };
    };
    host.frappe = {
      session: { user: "admin@example.invalid" },
      boot: { user: { roles: ["GBOS Admin"] } },
    };
    refreshSession();
    const refs = [
      fixtureIdentityRef("opaque-first"),
      fixtureIdentityRef("opaque-second", "phone"),
    ];
    const suggestionKeys = [
      `suggestion:v1:${"c".repeat(64)}`,
      `suggestion:v1:${"d".repeat(64)}`,
    ];
    let candidateReads = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = new URL(String(input), "https://gbos.invalid");
      if (url.pathname === BFF_V4_ENDPOINTS.communicationGet) {
        return Promise.resolve(
          okV4({
            communication: {
              observation_id: "OBS-MULTI",
              channel: "Email",
              occurred_at: "2026-08-10T02:00:00Z",
              summary_zh: "多人沟通摘要。",
              original_language: "zh",
              classification: "Customer Request",
              review_status: "Unreviewed",
              team_ref: "TEAM-1",
              party_ref: null,
              evidence_count: 0,
              evidence: [],
              fact_proposals: [],
              association_suggestions: [
                { type: "Party", confidence: 0.8, suggestion_key: suggestionKeys[0] },
                { type: "User", confidence: 0.7, suggestion_key: suggestionKeys[1] },
              ],
              participant_identities: refs.map((identity_ref, index) => ({
                identity_ref,
                provider: index === 0 ? "email" : "phone",
                status: "unresolved",
              })),
              model: { name: "deepseek-v4-flash", version: "2026-08-01" },
              raw_access_allowed: false,
            },
          }),
        );
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListStates) {
        return Promise.resolve(
          okV4({
            identities: refs.map((identity_ref, index) => ({
              identity_ref,
              provider: index === 0 ? "email" : "phone",
              status: "unresolved",
            })),
            connector_account_owner: null,
          }),
        );
      }
      if (url.pathname === BFF_V4_ENDPOINTS.identityListCandidates) {
        candidateReads += 1;
        return Promise.resolve(
          okV4({
            candidates: [
              {
                candidate_type: "Party",
                candidate_ref: "PARTY-SELECTED",
                display_label: "安全候选客户",
              },
            ],
            eligible_reviewers: [
              { reviewer_ref: "REVIEWER-1", display_label: "审核人甲" },
            ],
            has_more: false,
          }),
        );
      }
      return Promise.resolve(
        okV4({ status: "pending", mapping_ref: "MAP-1", mapping_revision: 1 }),
      );
    });
    const wrapper = mount(CommunicationDetailView, {
      props: { id: "OBS-MULTI" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-identity",
          }),
        },
      },
    });
    await flushPromises();

    const participantSelect = wrapper.get("select[name='participant_identity']");
    const suggestionSelect = wrapper.get("select[name='association_suggestion']");
    expect(participantSelect.findAll("option").map((option) => option.text())).toEqual([
      "请选择消息参与者",
      "消息参与者 1 · Email",
      "消息参与者 2 · 电话",
    ]);
    expect(suggestionSelect.findAll("option").map((option) => option.text())).toEqual([
      "请选择关联建议",
      "建议 1 · Party · 80%",
      "建议 2 · User · 70%",
    ]);
    expect(wrapper.html()).not.toContain(refs[0]);
    expect(wrapper.html()).not.toContain(refs[1]);
    expect(wrapper.html()).not.toContain(suggestionKeys[0]);
    expect(wrapper.html()).not.toContain(suggestionKeys[1]);
    expect(candidateReads).toBe(0);

    await participantSelect.setValue("1");
    await suggestionSelect.setValue("1");
    await flushPromises();
    expect(candidateReads).toBe(1);
    const candidateUrl = new URL(
      String(
        fetcher.mock.calls.find(([input]) =>
          String(input).includes(BFF_V4_ENDPOINTS.identityListCandidates),
        )?.[0],
      ),
      "https://gbos.invalid",
    );
    expect(candidateUrl.searchParams.get("identity_ref")).toBe(refs[1]);

    await wrapper.get("input[name='candidate']").setValue();
    await wrapper.get("select[name='assigned_reviewer']").setValue("0");
    await wrapper.get("form[aria-label='身份关联送审']").trigger("submit");
    await flushPromises();
    const submitCall = fetcher.mock.calls.find(([input]) =>
      String(input).includes(BFF_V4_ENDPOINTS.identitySubmitForReview),
    );
    const body = Object.fromEntries(new URLSearchParams(String(submitCall?.[1]?.body)));
    expect(body.identity_ref).toBe(refs[1]);
    expect(body.suggestion_key).toBe(suggestionKeys[1]);

    delete host.frappe;
    refreshSession();
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
