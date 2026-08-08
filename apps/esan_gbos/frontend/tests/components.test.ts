import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DemoBanner from "@/components/DemoBanner.vue";
import EvidenceCard from "@/components/EvidenceCard.vue";
import RecordGrid from "@/components/RecordGrid.vue";
import StatePanel from "@/components/StatePanel.vue";
import { isFixturePayload } from "@/presentation";

describe("沟通观察页面组合边界", () => {
  it("列表和详情复用共享模板及公共按钮，详情不声明任何命令按钮", () => {
    const listSource = readFileSync(
      resolve("src/views/CommunicationsView.vue"),
      "utf8",
    );
    const detailSource = readFileSync(
      resolve("src/views/CommunicationDetailView.vue"),
      "utf8",
    );

    expect(listSource).toMatch(/import PageHeader from/);
    expect(listSource).toMatch(/import OperationalListTemplate from/);
    expect(listSource).toMatch(/import ResourceBoundary from/);
    expect(listSource).toMatch(/import GbosButton from/);
    expect(listSource.match(/<GbosButton\b/g)).toHaveLength(3);
    expect(detailSource).toMatch(/import PageHeader from/);
    expect(detailSource).toMatch(/import DetailCommandTemplate from/);
    expect(detailSource).toMatch(/import EvidencePanel from/);
    expect(detailSource).toMatch(/import ResourceBoundary from/);
    expect(detailSource).not.toMatch(/<button\b/);
  });

  it("移动列表无需横向表格且两个页面不依赖 legacy theme class", () => {
    const listSource = readFileSync(
      resolve("src/views/CommunicationsView.vue"),
      "utf8",
    );
    const detailSource = readFileSync(
      resolve("src/views/CommunicationDetailView.vue"),
      "utf8",
    );
    const legacyClasses = [
      "button",
      "filter-bar",
      "status-list",
      "command-card",
      "evidence-ref-list",
      "proposal-list",
      "informal-label",
      "restricted-notice",
    ];
    const staticClasses = [...`${listSource}\n${detailSource}`.matchAll(/\bclass="([^"]+)"/g)]
      .flatMap((match) => (match[1] ?? "").split(/\s+/));

    expect(listSource).toContain("data-mobile-list");
    expect(listSource).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.communication-table\s*{[\s\S]*?display:\s*none/,
    );
    expect(listSource).toMatch(
      /@media \(max-width: 767px\)[\s\S]*?\.communication-mobile-list\s*{[\s\S]*?display:\s*grid/,
    );
    for (const legacyClass of legacyClasses) {
      expect(staticClasses).not.toContain(legacyClass);
    }
    expect(listSource).toContain("<style scoped>");
    expect(detailSource).toContain("<style scoped>");
    expect(listSource).not.toMatch(/var\(--(?!gbos-)/);
    expect(detailSource).not.toMatch(/var\(--(?!gbos-)/);
    expect(detailSource).toContain('class="communication-back-link"');
    expect(detailSource).toMatch(/\.communication-back-link\s*{/);
  });
});

describe("可操作中文状态", () => {
  it("加载中使用可访问状态语义", () => {
    const wrapper = mount(StatePanel, { props: { kind: "loading" } });
    expect(wrapper.get("[role='status']").text()).toContain("正在读取最新数据");
    expect(wrapper.get("[role='status']").attributes("aria-live")).toBe("polite");
  });

  it.each([
    ["empty", "暂无符合条件的数据"],
    ["permission", "当前角色无权查看此页面"],
    ["offline", "需要联网"],
    ["error", "暂时无法读取数据"],
  ] as const)("%s 显示中文说明和下一步", (kind, copy) => {
    const wrapper = mount(StatePanel, {
      props: { kind, requestId: kind === "error" ? "req-ui-1" : undefined },
    });
    expect(wrapper.text()).toContain(copy);
    expect(wrapper.find("button").exists()).toBe(true);
    if (kind === "error") {
      expect(wrapper.text()).toContain("req-ui-1");
    }
  });

  it("重试动作通过键盘可操作的按钮发出", async () => {
    const wrapper = mount(StatePanel, { props: { kind: "offline" } });
    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });
});

describe("多语言证据与演示数据", () => {
  it("中文摘要优先，同时原样保留原文和原始语言", () => {
    const original = "نحتاج عينة برائحة الحمضيات";
    const wrapper = mount(EvidenceCard, {
      props: {
        title: "客户反馈",
        summaryZh: "客户需要柑橘香调样品。",
        originalText: original,
        originalLanguage: "ar",
      },
    });

    expect(wrapper.text().indexOf("中文摘要")).toBeLessThan(
      wrapper.text().indexOf("原文"),
    );
    expect(wrapper.get("blockquote").text()).toBe(original);
    expect(wrapper.get("blockquote").attributes()).toMatchObject({
      lang: "ar",
      dir: "rtl",
    });
    expect(wrapper.text()).toContain("原始语言：阿拉伯语（ar）");
  });

  it("不确定或缺失摘要时明确提示人工确认，不伪造翻译", () => {
    const wrapper = mount(EvidenceCard, {
      props: {
        title: "外部消息",
        originalText: "Need approval by Friday.",
        originalLanguage: "en",
      },
    });
    expect(wrapper.text()).toContain("暂无已确认中文摘要");
    expect(wrapper.text()).toContain("请人工核对原文");
  });

  it("fixture 来源显示醒目的演示数据标签", () => {
    const wrapper = mount(DemoBanner);
    expect(wrapper.get("[role='note']").text()).toContain("演示数据");
    expect(wrapper.text()).toContain("不得用于真实业务决定");
    expect(isFixturePayload({ items: [{ origin: "Fixture" }] })).toBe(true);
    expect(isFixturePayload({ origin: "Manual" })).toBe(false);
  });

  it("工作项引用和详情分组生成受控客户或样品链接", () => {
    const wrapper = mount(RecordGrid, {
      props: {
        records: [
          {
            name: "WRK-1",
            title: "客户跟进",
            reference_doctype: "GBOS Party Profile",
            reference_name: "PTY-1",
          },
          {
            name: "SAM-1",
            title: "第一轮样品",
            presentation_section: "样品项目",
          },
        ],
      },
    });

    expect(wrapper.findAll("a.text-link").map((link) => link.attributes("href"))).toEqual([
      "/gbos/party/PTY-1",
      "/gbos/sample/SAM-1",
    ]);
  });

  it("采购候选卡片展示供应商、报价、交期和候选状态", () => {
    const wrapper = mount(RecordGrid, {
      props: {
        records: [
          {
            name: "CANDIDATE-1",
            presentation_section: "评估中 · 候选供应商",
            supplier_name: "合成供应商 A",
            quoted_price: 12.5,
            currency: "USD",
            lead_time_days: 21,
            candidate_status: "Shortlisted",
          },
        ],
      },
    });

    expect(wrapper.text()).toContain("合成供应商 A");
    expect(wrapper.text()).toContain("12.5 USD");
    expect(wrapper.text()).toContain("21 天");
    expect(wrapper.text()).toContain("Shortlisted");
  });
});
