import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { describe, expect, it, vi } from "vitest";

import {
  BFF_V2_ENDPOINTS,
  BFF_V4_ENDPOINTS,
  createBffClient,
  type Fetcher,
} from "@/api/bff";
import { BFF_CLIENT_KEY } from "@/api/injection";
import ReviewDecisionForm from "@/components/ReviewDecisionForm.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { refreshSession } from "@/session";
import ReviewDetailView from "@/views/ReviewDetailView.vue";
import ReviewQueueView from "@/views/ReviewQueueView.vue";

const ok = (data: unknown, requestId = "req-review") =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: requestId, schema_version: "1.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const okV4 = (data: unknown, requestId = "req-identity-review") =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: requestId, schema_version: "4.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const detailFixture = {
  case: {
    name: "REVIEW-01HZX",
    title: "确认客户反馈事实",
    team: "TEAM-SALES",
    assigned_reviewer: "reviewer@example.invalid",
    review_status: "Pending",
    case_revision: 3,
    case_payload_hash: "a".repeat(64),
    subject: {
      doctype: "GBOS Sample Feedback",
      name: "FEEDBACK-01HZX",
      revision: 2,
      payload_hash: "b".repeat(64),
      snapshot: {
        title: "中东客户试香反馈",
        summary_zh: "客户希望降低甜度后再确认。",
        nested: { market: "GCC", tags: ["citrus", "less-sweet"] },
      },
    },
    evidence: [
      {
        evidence_type: "Evidence",
        reference: "EVID-01HZX",
        payload_hash: "c".repeat(64),
      },
    ],
    policy_reference: "gbos-action-policy@1.0.0",
    origin: "Fixture",
  },
  decision: null,
};

describe("Gate 4 Review BFF client", () => {
  it("只新增三个版本化 Review 端点，不改变冻结的 v1 端点集", () => {
    expect(BFF_V2_ENDPOINTS).toEqual({
      reviewList: "/api/method/esan_gbos.api.v2.review_case.list",
      reviewGet: "/api/method/esan_gbos.api.v2.review_case.get",
      reviewDecide: "/api/method/esan_gbos.api.v2.review_case.decide",
    });
  });

  it("审核命令携带 CSRF、双 revision、主体摘要、证据、策略和幂等键", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok(detailFixture));
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-review",
    });

    await client.decideReviewCase({
      name: "REVIEW-01HZX",
      decision: "Approved",
      decision_note: "证据与客户反馈一致。",
      expected_revision: 3,
      expected_subject_revision: 2,
      idempotency_key: "review-command-01",
      subject_payload_sha256: "b".repeat(64),
      evidence_refs: ["EVID-01HZX"],
      policy_version: "gbos-action-policy@1.0.0",
    });

    const [url, init] = fetcher.mock.calls[0] ?? [];
    expect(url).toBe(BFF_V2_ENDPOINTS.reviewDecide);
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: expect.objectContaining({
        "X-Frappe-CSRF-Token": "csrf-review",
      }),
    });
    const body = new URLSearchParams(String(init?.body));
    expect(Object.fromEntries(body)).toMatchObject({
      name: "REVIEW-01HZX",
      decision: "Approved",
      decision_note: "证据与客户反馈一致。",
      expected_revision: "3",
      expected_subject_revision: "2",
      idempotency_key: "review-command-01",
      subject_payload_sha256: "b".repeat(64),
      evidence_refs: JSON.stringify(["EVID-01HZX"]),
      policy_version: "gbos-action-policy@1.0.0",
    });
    expect(String(url)).not.toContain("frappe.client");
  });
});

describe("Gate 4 人工审核界面", () => {
  it("身份解析筛选使用服务端专用列表与详情，并只展示安全目标和固定证据", async () => {
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
    const review = {
      review_case_ref: "IDENTITY-REV-1",
      review_case_revision: 4,
      status: "pending",
      assigned_reviewer: "REVIEWER-1",
      team_ref: "TEAM-SALES",
      mapping_ref: "MAP-1",
      mapping_revision: 2,
      target: {
        candidate_type: "Party",
        candidate_ref: "PROTECTED-TARGET-MUST-NOT-RENDER",
        display_label: "海湾香氛客户",
      },
      evidence_refs: ["EVID-IDENTITY-1"],
      policy_version: "identity-resolution-v1",
    };
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes(BFF_V4_ENDPOINTS.identityListPendingReviews)) {
        return Promise.resolve(okV4({ reviews: [review], has_more: false }));
      }
      if (url.includes(BFF_V4_ENDPOINTS.identityGetPendingReview)) {
        return Promise.resolve(okV4({ review }));
      }
      if (url.includes(BFF_V2_ENDPOINTS.reviewList)) {
        return Promise.resolve(ok({ cases: [], total: 0, next_cursor: null }));
      }
      return Promise.resolve(okV4({ drafts: [], next_cursor: null }));
    });
    const wrapper = mount(ReviewQueueView, {
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

    await wrapper.get("select[name='review_kind']").setValue("identity");
    await flushPromises();
    expect(wrapper.text()).toContain("Identity Resolution");
    expect(wrapper.text()).toContain("海湾香氛客户");
    expect(wrapper.text()).toContain("Party");
    expect(wrapper.text()).toContain("EVID-IDENTITY-1");
    expect(wrapper.text()).toContain("identity-resolution-v1");
    expect(wrapper.text()).toContain("REVIEWER-1");
    expect(wrapper.text()).not.toContain("PROTECTED-TARGET-MUST-NOT-RENDER");
    expect(wrapper.text()).not.toContain("subject_snapshot");
    expect(
      fetcher.mock.calls.some(([input]) =>
        String(input).includes(BFF_V4_ENDPOINTS.identityListPendingReviews),
      ),
    ).toBe(true);

    await wrapper.get("button[data-identity-detail='IDENTITY-REV-1']").trigger("click");
    await flushPromises();
    expect(
      fetcher.mock.calls.some(([input]) =>
        String(input).includes(BFF_V4_ENDPOINTS.identityGetPendingReview),
      ),
    ).toBe(true);

    delete host.frappe;
    refreshSession();
  });

  it("审核队列读取专用 Review API 并只显示待审案件", async () => {
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(
      ok({
        cases: [detailFixture.case],
        total: 1,
        page_size: 20,
        next_cursor: null,
      }),
    );
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-review",
    });
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/gbos/review/:id", component: { template: "<div />" } }],
    });
    await router.push("/gbos/review/placeholder");
    await router.isReady();

    const wrapper = mount(ReviewQueueView, {
      global: {
        plugins: [router],
        provide: { [BFF_CLIENT_KEY as symbol]: client },
      },
    });
    await flushPromises();

    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes(BFF_V2_ENDPOINTS.reviewList),
      ),
    ).toBe(true);
    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(OperationalListTemplate).exists()).toBe(true);
    expect(wrapper.findAllComponents(ResourceBoundary).length).toBeGreaterThanOrEqual(2);
    expect(wrapper.text()).toContain("确认客户反馈事实");
    expect(wrapper.text()).toContain("待审核");
    expect(wrapper.text()).toContain("演示数据");
    expect(wrapper.text()).not.toContain("客户希望降低甜度");
  });

  it("AI Draft 读取失败时仍展示可用 Review Cases，且失败状态保持独立", async () => {
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
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("review_case.list")) {
        return Promise.resolve(
          ok({ cases: [detailFixture.case], total: 1, next_cursor: null }),
        );
      }
      return Promise.reject(new TypeError("Failed to fetch"));
    });
    const wrapper = mount(ReviewQueueView, {
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

    expect(wrapper.get("[data-review-resource='cases']").text()).toContain(
      "确认客户反馈事实",
    );
    expect(
      wrapper.find("[data-review-resource='drafts'] .state-panel--offline").exists(),
    ).toBe(true);

    delete host.frappe;
    refreshSession();
  });

  it("Review Cases 权限失败时仍展示 AI Draft，但不把草稿伪装为正式案件", async () => {
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
    const permission = new Response(
      JSON.stringify({
        message: {
          error: {
            code: "permission_denied",
            message: "无权读取审核案件。",
            request_id: "req-review-denied",
            details: {},
          },
        },
      }),
      { status: 403, headers: { "Content-Type": "application/json" } },
    );
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = String(input);
      if (url.includes("review_case.list")) {
        return Promise.resolve(permission.clone());
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            message: {
              data: {
                drafts: [
                  {
                    draft_id: "DRAFT-PARTIAL",
                    kind: "Review Case",
                    status: "AI Draft",
                    origin: "AI",
                    subject: "只是一份 AI 草稿",
                    evidence: [],
                    model: { name: "deepseek-v4-flash", version: "2026-08-01" },
                    revision: 1,
                  },
                ],
                next_cursor: null,
              },
              meta: { request_id: "req-draft", schema_version: "4.0" },
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    const wrapper = mount(ReviewQueueView, {
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
      wrapper.find("[data-review-resource='cases'] .state-panel--permission").exists(),
    ).toBe(true);
    expect(wrapper.get("[data-review-resource='drafts']").text()).toContain(
      "只是一份 AI 草稿",
    );
    expect(wrapper.find("a[href*='/gbos/review/DRAFT-PARTIAL']").exists()).toBe(false);

    delete host.frappe;
    refreshSession();
  });

  it("Review Cases 使用 opaque cursor，可回首页并丢弃迟到的上一页结果", async () => {
    let resolveLatePage: ((response: Response) => void) | undefined;
    let reviewRequest = 0;
    const fetcher = vi.fn<Fetcher>().mockImplementation((input) => {
      const url = new URL(String(input), "https://gbos.invalid");
      if (!url.pathname.includes("review_case.list")) {
        return Promise.resolve(ok({ drafts: [], next_cursor: null }));
      }
      reviewRequest += 1;
      if (reviewRequest === 1) {
        return Promise.resolve(
          ok({
            cases: [detailFixture.case],
            total: 1,
            next_cursor: "opaque:%2F下一页==",
          }),
        );
      }
      if (reviewRequest === 2) {
        return new Promise<Response>((resolve) => {
          resolveLatePage = resolve;
        });
      }
      return Promise.resolve(
        ok({
          cases: [{ ...detailFixture.case, name: "REVIEW-HOME", title: "首页最新案件" }],
          total: 1,
          next_cursor: null,
        }),
      );
    });
    const wrapper = mount(ReviewQueueView, {
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

    await wrapper.get("button[data-pagination='next']").trigger("click");
    expect(wrapper.text()).not.toContain("确认客户反馈事实");
    const nextUrl = new URL(
      String(fetcher.mock.calls.find(([url]) => String(url).includes("cursor="))?.[0]),
      "https://gbos.invalid",
    );
    expect(nextUrl.searchParams.get("cursor")).toBe("opaque:%2F下一页==");

    await wrapper.get("button[data-pagination='home']").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("首页最新案件");

    resolveLatePage?.(
      ok({
        cases: [{ ...detailFixture.case, name: "REVIEW-LATE", title: "迟到案件" }],
        total: 1,
        next_cursor: null,
      }),
    );
    await flushPromises();
    expect(wrapper.text()).toContain("首页最新案件");
    expect(wrapper.text()).not.toContain("迟到案件");
  });

  it("审核说明持续展示最少字符帮助、当前计数和 aria 关联，并阻止重复提交", async () => {
    const wrapper = mount(ReviewDecisionForm, {
      props: { submitting: false, resetKey: 0 },
    });
    const textarea = wrapper.get("textarea");
    const describedBy = textarea.attributes("aria-describedby")?.split(" ") ?? [];

    expect(describedBy).toHaveLength(2);
    expect(describedBy.every((id) => wrapper.find(`#${id}`).exists())).toBe(true);
    expect(wrapper.text()).toContain("至少 4 个字符");
    expect(wrapper.text()).toContain("0 / 1000");
    expect(wrapper.findAllComponents(GbosButton)).toHaveLength(2);

    await textarea.setValue("证据充分");
    expect(wrapper.text()).toContain("4 / 1000");
    await wrapper.get('[data-decision="Approved"]').trigger("click");
    await wrapper.get('[data-decision="Approved"]').trigger("click");
    expect(wrapper.emitted("decide")).toEqual([["Approved", "证据充分"]]);
  });

  it("详情页只能决定案件，批准后不发送任何主体修改请求", async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(ok(detailFixture))
      .mockResolvedValueOnce(
        ok({
          case: {
            ...detailFixture.case,
            review_status: "Approved",
            case_revision: 4,
            decision_note: "证据充分，可以确认。",
          },
          decision: {
            name: "DECISION-01HZX",
            decision: "Approved",
          },
        }),
      );
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-review",
    });
    const wrapper = mount(ReviewDetailView, {
      props: { id: "REVIEW-01HZX" },
      global: { provide: { [BFF_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    expect(wrapper.findComponent(DetailCommandTemplate).exists()).toBe(true);
    expect(wrapper.text()).toContain("客户希望降低甜度后再确认");
    expect(wrapper.text()).toContain("EVID-01HZX");
    expect(wrapper.text()).toContain(detailFixture.case.case_payload_hash);
    expect(wrapper.text()).toContain(detailFixture.case.subject.payload_hash);
    expect(wrapper.text()).toContain('"market": "GCC"');
    expect(wrapper.text()).toContain('"tags": [');
    await wrapper.get("textarea").setValue("证据充分，可以确认。");
    await wrapper.get('button[data-decision="Approved"]').trigger("click");
    await flushPromises();

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining(BFF_V2_ENDPOINTS.reviewGet),
      BFF_V2_ENDPOINTS.reviewDecide,
    ]);
    expect(fetcher.mock.calls.some(([url]) => String(url).includes("set_value"))).toBe(false);
    expect(fetcher.mock.calls.some(([url]) => String(url).includes("subject"))).toBe(false);
    expect(wrapper.text()).toContain("审核决定已记录");
  });

  it("409 冲突时清除待提交状态并刷新案件，不自动重放旧决定", async () => {
    const conflict = new Response(
      JSON.stringify({
        message: {
          error: {
            code: "revision_conflict",
            message: "案件或主体已更新，请重新审核。",
            request_id: "req-conflict",
            details: {},
          },
        },
      }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    );
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(ok(detailFixture))
      .mockResolvedValueOnce(conflict)
      .mockResolvedValueOnce(ok(detailFixture, "req-refreshed"));
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-review",
    });
    const wrapper = mount(ReviewDetailView, {
      props: { id: "REVIEW-01HZX" },
      global: { provide: { [BFF_CLIENT_KEY as symbol]: client } },
    });
    await flushPromises();

    await wrapper.get("textarea").setValue("证据不足，拒绝确认。");
    await wrapper.get('button[data-decision="Rejected"]').trigger("click");
    await flushPromises();

    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[2]?.[0]).toEqual(
      expect.stringContaining(BFF_V2_ENDPOINTS.reviewGet),
    );
    expect(wrapper.text()).toContain("案件或主体已更新，请重新审核");
    expect(wrapper.get("textarea").element.value).toBe("");
  });

  it("原始证据不会写入 localStorage 或 sessionStorage", async () => {
    const localSet = vi.spyOn(Storage.prototype, "setItem");
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(ok(detailFixture));
    const wrapper = mount(ReviewDetailView, {
      props: { id: "REVIEW-01HZX" },
      global: {
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

    expect(wrapper.text()).not.toContain("raw_message");
    expect(localSet).not.toHaveBeenCalled();
    localSet.mockRestore();
  });

  it("External Identity 案件不转储受保护主体快照，通用案件仍保留现有快照视图", async () => {
    const identityFixture = {
      case: {
        ...detailFixture.case,
        title: "Identity Resolution",
        subject: {
          ...detailFixture.case.subject,
          doctype: "GBOS External Identity",
          name: "PROTECTED-MAPPING-REF",
          snapshot: {
            external_subject: "RAW-SUBJECT-MUST-NOT-RENDER",
            target_ref: "PROTECTED-TARGET-MUST-NOT-RENDER",
            model_target: "MODEL-TARGET-MUST-NOT-RENDER",
          },
        },
      },
      decision: null,
    };
    const wrapper = mount(ReviewDetailView, {
      props: { id: "IDENTITY-REV-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher: vi.fn<Fetcher>().mockResolvedValue(ok(identityFixture)),
            isOnline: () => true,
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("身份解析案件");
    expect(wrapper.text()).toContain("身份解析筛选");
    expect(wrapper.text()).not.toContain("RAW-SUBJECT-MUST-NOT-RENDER");
    expect(wrapper.text()).not.toContain("PROTECTED-TARGET-MUST-NOT-RENDER");
    expect(wrapper.text()).not.toContain("MODEL-TARGET-MUST-NOT-RENDER");
    expect(wrapper.text()).not.toContain("PROTECTED-MAPPING-REF");
  });
});
