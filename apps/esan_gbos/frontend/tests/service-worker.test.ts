import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..");
const source = readFileSync(resolve(root, "src/service-worker.ts"), "utf8");
const viteConfig = readFileSync(resolve(root, "vite.config.ts"), "utf8");

describe("Service Worker 敏感数据边界", () => {
  it("/api/ 总是 NetworkOnly", () => {
    expect(source).toContain('url.pathname.startsWith("/api/")');
    expect(source).toContain("new NetworkOnly()");
    expect(source.indexOf("new NetworkOnly()")).toBeLessThan(
      source.indexOf("new CacheFirst("),
    );
  });

  it("CacheFirst 仅匹配同源版本化静态资产", () => {
    expect(source).toContain("new CacheFirst(");
    expect(source).toContain('url.origin === self.location.origin');
    expect(source).toContain('url.pathname.startsWith("/assets/esan_gbos/frontend/")');
    expect(source).not.toMatch(/CacheFirst\([^)]*api/su);
  });

  it("浏览器持久化只由 Workbox 管理静态 shell", () => {
    const applicationSource = [
      readFileSync(resolve(root, "src/api/bff.ts"), "utf8"),
      readFileSync(resolve(root, "src/session.ts"), "utf8"),
      readFileSync(resolve(root, "src/App.vue"), "utf8"),
    ].join("\n");
    expect(applicationSource).not.toMatch(
      /\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b|\bCacheStorage\b/u,
    );
    expect(source).toContain("precacheAndRoute(self.__WB_MANIFEST)");
  });

  it("在线导航直取 Frappe shell，仅断网时回退无敏感空壳", () => {
    expect(source).toContain("await fetch(options.request)");
    expect(source).toContain("return offlineShellHandler(options)");
    expect(source).not.toContain(
      "new NavigationRoute(createHandlerBoundToURL",
    );
  });

  it("manifest 声明中文独立 PWA 和 /gbos/ 范围", () => {
    const manifest = JSON.parse(
      readFileSync(resolve(root, "public/manifest.webmanifest"), "utf8"),
    ) as Record<string, unknown>;
    expect(manifest).toMatchObject({
      name: "ESAN GBOS",
      lang: "zh-CN",
      display: "standalone",
      start_url: "/gbos/ceo",
      scope: "/gbos/",
    });
    expect(viteConfig).toContain('scope: "/gbos/"');
    expect(viteConfig).toContain("manifest: true");
    expect(viteConfig).toContain('base: "/assets/esan_gbos/frontend/"');
    expect(viteConfig).toContain('"Service-Worker-Allowed": "/gbos/"');
  });
});
