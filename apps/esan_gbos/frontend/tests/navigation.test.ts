import { describe, expect, it } from "vitest";

import { navigationForRoles } from "@/navigation";
import { APP_ROUTES, isRouteAllowed } from "@/router";
import { clearSession, readFrappeSession } from "@/session";

describe("角色裁剪导航", () => {
  it("精确声明 Gate 1 的七条业务路由", () => {
    expect(APP_ROUTES.map((route) => route.path)).toEqual([
      "/gbos/ceo",
      "/gbos/sales",
      "/gbos/purchase",
      "/gbos/product",
      "/gbos/review",
      "/gbos/party/:id",
      "/gbos/sample/:id",
    ]);
  });

  it.each([
    [["CEO"], ["经营总览"]],
    [["Sales User"], ["销售协同"]],
    [["Purchase Manager"], ["采购协同"]],
    [["Product/R&D"], ["产品与样品"]],
    [["Reviewer"], ["审核队列"]],
  ])("%j 只显示授权工作台", (roles, expected) => {
    expect(navigationForRoles(roles).map((item) => item.label)).toEqual(expected);
  });

  it("GBOS Admin 可见全部工作台但仍不伪装业务审批角色", () => {
    expect(navigationForRoles(["GBOS Admin"]).map((item) => item.label)).toEqual([
      "经营总览",
      "销售协同",
      "采购协同",
      "产品与样品",
      "审核队列",
    ]);
  });

  it("详情页同样按角色拒绝越权访问", () => {
    expect(isRouteAllowed("/gbos/party/CUST-1", ["Sales User"])).toBe(true);
    expect(isRouteAllowed("/gbos/party/CUST-1", ["Buyer"])).toBe(false);
    expect(isRouteAllowed("/gbos/sample/SAMPLE-1", ["Product/R&D"])).toBe(true);
    expect(isRouteAllowed("/gbos/review", ["CEO"])).toBe(false);
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
