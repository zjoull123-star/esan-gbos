import { describe, expect, it } from "vitest";

import {
  defaultWorkspaceForRoles,
  navigationForRoles,
} from "@/navigation";
import { APP_ROUTES, isRouteAllowed } from "@/router";
import { clearSession, readFrappeSession } from "@/session";

describe("角色裁剪导航", () => {
  it("在现有业务路由前声明角色化产品入口", () => {
    expect(APP_ROUTES.map((route) => route.path)).toEqual([
      "/gbos",
      "/gbos/ceo",
      "/gbos/sales",
      "/gbos/purchase",
      "/gbos/product",
      "/gbos/review",
      "/gbos/integrations",
      "/gbos/communications",
      "/gbos/communications/:id",
      "/gbos/review/:id",
      "/gbos/party/:id",
      "/gbos/sample/:id",
    ]);
  });

  it("按当前角色选择第一个授权工作台", () => {
    expect(defaultWorkspaceForRoles(["CEO"])).toBe("/gbos/ceo");
    expect(defaultWorkspaceForRoles(["Sales User"])).toBe("/gbos/sales");
    expect(defaultWorkspaceForRoles(["Integration Admin"])).toBe(
      "/gbos/integrations",
    );
    expect(defaultWorkspaceForRoles([])).toBeUndefined();
  });

  it.each([
    [[], []],
    [["Sales User"], ["销售协同", "沟通观察"]],
    [["Sales Manager"], ["销售协同", "沟通观察"]],
    [["Buyer"], ["采购协同"]],
    [["Purchase Manager"], ["采购协同"]],
    [["Product/R&D"], ["产品与样品"]],
    [["Reviewer"], ["审核队列"]],
    [["Integration Admin"], ["集成状态"]],
    [
      ["CEO"],
      [
        "经营总览",
        "销售协同",
        "采购协同",
        "产品与样品",
        "审核队列",
        "集成状态",
        "沟通观察",
      ],
    ],
    [
      ["GBOS Admin"],
      [
        "经营总览",
        "销售协同",
        "采购协同",
        "产品与样品",
        "审核队列",
        "集成状态",
        "沟通观察",
      ],
    ],
  ])("%j 只显示授权工作台", (roles, expected) => {
    expect(navigationForRoles(roles).map((item) => item.label)).toEqual(expected);
  });

  it("CEO 可见并可进入全部一级菜单", () => {
    const expected = [
      ["经营总览", "/gbos/ceo"],
      ["销售协同", "/gbos/sales"],
      ["采购协同", "/gbos/purchase"],
      ["产品与样品", "/gbos/product"],
      ["审核队列", "/gbos/review"],
      ["集成状态", "/gbos/integrations"],
      ["沟通观察", "/gbos/communications"],
    ];

    expect(
      navigationForRoles(["CEO"]).map((item) => [item.label, item.to]),
    ).toEqual(expected);
    expect(expected.every(([, path]) => isRouteAllowed(path, ["CEO"]))).toBe(
      true,
    );
  });

  it("GBOS Admin 可见全部工作台但仍不伪装业务审批角色", () => {
    expect(navigationForRoles(["GBOS Admin"]).map((item) => item.label)).toEqual([
      "经营总览",
      "销售协同",
      "采购协同",
      "产品与样品",
      "审核队列",
      "集成状态",
      "沟通观察",
    ]);
  });

  it("详情页同样按角色拒绝越权访问", () => {
    expect(isRouteAllowed("/gbos/party/CUST-1", ["Sales User"])).toBe(true);
    expect(isRouteAllowed("/gbos/party/CUST-1", ["Buyer"])).toBe(false);
    expect(isRouteAllowed("/gbos/sample/SAMPLE-1", ["Product/R&D"])).toBe(true);
    expect(isRouteAllowed("/gbos/review", ["CEO"])).toBe(true);
    expect(isRouteAllowed("/gbos/review/REVIEW-1", ["Reviewer"])).toBe(true);
    expect(isRouteAllowed("/gbos/review/REVIEW-1", ["CEO"])).toBe(false);
    expect(isRouteAllowed("/gbos/review/REVIEW-1", ["Sales User"])).toBe(false);
    expect(isRouteAllowed("/gbos/integrations", ["Integration Admin"])).toBe(true);
    expect(isRouteAllowed("/gbos/integrations", ["Sales User"])).toBe(false);
    expect(isRouteAllowed("/gbos/communications/OBS-1", ["CEO"])).toBe(true);
  });
});

describe("Frappe session", () => {
  it("只从当前页面的 Frappe boot/session 内存读取用户和角色", () => {
    const host = globalThis as typeof globalThis & {
      frappe?: unknown;
    };
    host.frappe = {
      session: { user: "sales@example.invalid" },
      boot: { user: { roles: ["Sales User", "Employee"] } },
      csrf_token: "csrf-in-memory",
    };

    expect(readFrappeSession()).toEqual({
      user: "sales@example.invalid",
      roles: ["Sales User", "Employee"],
      authenticated: true,
    });

    delete host.frappe;
  });

  it("Guest 会话不获得任何业务导航", () => {
    expect(navigationForRoles([])).toEqual([]);
    expect(isRouteAllowed("/gbos/ceo", [])).toBe(false);
  });

  it("正式 shell bootstrap JSON 可提供内存 session 且不依赖全局对象", () => {
    document.body.innerHTML = `
      <script id="gbos-bootstrap" type="application/json">
        {"user":"reviewer@example.invalid","roles":["Reviewer"],"csrf_token":"csrf-review"}
      </script>
    `;
    const host = globalThis as typeof globalThis & { frappe?: unknown };
    delete host.frappe;

    expect(readFrappeSession()).toEqual({
      user: "reviewer@example.invalid",
      roles: ["Reviewer"],
      authenticated: true,
    });
  });

  it("清除会话时同步移除 bootstrap 中的 CSRF 与角色", () => {
    document.body.innerHTML = `
      <script id="gbos-bootstrap" type="application/json">
        {"user":"reviewer@example.invalid","roles":["Reviewer"],"csrf_token":"csrf-review"}
      </script>
    `;

    clearSession();

    expect(document.getElementById("gbos-bootstrap")).toBeNull();
    expect(readFrappeSession()).toEqual({
      user: "Guest",
      roles: [],
      authenticated: false,
    });
  });
});
