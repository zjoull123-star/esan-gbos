import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DemoBanner from "@/components/DemoBanner.vue";
import StatePanel from "@/components/StatePanel.vue";
import EvidencePanel from "@/components/data/EvidencePanel.vue";
import ObjectSummary from "@/components/data/ObjectSummary.vue";
import Timeline from "@/components/data/Timeline.vue";
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
    const wrapper = mount(EvidencePanel, {
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
    const wrapper = mount(EvidencePanel, {
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

  it("对象摘要只展示显式字段并保留受控详情链接", () => {
    const wrapper = mount(ObjectSummary, {
      props: {
        title: "客户档案",
        eyebrow: "PARTY-1",
        fields: [
          {
            key: "party_name",
            label: "客户名称",
            value: "海湾香氛贸易",
          },
          {
            key: "sample",
            label: "样品项目",
            value: "第一轮样品",
            to: "/gbos/sample/SAM-1",
          },
          { key: "missing", label: "缺失字段", value: undefined },
        ],
      },
      global: {
        stubs: {
          RouterLink: {
            props: ["to"],
            template: '<a :href="to"><slot /></a>',
          },
        },
      },
    });

    expect(wrapper.get("h2").text()).toBe("客户档案");
    expect(wrapper.text()).toContain("海湾香氛贸易");
    expect(wrapper.get('a[href="/gbos/sample/SAM-1"]').text()).toBe("第一轮样品");
    expect(wrapper.text()).not.toContain("缺失字段");
  });

  it("时间线按服务端顺序展示类型化字段，不转储任意 JSON", () => {
    const wrapper = mount(Timeline, {
      props: {
        title: "样品迭代",
        entries: [
          {
            id: "ITER-2",
            title: "第 2 轮",
            fields: [
              { key: "summary", label: "摘要", value: "调整柑橘前调" },
              { key: "revision", label: "版本", value: 4 },
            ],
          },
        ],
      },
    });

    expect(wrapper.get("h2").text()).toBe("样品迭代");
    expect(wrapper.text()).toContain("第 2 轮");
    expect(wrapper.text()).toContain("调整柑橘前调");
    expect(wrapper.text()).toContain("版本");
    expect(wrapper.text()).not.toContain('{"');
  });

  it("客户与样品详情不再依赖通用 RecordGrid 或 legacy 状态面板", () => {
    const partySource = readFileSync(resolve("src/views/PartyDetailView.vue"), "utf8");
    const sampleSource = readFileSync(resolve("src/views/SampleDetailView.vue"), "utf8");

    for (const source of [partySource, sampleSource]) {
      expect(source).toMatch(/import PageHeader from/);
      expect(source).toMatch(/import ResourceBoundary from/);
      expect(source).not.toMatch(/import RecordGrid from/);
      expect(source).not.toMatch(/import StatePanel from/);
      expect(source).not.toMatch(/var\(--(?!gbos-)/);
    }
    expect(partySource).toMatch(/from "@\/components\/data\/ObjectSummary\.vue"/);
    expect(sampleSource).toMatch(/from "@\/components\/data\/Timeline\.vue"/);
    expect(sampleSource).toMatch(/import GbosButton from/);
  });
});
