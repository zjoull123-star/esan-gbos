import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";

import App from "@/App.vue";
import AppShell from "@/components/shell/AppShell.vue";
import { navigationForRoles } from "@/navigation";
import { APP_ROUTES } from "@/router";
import { refreshSession } from "@/session";
import baseCss from "@/design/base.css?raw";
import tokensCss from "@/design/tokens.css?raw";

const originalOnlineDescriptor = Object.getOwnPropertyDescriptor(
  navigator,
  "onLine",
);

const setFrappeSession = (user: string, roles: string[]) => {
  const host = globalThis as typeof globalThis & { frappe?: unknown };
  host.frappe = {
    session: { user },
    boot: { user: { roles } },
  };
  refreshSession();
};

const appRouterAt = async (path: string) => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [...APP_ROUTES],
  });
  await router.push(path);
  await router.isReady();
  return router;
};

afterEach(() => {
  const host = globalThis as typeof globalThis & { frappe?: unknown };
  delete host.frappe;
  refreshSession();
  if (originalOnlineDescriptor) {
    Object.defineProperty(navigator, "onLine", originalOnlineDescriptor);
  } else {
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: true,
    });
  }
});

describe("GBOS 语义设计基础", () => {
  it("声明批准的颜色、圆角与字体 token", () => {
    for (const token of [
      "--gbos-canvas: #f6f8fb;",
      "--gbos-sidebar: #0b1220;",
      "--gbos-primary: #6c5ce7;",
      "--gbos-accent: #0f9f8f;",
      "--gbos-text: #172033;",
      "--gbos-muted: #64748b;",
      "--gbos-border: #e2e8f0;",
      "--gbos-accent-text: #0a6f65;",
      "--gbos-radius-control: 14px;",
      "--gbos-radius-card: 16px;",
      '"Noto Sans SC"',
    ]) {
      expect(tokensCss).toContain(token);
    }
  });

  it("提供键盘焦点和减少动效规则", () => {
    expect(baseCss).toContain(":focus-visible");
    expect(baseCss).toContain("@media (prefers-reduced-motion: reduce)");
  });
});

describe("桌面应用壳", () => {
  it("按授权导航分组并保留可访问主内容与会话上下文", async () => {
    const router = await appRouterAt("/gbos/ceo");
    const wrapper = mount(AppShell, {
      props: {
        navigation: navigationForRoles(["CEO"]),
        sessionLabel: "ceo@example.invalid",
      },
      slots: { default: "<p>经营内容</p>" },
      global: { plugins: [router] },
    });

    const navigation = wrapper.get("nav[aria-label='工作区导航']");
    expect(wrapper.get("a[href='/gbos']").text()).toContain("ESAN GBOS");
    expect(wrapper.get("a.skip-link").attributes("href")).toBe("#main-content");
    expect(wrapper.get("#main-content").attributes("tabindex")).toBe("-1");
    expect(navigation.text()).toContain("经营管理");
    expect(navigation.text()).toContain("业务协同");
    expect(navigation.text()).toContain("智能与审核");
    expect(navigation.text()).toContain("系统与集成");
    expect(navigation.get("a[aria-current='page']").text()).toContain("经营总览");
    expect(wrapper.text()).toContain("ceo@example.invalid");
    expect(wrapper.text()).toContain("经营内容");
  });

  it("App 保留越权状态边界", async () => {
    setFrappeSession("sales@example.invalid", ["Sales User"]);
    const router = await appRouterAt("/gbos/purchase");
    const wrapper = mount(App, { global: { plugins: [router] } });
    await flushPromises();

    expect(wrapper.find(".state-panel--permission").exists()).toBe(true);
    expect(wrapper.text()).toContain("当前角色无权查看此页面");
    expect(wrapper.get("nav[aria-label='工作区导航']").text()).not.toContain(
      "采购协同",
    );
  });

  it("App 离线时优先显示离线状态", async () => {
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    const router = await appRouterAt("/gbos/sales");
    const wrapper = mount(App, { global: { plugins: [router] } });
    await flushPromises();

    expect(wrapper.find(".state-panel--offline").exists()).toBe(true);
    expect(wrapper.text()).toContain("需要联网");
    expect(wrapper.text()).not.toContain("Frappe session 已失效");
  });
});
