import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "@/App.vue";
import { createBffClient, type Fetcher } from "@/api/bff";
import { BFF_CLIENT_KEY } from "@/api/injection";
import ObjectSummary from "@/components/data/ObjectSummary.vue";
import Timeline from "@/components/data/Timeline.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import { APP_ROUTES } from "@/router";
import { refreshSession } from "@/session";
import OverviewView from "@/views/OverviewView.vue";
import PartyDetailView from "@/views/PartyDetailView.vue";
import SampleDetailView from "@/views/SampleDetailView.vue";

const apiResponse = (data: unknown) =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: { request_id: "req-view", schema_version: "1.0" },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const clientWith = (data: unknown, options: { online?: boolean } = {}) =>
  createBffClient({
    fetcher: vi.fn<Fetcher>().mockResolvedValue(apiResponse(data)),
    isOnline: () => options.online ?? true,
    getCsrfToken: () => "csrf-test",
  });

const setFrappeSession = (user: string, roles: string[]) => {
  const host = globalThis as typeof globalThis & { frappe?: unknown };
  host.frappe = {
    session: { user },
    boot: { user: { roles } },
    csrf_token: "csrf-test",
  };
  refreshSession();
};

afterEach(() => {
  const host = globalThis as typeof globalThis & { frappe?: unknown };
  delete host.frappe;
  refreshSession();
});

describe("详情页", () => {
  it("客户详情 id 改变时重新读取新客户并清除旧客户内容", async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(
        apiResponse({
          profile: { name: "PARTY-OLD", party_name: "OLD-PARTY" },
          product_briefs: [],
          samples: [],
          demands: [],
        }),
      )
      .mockResolvedValueOnce(
        apiResponse({
          profile: { name: "PARTY-NEW", party_name: "NEW-PARTY" },
          product_briefs: [],
          samples: [],
          demands: [],
        }),
      );
    const wrapper = mount(PartyDetailView, {
      props: { id: "PARTY-OLD" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-test",
          }),
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("OLD-PARTY");

    await wrapper.setProps({ id: "PARTY-NEW" });
    await flushPromises();

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(String(fetcher.mock.calls[1]?.[0])).toContain("party=PARTY-NEW");
    expect(wrapper.text()).toContain("NEW-PARTY");
    expect(wrapper.text()).not.toContain("OLD-PARTY");
  });

  it("样品详情 id 改变时忽略迟到的旧响应和旧 revision", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    let resolveOld: ((value: Response) => void) | undefined;
    const oldResponse = new Promise<Response>((resolve) => {
      resolveOld = resolve;
    });
    const fetcher = vi
      .fn<Fetcher>()
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce(
        apiResponse({
          project: {
            name: "SAMPLE-NEW",
            title: "NEW-SAMPLE",
            revision: 7,
            business_status: "Draft",
          },
          iterations: [],
          shipments: [],
          feedback: [],
        }),
      );
    const wrapper = mount(SampleDetailView, {
      props: { id: "SAMPLE-OLD" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-test",
          }),
        },
      },
    });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(1);

    await wrapper.setProps({ id: "SAMPLE-NEW" });
    await flushPromises();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(String(fetcher.mock.calls[1]?.[0])).toContain("project=SAMPLE-NEW");
    expect(wrapper.text()).toContain("NEW-SAMPLE");
    expect(wrapper.find("form").exists()).toBe(false);

    resolveOld?.(
      apiResponse({
        project: {
          name: "SAMPLE-OLD",
          title: "OLD-SAMPLE",
          revision: 99,
          business_status: "Sent",
        },
        iterations: [],
        shipments: [],
        feedback: [],
      }),
    );
    await flushPromises();

    expect(wrapper.text()).toContain("NEW-SAMPLE");
    expect(wrapper.text()).not.toContain("OLD-SAMPLE");
    expect(wrapper.find("form").exists()).toBe(false);
  });

  it("客户 360 只展示固定 DTO 字段，不转储额外载荷", async () => {
    const wrapper = mount(PartyDetailView, {
      props: { id: "PARTY-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: clientWith({
            profile: {
              name: "PARTY-1",
              party_name: "海湾香氛贸易",
              team: "TEAM-1",
              business_status: "Active",
              unexpected_secret: "MUST-NOT-RENDER",
            },
            organization: null,
            contact: null,
            lead: null,
            deal: null,
            product_briefs: [],
            samples: [],
            demands: [],
          }),
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("客户 360");
    expect(wrapper.text()).toContain("海湾香氛贸易");
    expect(wrapper.text()).not.toContain("MUST-NOT-RENDER");
    expect(wrapper.find("blockquote").exists()).toBe(false);
  });

  it("客户 360 展开真实嵌套响应的全部分组", async () => {
    const wrapper = mount(PartyDetailView, {
      props: { id: "PARTY-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: clientWith({
            profile: { name: "PARTY-1", party_name: "海湾香氛贸易" },
            organization: { name: "ORG-1", organization_name: "Gulf Aroma LLC" },
            contact: { name: "CONTACT-1", full_name: "Mariam" },
            lead: { name: "LEAD-1", lead_name: "Dubai retail lead" },
            deal: { name: "DEAL-1", organization: "ORG-1", status: "Open" },
            product_briefs: [{ name: "BRIEF-1", title: "柑橘香型" }],
            samples: [{ name: "SAMPLE-1", title: "第一轮小样" }],
            demands: [{ name: "DEMAND-1", title: "迪拜交付" }],
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(DetailCommandTemplate).exists()).toBe(true);
    expect(wrapper.findAllComponents(ObjectSummary).length).toBeGreaterThanOrEqual(8);

    for (const text of [
      "客户档案",
      "组织",
      "联系人",
      "销售线索",
      "商机",
      "产品简报",
      "样品项目",
      "客户需求",
      "Gulf Aroma LLC",
      "Mariam",
      "海湾香氛贸易",
    ]) {
      expect(wrapper.text()).toContain(text);
    }
    const sampleSummary = wrapper
      .findAllComponents(ObjectSummary)
      .find((summary) => summary.props("eyebrow") === "SAMPLE-1");
    expect(sampleSummary?.props("fields")).toContainEqual(
      expect.objectContaining({
        key: "sample_link",
        value: "第一轮小样",
        to: "/gbos/sample/SAMPLE-1",
      }),
    );
  });

  it("Sent 样品反馈表从 project.revision 提交 revision、幂等键和 CSRF", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(
        apiResponse({
          project: {
            name: "SAMPLE-1",
            title: "柑橘方向小样",
            revision: 3,
            business_status: "Sent",
          },
          iterations: [{ name: "ITER-1", iteration_number: 1 }],
          shipments: [
            {
              name: "SHIP-1",
              carrier: "DHL",
              tracking_number: "TRACK-1",
              business_status: "Sent",
            },
          ],
          feedback: [],
        }),
      )
      .mockResolvedValueOnce(apiResponse({ name: "FEEDBACK-1", revision: 4 }));
    const client = createBffClient({
      fetcher,
      isOnline: () => true,
      getCsrfToken: () => "csrf-form",
    });
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/", component: { template: "<div />" } }],
    });
    const wrapper = mount(SampleDetailView, {
      props: { id: "SAMPLE-1" },
      global: {
        plugins: [router],
        provide: { [BFF_CLIENT_KEY as symbol]: client },
      },
    });
    await flushPromises();

    expect(wrapper.findComponent(PageHeader).exists()).toBe(true);
    expect(wrapper.findComponent(DetailCommandTemplate).exists()).toBe(true);
    expect(wrapper.findComponent(ObjectSummary).exists()).toBe(true);
    expect(wrapper.findAllComponents(Timeline)).toHaveLength(3);
    expect(wrapper.text()).toContain("样品迭代");
    expect(wrapper.text()).toContain("寄样记录");
    expect(wrapper.text()).toContain("客户反馈");

    await wrapper.get("textarea[name='summary']").setValue("客户确认第一轮香气偏弱。");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    const [, init] = fetcher.mock.calls[1] ?? [];
    expect(init?.headers).toMatchObject({ "X-Frappe-CSRF-Token": "csrf-form" });
    expect(Object.fromEntries(new URLSearchParams(String(init?.body)))).toMatchObject({
      project: "SAMPLE-1",
      summary: "客户确认第一轮香气偏弱。",
      expected_revision: "3",
    });
    expect(wrapper.text()).toContain("反馈已记录");
    expect(wrapper.get("[role='status']").text()).toContain("反馈已记录");
  });

  it("样品反馈失败使用 alert 且不伪装为成功", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(
        apiResponse({
          project: {
            name: "SAMPLE-ERROR",
            title: "失败用例样品",
            revision: 5,
            business_status: "Sent",
          },
          iterations: [],
          shipments: [],
          feedback: [],
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: {
              code: "internal_error",
              message: "反馈暂时无法保存",
              request_id: "req-feedback-error",
              details: {},
            },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
      );
    const wrapper = mount(SampleDetailView, {
      props: { id: "SAMPLE-ERROR" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: createBffClient({
            fetcher,
            isOnline: () => true,
            getCsrfToken: () => "csrf-error",
          }),
        },
      },
    });
    await flushPromises();

    await wrapper.get("textarea[name='summary']").setValue("客户反馈内容");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(wrapper.get("[role='alert']").text()).toContain("反馈暂时无法保存");
    expect(wrapper.find("[role='status']").exists()).toBe(false);
  });

  it("CEO 查看 Sent 样品时保持只读且不显示反馈命令", async () => {
    setFrappeSession("ceo@example.invalid", ["CEO"]);
    const wrapper = mount(SampleDetailView, {
      props: { id: "SAMPLE-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: clientWith({
            project: {
              name: "SAMPLE-1",
              title: "只读样品",
              revision: 3,
              business_status: "Sent",
            },
            iterations: [],
            shipments: [],
            feedback: [],
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.find("form").exists()).toBe(false);
    expect(wrapper.find("textarea[name='summary']").exists()).toBe(false);
    expect(wrapper.text()).toContain("只读访问");
    expect(wrapper.text()).toContain("当前角色不能记录客户反馈");
  });

  it("非 Sent 样品隐藏反馈命令并提示下一步", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const wrapper = mount(SampleDetailView, {
      props: { id: "SAMPLE-1" },
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: clientWith({
            project: {
              name: "SAMPLE-1",
              title: "柑橘方向小样",
              revision: 2,
              business_status: "In Progress",
            },
            iterations: [],
            shipments: [],
            feedback: [],
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.find("form").exists()).toBe(false);
    expect(wrapper.find("textarea[name='summary']").exists()).toBe(false);
    expect(wrapper.text()).toContain("当前状态为 In Progress");
    expect(wrapper.text()).toContain("推进到 Sent 后才能记录客户反馈");
  });
});

describe("应用壳", () => {
  it("产品总览只显示当前授权模块与运行标签，且不读取业务 API 或正式数值", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const fetcher = vi.fn<Fetcher>().mockResolvedValue(apiResponse([]));
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/gbos", component: OverviewView }],
    });
    await router.push("/gbos");
    await router.isReady();

    const wrapper = mount(OverviewView, {
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

    expect(wrapper.get("h1").text()).toBe("产品总览");
    expect(wrapper.find(".page-header__copy").exists()).toBe(true);
    expect(wrapper.text()).toContain("sales@example.invalid");
    expect(wrapper.text()).toContain("在线优先 · 不保留业务离线快照");
    expect(
      wrapper
        .findAll("[aria-label='已授权工作台'] a")
        .map((link) => link.text()),
    ).toEqual(["进入销售协同", "进入沟通观察", "进入邮件收件箱"]);
    expect(wrapper.text()).not.toContain("经营总览");
    expect(wrapper.find("[data-official-value]").exists()).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("只显示当前角色导航，并在越权深链显示权限状态", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [...APP_ROUTES],
    });
    await router.push("/gbos/purchase");
    await router.isReady();

    const wrapper = mount(App, {
      global: {
        plugins: [router],
        provide: { [BFF_CLIENT_KEY as symbol]: clientWith([]) },
      },
    });
    await flushPromises();

    expect(wrapper.get("nav").text()).toContain("销售协同");
    expect(wrapper.get("nav").text()).not.toContain("采购协同");
    expect(wrapper.text()).toContain("当前角色无权查看此页面");
    expect(wrapper.get("a.skip-link").attributes("href")).toBe("#main-content");
    expect(wrapper.get("nav").attributes("aria-label")).toBe("工作区导航");
    expect(wrapper.find("footer").exists()).toBe(false);
  });

  it("离线 fallback 没有 Frappe session 时仍优先显示精确需要联网", async () => {
    const onlineDescriptor = Object.getOwnPropertyDescriptor(navigator, "onLine");
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [...APP_ROUTES],
    });
    await router.push("/gbos/sales");
    await router.isReady();

    const wrapper = mount(App, {
      global: {
        plugins: [router],
        provide: { [BFF_CLIENT_KEY as symbol]: clientWith([]) },
      },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("需要联网");
    expect(wrapper.text()).not.toContain("Frappe session 已失效");
    wrapper.unmount();
    if (onlineDescriptor) {
      Object.defineProperty(navigator, "onLine", onlineDescriptor);
    } else {
      Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    }
  });
});
