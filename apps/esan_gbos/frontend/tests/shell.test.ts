import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";

import App from "@/App.vue";
import AppShell from "@/components/shell/AppShell.vue";
import appShellSource from "@/components/shell/AppShell.vue?raw";
import appTopbarSource from "@/components/shell/AppTopbar.vue?raw";
import workspaceSidebarSource from "@/components/shell/WorkspaceSidebar.vue?raw";
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
  document.body.style.overflow = "";
  document.body.innerHTML = "";
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
  it("遵循 64px 顶栏与 24px 主内容桌面尺寸", () => {
    expect.soft(appTopbarSource).toContain("min-height: 64px;");
    expect.soft(appShellSource).toContain("padding: 24px;");
  });

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

describe("响应式应用导航", () => {
  const mountShell = async (
    roles: readonly string[] = ["CEO"],
    path = "/gbos/ceo",
  ) => {
    const router = await appRouterAt(path);
    const wrapper = mount(AppShell, {
      attachTo: document.body,
      props: {
        navigation: navigationForRoles(roles),
        sessionLabel: "mobile@example.invalid",
      },
      slots: { default: "<p>移动工作区</p>" },
      global: { plugins: [router] },
    });
    return { router, wrapper };
  };

  it("菜单按钮打开模态抽屉并把焦点移到第一个链接", async () => {
    const { wrapper } = await mountShell();
    const menuButton = wrapper.get("button[aria-controls='mobile-navigation-drawer']");

    expect(menuButton.attributes("aria-expanded")).toBe("false");
    await menuButton.trigger("click");
    await flushPromises();

    expect(menuButton.attributes("aria-expanded")).toBe("true");
    const drawer = wrapper.get("#mobile-navigation-drawer");
    expect(drawer.attributes("role")).toBe("dialog");
    expect(drawer.attributes("aria-modal")).toBe("true");
    expect(drawer.attributes("aria-labelledby")).toBe(
      "mobile-navigation-drawer-title",
    );
    expect(document.activeElement).toBe(drawer.get("a").element);

    wrapper.unmount();
  });

  it("Escape 关闭抽屉并把焦点还给菜单按钮", async () => {
    const { wrapper } = await mountShell();
    const menuButton = wrapper.get("button[aria-controls='mobile-navigation-drawer']");
    await menuButton.trigger("click");
    await flushPromises();

    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await flushPromises();

    expect(wrapper.find("#mobile-navigation-drawer").exists()).toBe(false);
    expect(menuButton.attributes("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(menuButton.element);

    wrapper.unmount();
  });

  it("在抽屉首尾动作项之间循环 Tab 与 Shift+Tab", async () => {
    const { wrapper } = await mountShell();
    await wrapper
      .get("button[aria-controls='mobile-navigation-drawer']")
      .trigger("click");
    await flushPromises();

    const drawer = wrapper.get("#mobile-navigation-drawer");
    const actions = drawer.findAll<HTMLElement>("a, button");
    const first = actions[0]!.element;
    const last = actions.at(-1)!.element;

    last.focus();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
    );
    expect(document.activeElement).toBe(first);

    first.focus();
    document.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        shiftKey: true,
        bubbles: true,
      }),
    );
    expect(document.activeElement).toBe(last);

    wrapper.unmount();
  });

  it("点击背景或路由变化都会关闭抽屉", async () => {
    const { router, wrapper } = await mountShell();
    const menuButton = wrapper.get("button[aria-controls='mobile-navigation-drawer']");
    await menuButton.trigger("click");
    await flushPromises();

    await wrapper.get(".mobile-nav-drawer__backdrop").trigger("click");
    await flushPromises();
    expect(wrapper.find("#mobile-navigation-drawer").exists()).toBe(false);

    await menuButton.trigger("click");
    await flushPromises();
    await router.push("/gbos/sales");
    await flushPromises();
    expect(wrapper.find("#mobile-navigation-drawer").exists()).toBe(false);

    wrapper.unmount();
  });

  it("组件卸载时恢复打开抽屉前的 body 滚动状态", async () => {
    document.body.style.overflow = "scroll";
    const { wrapper } = await mountShell();
    await wrapper
      .get("button[aria-controls='mobile-navigation-drawer']")
      .trigger("click");
    await flushPromises();

    expect(document.body.style.overflow).toBe("hidden");
    wrapper.unmount();
    expect(document.body.style.overflow).toBe("scroll");
  });

  it("CEO 底部导航严格显示首页、销售、沟通、审核、更多", async () => {
    const { wrapper } = await mountShell();
    const bottomNavigation = wrapper.get(
      "nav[aria-label='移动端快捷导航']",
    );

    expect(
      bottomNavigation
        .findAll(".mobile-bottom-nav__item")
        .map((item) => item.text()),
    ).toEqual(["首页", "销售", "沟通", "审核", "更多"]);
    expect(bottomNavigation.get("a[href='/gbos']").attributes("href")).toBe(
      "/gbos",
    );
    await bottomNavigation.get("button").trigger("click");
    await flushPromises();
    expect(wrapper.get("#mobile-navigation-drawer").attributes("role")).toBe(
      "dialog",
    );

    wrapper.unmount();
  });

  it("销售用户的所有移动与桌面入口都不泄露未授权业务链接", async () => {
    const { wrapper } = await mountShell(["Sales User"], "/gbos/sales");

    const hrefs = wrapper.findAll("a").map((link) => link.attributes("href"));
    for (const unauthorizedPath of [
      "/gbos/purchase",
      "/gbos/product",
      "/gbos/review",
      "/gbos/integrations",
    ]) {
      expect(hrefs).not.toContain(unauthorizedPath);
    }
    expect(wrapper.text()).not.toContain("采购协同");
    expect(wrapper.text()).not.toContain("产品与样品");
    expect(wrapper.text()).not.toContain("审核队列");
    expect(wrapper.text()).not.toContain("集成状态");

    wrapper.unmount();
  });

  it("为 1200/768 断点、72px 轨道和无障碍轨道链接声明静态契约", async () => {
    expect(appShellSource).toContain(
      "grid-template-columns: 240px minmax(0, 1fr);",
    );
    expect(appShellSource).toContain("@media (min-width: 768px) and (max-width: 1199px)");
    expect(appShellSource).toContain("grid-template-columns: 72px minmax(0, 1fr);");
    expect(appShellSource).toContain("@media (max-width: 767px)");
    expect(workspaceSidebarSource).toContain("@media (min-width: 768px) and (max-width: 1199px)");
    expect(workspaceSidebarSource).toContain("width: 72px;");
    expect(workspaceSidebarSource).toContain("@media (max-width: 767px)");

    const { wrapper } = await mountShell();
    for (const link of wrapper.findAll(".workspace-sidebar a")) {
      expect(link.attributes("aria-label")).toBe(link.attributes("title"));
      expect(link.attributes("title")).toBeTruthy();
    }
    wrapper.unmount();
  });
});
