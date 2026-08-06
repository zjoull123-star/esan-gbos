import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { describe, expect, it, vi } from "vitest";

import {
  BFF_V2_ENDPOINTS,
  createBffClient,
  type Fetcher,
} from "@/api/bff";
import { BFF_CLIENT_KEY } from "@/api/injection";
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

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]?.[0])).toContain(BFF_V2_ENDPOINTS.reviewList);
    expect(wrapper.text()).toContain("确认客户反馈事实");
    expect(wrapper.text()).toContain("待审核");
    expect(wrapper.text()).toContain("演示数据");
    expect(wrapper.text()).not.toContain("客户希望降低甜度");
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

    expect(wrapper.text()).toContain("客户希望降低甜度后再确认");
    expect(wrapper.text()).toContain("EVID-01HZX");
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
});
