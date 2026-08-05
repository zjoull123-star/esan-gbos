import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DemoBanner from "@/components/DemoBanner.vue";
import EvidenceCard from "@/components/EvidenceCard.vue";
import StatePanel from "@/components/StatePanel.vue";
import { isFixturePayload } from "@/presentation";

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
});
