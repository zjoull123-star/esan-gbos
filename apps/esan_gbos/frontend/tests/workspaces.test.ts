import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { flushPromises, mount, RouterLinkStub } from "@vue/test-utils";
import { nextTick } from "vue";
import { describe, expect, it, vi } from "vitest";

import { createBffClient, type Fetcher } from "@/api/bff";
import { BFF_CLIENT_KEY } from "@/api/injection";
import SourcingComparison from "@/components/data/SourcingComparison.vue";
import { APP_ROUTES } from "@/router";
import ProductWorkspaceView from "@/views/ProductWorkspaceView.vue";
import PurchaseWorkspaceView from "@/views/PurchaseWorkspaceView.vue";
import SalesWorkspaceView from "@/views/SalesWorkspaceView.vue";

const apiResponse = (
  data: unknown,
  meta: { next_cursor?: string | null } = {},
) =>
  new Response(
    JSON.stringify({
      message: {
        data,
        meta: {
          request_id: "req-workspace",
          schema_version: "1.0",
          ...meta,
        },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );

const errorResponse = (
  code: "permission_denied" | "internal_error",
  status: number,
) =>
  new Response(
    JSON.stringify({
      message: {
        error: {
          code,
          message:
            code === "permission_denied"
              ? "当前角色无权执行此操作。"
              : "服务暂时不可用，请稍后重试。",
          request_id: "req-workspace-error",
          details: {},
        },
      },
    }),
    { status, headers: { "Content-Type": "application/json" } },
  );

const clientWith = (
  data: unknown,
  options: { online?: boolean; nextCursor?: string | null } = {},
) =>
  createBffClient({
    fetcher: vi
      .fn<Fetcher>()
      .mockResolvedValue(apiResponse(data, { next_cursor: options.nextCursor })),
    isOnline: () => options.online ?? true,
    getCsrfToken: () => "csrf-test",
  });

const mountSales = (client: ReturnType<typeof createBffClient>) =>
  mount(SalesWorkspaceView, {
    global: {
      provide: { [BFF_CLIENT_KEY as symbol]: client },
      stubs: { RouterLink: RouterLinkStub },
    },
  });

describe("销售工作台", () => {
  it("只显示 work_item.list 的真实列、fixture 来源和安全映射的相关链接", async () => {
    const wrapper = mountSales(
      clientWith({
        items: [
          {
            name: "WORK-1",
            title: "确认客户香调",
            team: "Sales",
            assigned_to: "sales@example.invalid",
            priority: "High",
            due_date: null,
            origin: "Fixture",
            business_status: "Open",
            review_status: "Pending",
            revision: 3,
            reference_doctype: "GBOS Party Profile",
            reference_name: "PARTY / 1",
            modified: "2026-08-09 09:30:00",
          },
          {
            name: "WORK-2",
            title: "确认寄样反馈",
            reference_doctype: "GBOS Sample Project",
            reference_name: "SAMPLE-1",
          },
          {
            name: "WORK-3",
            title: "核对其他引用",
            reference_doctype: "Sales Order",
            reference_name: "SO-1",
          },
        ],
      }),
    );

    await flushPromises();

    expect(wrapper.get("h1").text()).toBe("销售工作项");
    expect(wrapper.text()).toContain("工作项 / 下一动作");
    for (const label of [
      "团队",
      "负责人",
      "优先级",
      "到期日",
      "业务状态",
      "审核状态",
      "版本",
      "更新时间",
    ]) {
      expect(wrapper.text()).toContain(label);
    }
    expect(wrapper.text()).toContain("演示数据");
    expect(wrapper.text()).toContain("Sales Order · SO-1");
    const links = wrapper.findAllComponents(RouterLinkStub);
    expect(
      links.filter(
        (link) => link.props("to") === "/gbos/party/PARTY%20%2F%201",
      ),
    ).toHaveLength(2);
    expect(
      links.filter((link) => link.props("to") === "/gbos/sample/SAMPLE-1"),
    ).toHaveLength(2);
    expect(links.map((link) => link.text())).toEqual([
      "查看相关客户",
      "查看相关样品",
      "查看相关客户",
      "查看相关样品",
    ]);
    expect(
      wrapper.findAllComponents(RouterLinkStub).some((link) =>
        String(link.props("to")).includes("SO-1"),
      ),
    ).toBe(false);
    expect(wrapper.find('a[href*="SO-1"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("客户名称");
    expect(wrapper.text()).not.toContain("金额");
  });

  it("在 767px 以下切换为字段完整的移动标签行", async () => {
    const wrapper = mountSales(
      clientWith({
        items: [
          {
            name: "WORK-MOBILE",
            title: "移动端跟进",
            team: "Sales",
            assigned_to: "owner@example.invalid",
            priority: "High",
            due_date: "2026-08-20",
            business_status: "Open",
            review_status: "Pending",
            revision: 4,
            reference_doctype: "Sales Order",
            reference_name: "SO-MOBILE",
            modified: "2026-08-09 12:00:00",
          },
        ],
      }),
    );
    await flushPromises();

    expect(wrapper.find("[data-desktop-table]").exists()).toBe(true);
    const mobileRow = wrapper.get("[data-mobile-list] li");
    expect(mobileRow.findAll("dt").map((label) => label.text())).toEqual([
      "工作项 / 下一动作",
      "编号",
      "团队",
      "负责人",
      "优先级",
      "到期日",
      "业务状态",
      "审核状态",
      "版本",
      "相关记录",
      "更新时间",
    ]);
    expect(mobileRow.text()).toContain("移动端跟进");
    expect(mobileRow.text()).toContain("Sales Order · SO-MOBILE");

    const source = readFileSync(resolve("src/views/SalesWorkspaceView.vue"), "utf8");
    expect(source).toContain("@media (max-width: 767px)");
    expect(source).toContain(".work-item-mobile {\n    display: block;");
    expect(source).toContain(".work-item-table {\n    display: none;");
  });

  it("使用 response.meta.next_cursor 请求下一页，切页清旧数据且迟到响应不能覆盖首页", async () => {
    let resolveNext: ((value: Response) => void) | undefined;
    const nextResponse = new Promise<Response>((resolve) => {
      resolveNext = resolve;
    });
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValueOnce(
        apiResponse(
          { items: [{ name: "WORK-FIRST", title: "FIRST-PAGE" }] },
          { next_cursor: "opaque/cursor+2" },
        ),
      )
      .mockReturnValueOnce(nextResponse)
      .mockResolvedValueOnce(
        apiResponse({ items: [{ name: "WORK-REFRESH", title: "REFRESHED-FIRST" }] }),
      );
    const wrapper = mountSales(
      createBffClient({
        fetcher,
        isOnline: () => true,
        getCsrfToken: () => "csrf-test",
      }),
    );
    await flushPromises();

    await wrapper.get("[data-next-page]").trigger("click");
    await nextTick();

    expect(String(fetcher.mock.calls[1]?.[0])).toContain(
      "cursor=opaque%2Fcursor%2B2",
    );
    expect(wrapper.text()).not.toContain("FIRST-PAGE");
    expect(wrapper.text()).toContain("正在读取最新数据");

    await wrapper.get("[data-first-page]").trigger("click");
    await flushPromises();
    expect(String(fetcher.mock.calls[2]?.[0])).not.toContain("cursor=");
    expect(wrapper.text()).toContain("REFRESHED-FIRST");

    resolveNext?.(
      apiResponse({ items: [{ name: "WORK-LATE", title: "LATE-NEXT-PAGE" }] }),
    );
    await flushPromises();

    expect(wrapper.text()).toContain("REFRESHED-FIRST");
    expect(wrapper.text()).not.toContain("LATE-NEXT-PAGE");
    expect(wrapper.text()).not.toContain("总计");
  });
});

describe("采购工作台", () => {
  it("按真实 lane 展开候选供应商报价快照，不计算评分、排名或推荐", async () => {
    const wrapper = mount(PurchaseWorkspaceView, {
      global: {
        provide: {
          [BFF_CLIENT_KEY as symbol]: clientWith({
            lanes: {
              Draft: [
                {
                  name: "SRC-1",
                  title: "玻璃瓶询源",
                  team: "Purchase",
                  demand_signal: "DEMAND-9",
                  selected_supplier: null,
                  owner_user: "buyer@example.invalid",
                  origin: "Fixture",
                  business_status: "Draft",
                  review_status: "Pending",
                  revision: 1,
                  modified: "2026-08-09 10:00:00",
                  candidates: [
                    {
                      supplier_name: "供应商 A",
                      external_supplier_id: "EXT-A",
                      quoted_price: 12.5,
                      currency: "USD",
                      lead_time_days: 21,
                      candidate_status: "Quoted",
                      notes: "含基础包装",
                    },
                  ],
                },
              ],
              Invited: [],
              Collecting: [],
              Evaluating: [],
              Selected: [],
              Closed: [],
              Cancelled: [],
            },
          }),
        },
      },
    });
    await flushPromises();

    expect(wrapper.get("h1").text()).toBe("采购询源工作台");
    expect(wrapper.text()).toContain("草稿");
    expect(wrapper.text()).toContain("玻璃瓶询源");
    expect(wrapper.text()).toContain("报价快照");
    expect(wrapper.text()).toContain("供应商 A");
    expect(wrapper.text()).toContain("12.5 USD");
    expect(wrapper.text()).toContain("21 天");
    expect(wrapper.text()).toContain("演示数据");
    for (const forbidden of ["supplier score", "供应商评分", "排名", "推荐供应商"]) {
      expect(wrapper.text().toLowerCase()).not.toContain(forbidden.toLowerCase());
    }
  });

  it("SourcingComparison 对空值保持空白，不伪造默认选择", () => {
    const wrapper = mount(SourcingComparison, {
      props: {
        lanes: [
          {
            key: "Evaluating",
            label: "评估中",
            events: [
              {
                name: "SRC-EMPTY",
                title: "空值询源",
                candidates: [
                  {
                    supplier_name: "供应商 B",
                    quoted_price: null,
                    currency: null,
                    lead_time_days: null,
                  },
                ],
              },
            ],
          },
        ],
      },
    });

    expect(wrapper.text()).toContain("供应商 B");
    expect(wrapper.text()).not.toContain("0 天");
    expect(wrapper.text()).not.toContain("已选定");
    expect(wrapper.text()).not.toContain("暂无报价");
  });

  it("候选报价在 767px 以下切换为字段完整的移动标签行", () => {
    const wrapper = mount(SourcingComparison, {
      props: {
        lanes: [
          {
            key: "Collecting",
            label: "收集中",
            events: [
              {
                name: "SRC-MOBILE",
                title: "移动报价",
                candidates: [
                  {
                    supplier_name: "供应商 Mobile",
                    external_supplier_id: "EXT-MOBILE",
                    quoted_price: 19.75,
                    currency: "AED",
                    lead_time_days: 14,
                    candidate_status: "Quoted",
                    notes: "移动标签完整",
                  },
                ],
              },
            ],
          },
        ],
      },
    });

    expect(wrapper.find("[data-desktop-table]").exists()).toBe(true);
    const mobileRow = wrapper.get("[data-mobile-list] li");
    expect(mobileRow.findAll("dt").map((label) => label.text())).toEqual([
      "供应商",
      "外部供应商 ID",
      "报价",
      "预计交期",
      "候选状态",
      "备注",
    ]);
    expect(mobileRow.text()).toContain("19.75 AED");
    expect(mobileRow.text()).toContain("14 天");

    const source = readFileSync(
      resolve("src/components/data/SourcingComparison.vue"),
      "utf8",
    );
    expect(source).toContain("@media (max-width: 767px)");
    expect(source).toContain(".quote-snapshot__mobile {\n    display: block;");
    expect(source).toContain(".quote-snapshot__table {\n    display: none;");
  });
});

describe("产品工作台", () => {
  it("如实显示产品与样品工作项，不声称存在 Product Brief 或 Sample 索引", async () => {
    const fetcher = vi
      .fn<Fetcher>()
      .mockResolvedValue(
        apiResponse({
          items: [
            {
              name: "WORK-PRODUCT-1",
              title: "确认第三轮样品",
              team: "Product",
              business_status: "Open",
            },
          ],
        }),
      );
    const wrapper = mount(ProductWorkspaceView, {
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

    expect(wrapper.get("h1").text()).toBe("产品与样品工作项");
    expect(wrapper.text()).toContain("确认第三轮样品");
    expect(wrapper.text()).not.toContain("Product Brief 索引");
    expect(wrapper.text()).not.toContain("Sample 索引");
    expect(String(fetcher.mock.calls[0]?.[0])).toContain("work_item.list");
    expect(String(fetcher.mock.calls[0]?.[0])).not.toContain("filters=");
  });
});

describe("工作台资源状态", () => {
  it("loading 时不保留业务数据", async () => {
    const fetcher = vi.fn<Fetcher>().mockReturnValue(new Promise<Response>(() => undefined));
    const wrapper = mountSales(
      createBffClient({ fetcher, isOnline: () => true, getCsrfToken: () => "csrf" }),
    );
    await nextTick();

    expect(wrapper.text()).toContain("正在读取最新数据");
    expect(wrapper.find("[data-work-items]").exists()).toBe(false);
  });

  it.each([
    ["offline", "需要联网"],
    ["permission", "当前角色无权执行此操作。"],
    ["error", "服务暂时不可用，请稍后重试。"],
    ["empty", "暂无符合条件的数据"],
  ] as const)("显示 %s 状态", async (kind, copy) => {
    const fetcher =
      kind === "permission"
        ? vi.fn<Fetcher>().mockResolvedValue(errorResponse("permission_denied", 403))
        : kind === "error"
          ? vi.fn<Fetcher>().mockResolvedValue(errorResponse("internal_error", 500))
          : vi.fn<Fetcher>().mockResolvedValue(apiResponse({ items: [] }));
    const wrapper = mountSales(
      createBffClient({
        fetcher,
        isOnline: () => kind !== "offline",
        getCsrfToken: () => "csrf",
      }),
    );
    await flushPromises();

    expect(wrapper.text()).toContain(copy);
    expect(wrapper.find("[data-work-items]").exists()).toBe(false);
    if (kind === "error") {
      expect(wrapper.text()).toContain("req-workspace-error");
    }
  });

  it("浏览器转为离线时清空已经显示的业务数据", async () => {
    const wrapper = mountSales(
      clientWith({ items: [{ name: "WORK-ONLINE", title: "ONLINE-ONLY" }] }),
    );
    await flushPromises();
    expect(wrapper.text()).toContain("ONLINE-ONLY");

    window.dispatchEvent(new Event("offline"));
    await nextTick();

    expect(wrapper.text()).toContain("需要联网");
    expect(wrapper.text()).not.toContain("ONLINE-ONLY");
  });
});

describe("工作台路由", () => {
  it.each([
    ["sales", SalesWorkspaceView],
    ["purchase", PurchaseWorkspaceView],
    ["product", ProductWorkspaceView],
  ] as const)("%s 路由加载领域专用组件", async (name, expected) => {
    const route = APP_ROUTES.find((candidate) => candidate.name === name);
    expect(route).toBeDefined();
    expect(route).not.toHaveProperty("props.workspace");
    expect(typeof route?.component).toBe("function");
    const module = await (route?.component as () => Promise<{ default: unknown }>)();
    expect(module.default).toBe(expected);
  });
});
