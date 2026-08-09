import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const read = (path: string) => readFileSync(resolve(path), "utf8");

describe("GBOS 精简主题", () => {
  it("PWA 与 Frappe 壳使用批准的侧栏和内容区颜色", () => {
    const manifest = JSON.parse(read("public/manifest.webmanifest")) as {
      theme_color: string;
      background_color: string;
      start_url: string;
      icons: Array<{ purpose?: string }>;
    };
    const index = read("index.html");
    const frappeShell = read("../esan_gbos/www/gbos.html");

    expect(manifest).toMatchObject({
      theme_color: "#0B1220",
      background_color: "#F6F8FB",
      start_url: "/gbos",
    });
    expect(manifest.icons[0]?.purpose?.split(/\s+/)).toContain("maskable");
    expect(index).toContain('name="theme-color" content="#0B1220"');
    expect(frappeShell).toContain('name="theme-color" content="#0B1220"');
  });

  it("本地图标使用 maskable 安全区并移除旧绿金配色", () => {
    const icon = read("public/icon.svg");

    expect(icon).toContain('viewBox="0 0 512 512"');
    expect(icon).toContain('data-maskable-safe-area="true"');
    expect(icon).toContain("#0B1220");
    expect(icon).toContain("#6C5CE7");
    expect(icon).not.toMatch(/#12372a|#f6c85f|#e66a4e/i);
  });

  it("全局 CSS 只保留基础规则，不再覆盖语义标签或旧原型主题", () => {
    const styles = read("src/styles.css");

    expect(styles).toContain("@tailwind base;");
    expect(styles).toContain(".view");
    expect(styles).not.toMatch(/--(?:forest|forest-soft|gold|coral|paper|line|shadow):/);
    expect(styles).not.toContain("Georgia");
    expect(styles).not.toMatch(/(?:^|\n)nav\s*\{/);
    expect(styles).not.toMatch(/(?:^|\n)footer\s*\{/);
    expect(styles).not.toMatch(/(?:^|\n)blockquote\s*\{/);
    expect(styles).not.toMatch(/\.(?:record-grid|evidence-card|command-card)\b/);
  });

  it("删除已无调用方的通用记录和证据组件", () => {
    expect(existsSync(resolve("src/components/RecordGrid.vue"))).toBe(false);
    expect(existsSync(resolve("src/components/EvidenceCard.vue"))).toBe(false);
  });

  it("仍在使用的无数据状态和产品入口拥有自身主题边界", () => {
    const statePanel = read("src/components/StatePanel.vue");
    const demoBanner = read("src/components/DemoBanner.vue");
    const overview = read("src/views/OverviewView.vue");

    for (const source of [statePanel, demoBanner, overview]) {
      expect(source).toContain("<style scoped>");
      expect(source).not.toMatch(/var\(--(?:forest|muted|line|paper|gold|coral|shadow)/);
    }
    expect(statePanel).toMatch(/import GbosButton from/);
    expect(overview).not.toContain('class="button');
    expect(overview).not.toContain('class="eyebrow"');
  });
});
