import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import EvidencePanel from "@/components/data/EvidencePanel.vue";
import OperationalList from "@/components/data/OperationalList.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DashboardTemplate from "@/components/layout/DashboardTemplate.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import GbosField from "@/components/ui/GbosField.vue";
import StatusBadge from "@/components/ui/StatusBadge.vue";

const buttonStub = {
  name: "Button",
  inheritAttrs: false,
  props: {
    theme: String,
    variant: String,
    type: String,
    loading: Boolean,
    disabled: Boolean,
  },
  emits: ["click"],
  template: `
    <button
      v-bind="$attrs"
      :type="type"
      :disabled="disabled || loading"
      :data-theme="theme"
      :data-variant="variant"
      :aria-busy="loading ? 'true' : undefined"
      @click="$emit('click', $event)"
    ><slot /></button>
  `,
};

const fieldStub = {
  name: "FormControl",
  inheritAttrs: false,
  props: {
    label: String,
    description: String,
    type: String,
    modelValue: [String, Number],
    required: Boolean,
  },
  emits: ["update:modelValue"],
  template: `
    <label>
      <span>{{ label }}</span>
      <input
        v-bind="$attrs"
        :type="type"
        :value="modelValue"
        :required="required"
        @input="$emit('update:modelValue', $event.target.value)"
      />
      <small v-if="description">{{ description }}</small>
    </label>
  `,
};

const gbosButtonStub = {
  name: "GbosButton",
  props: { type: String, intent: String },
  emits: ["click"],
  template:
    "<button :type='type' :data-intent='intent' @click='$emit(\"click\", $event)'><slot /></button>",
};

let restoreDialogPrototypes: (() => void) | undefined;

const installDialogStubs = () => {
  restoreDialogPrototypes?.();
  const showModalDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    "showModal",
  );
  const closeDescriptor = Object.getOwnPropertyDescriptor(
    HTMLDialogElement.prototype,
    "close",
  );
  const showModal = vi.fn(function (this: HTMLDialogElement) {
    this.setAttribute("open", "");
  });
  const close = vi.fn(function (this: HTMLDialogElement) {
    this.removeAttribute("open");
  });

  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, writable: true, value: showModal },
    close: { configurable: true, writable: true, value: close },
  });
  restoreDialogPrototypes = () => {
    if (showModalDescriptor) {
      Object.defineProperty(
        HTMLDialogElement.prototype,
        "showModal",
        showModalDescriptor,
      );
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
    if (closeDescriptor) {
      Object.defineProperty(
        HTMLDialogElement.prototype,
        "close",
        closeDescriptor,
      );
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
    }
  };

  return { showModal, close };
};

afterEach(() => {
  restoreDialogPrototypes?.();
  restoreDialogPrototypes = undefined;
});

describe("ResourceBoundary", () => {
  it.each(["idle", "loading"] as const)(
    "%s 只显示 loading 状态",
    (state) => {
      const wrapper = mount(ResourceBoundary, {
        props: { state },
        slots: { default: "<p data-content>业务数据</p>" },
      });

      expect(wrapper.get(".state-panel--loading").classes()).toContain(
        "state-panel--loading",
      );
      expect(wrapper.find("[data-content]").exists()).toBe(false);
    },
  );

  it.each([
    ["offline", "offline"],
    ["permission", "permission"],
    ["error", "error"],
  ] as const)("%s 精确映射到 %s 状态", (state, kind) => {
    const wrapper = mount(ResourceBoundary, {
      props: {
        state,
        message: "可操作说明",
        requestId: "req-boundary-1",
      },
      slots: { default: "<p data-content>业务数据</p>" },
    });

    expect(wrapper.get(`.state-panel--${kind}`).text()).toContain("可操作说明");
    if (state === "error") {
      expect(wrapper.text()).toContain("req-boundary-1");
    }
    expect(wrapper.find("[data-content]").exists()).toBe(false);
  });

  it("重新发出状态面板的 retry 事件", async () => {
    const wrapper = mount(ResourceBoundary, { props: { state: "offline" } });
    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });

  it("ready 且 empty 时只显示空状态", () => {
    const wrapper = mount(ResourceBoundary, {
      props: { state: "ready", empty: true },
      slots: { default: "<p data-content>业务数据</p>" },
    });

    expect(wrapper.get(".state-panel--empty").classes()).toContain(
      "state-panel--empty",
    );
    expect(wrapper.find("[data-content]").exists()).toBe(false);
  });

  it("仅 ready 且非 empty 时显示业务插槽", () => {
    const wrapper = mount(ResourceBoundary, {
      props: { state: "ready", empty: false },
      slots: { default: "<p data-content>业务数据</p>" },
    });

    expect(wrapper.get("[data-content]").text()).toBe("业务数据");
    expect(wrapper.find(".state-panel").exists()).toBe(false);
  });
});

describe("页面模板", () => {
  it("PageHeader 提供紧凑语义标题与动作区", () => {
    const wrapper = mount(PageHeader, {
      props: {
        eyebrow: "销售协同",
        title: "待跟进事项",
        description: "仅显示当前在线数据。",
      },
      slots: { actions: "<button type='button'>新建</button>" },
    });

    expect(wrapper.get("header").classes()).toContain("page-header");
    expect(wrapper.get("h1").text()).toBe("待跟进事项");
    expect(wrapper.text()).toContain("销售协同");
    expect(wrapper.text()).toContain("仅显示当前在线数据。");
    expect(wrapper.get("[data-region='actions'] button").text()).toBe("新建");
  });

  it("DashboardTemplate 暴露所有命名区域", () => {
    const wrapper = mount(DashboardTemplate, {
      slots: {
        header: "<div data-slot='header' />",
        status: "<div data-slot='status' />",
        metrics: "<div data-slot='metrics' />",
        main: "<div data-slot='main' />",
        aside: "<div data-slot='aside' />",
      },
    });

    for (const slot of ["header", "status", "metrics", "main", "aside"]) {
      expect(wrapper.find(`[data-slot='${slot}']`).exists()).toBe(true);
    }
    expect(wrapper.get("section[data-region='main']").element.tagName).toBe("SECTION");
    expect(wrapper.get("aside[data-region='aside']").element.tagName).toBe("ASIDE");
  });

  it("OperationalListTemplate 暴露页头、筛选、列表和分页区域", () => {
    const wrapper = mount(OperationalListTemplate, {
      slots: {
        header: "<div data-slot='header' />",
        filters: "<div data-slot='filters' />",
        list: "<div data-slot='list' />",
        pagination: "<div data-slot='pagination' />",
      },
    });

    for (const slot of ["header", "filters", "list", "pagination"]) {
      expect(wrapper.find(`[data-slot='${slot}']`).exists()).toBe(true);
    }
  });

  it("DetailCommandTemplate 分离 facts/main 与 command/aside 区域", () => {
    const wrapper = mount(DetailCommandTemplate, {
      slots: {
        header: "<div data-slot='header' />",
        facts: "<div data-slot='facts' />",
        main: "<div data-slot='main' />",
        command: "<div data-slot='command' />",
      },
    });

    for (const slot of ["header", "facts", "main", "command"]) {
      expect(wrapper.find(`[data-slot='${slot}']`).exists()).toBe(true);
    }
    expect(wrapper.get("section[data-region='main']").element.tagName).toBe("SECTION");
    expect(wrapper.get("aside[data-region='command']").element.tagName).toBe("ASIDE");
  });
});

describe("GBOS 基础控件", () => {
  it.each([
    ["primary", "blue", "solid"],
    ["secondary", "gray", "outline"],
    ["danger", "red", "solid"],
  ] as const)("GbosButton 将 %s 映射为公共主题", (intent, theme, variant) => {
    const wrapper = mount(GbosButton, {
      props: { intent },
      slots: { default: "保存" },
      global: { stubs: { Button: buttonStub } },
    });

    expect(wrapper.get("button").attributes()).toMatchObject({
      "data-theme": theme,
      "data-variant": variant,
    });
    expect(wrapper.text()).toBe("保存");
  });

  it("GbosButton 保留 type/loading/disabled 和 click 事件", async () => {
    const wrapper = mount(GbosButton, {
      props: { type: "submit", loading: true, disabled: true },
      slots: { default: "提交" },
      global: { stubs: { Button: buttonStub } },
    });

    expect(wrapper.get("button").attributes()).toMatchObject({
      type: "submit",
      disabled: "",
      "aria-busy": "true",
    });
    await wrapper.setProps({ loading: false, disabled: false });
    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("click")).toHaveLength(1);
  });

  it("GbosField 转发标签、说明、必填和值更新", async () => {
    const wrapper = mount(GbosField, {
      props: {
        label: "客户名称",
        description: "使用工商登记名称。",
        type: "text",
        modelValue: "旧名称",
        required: true,
      },
      global: { stubs: { FormControl: fieldStub } },
    });

    expect(wrapper.text()).toContain("客户名称");
    expect(wrapper.text()).toContain("使用工商登记名称。");
    expect(wrapper.get("input").attributes("required")).toBe("");
    await wrapper.get("input").setValue("新名称");
    expect(wrapper.emitted("update:modelValue")?.[0]).toEqual(["新名称"]);
  });

  it("GbosField 不将 FormControl 的数值更新转成字符串", async () => {
    const wrapper = mount(GbosField, {
      props: { label: "数量", type: "number", modelValue: 1 },
      global: {
        stubs: {
          FormControl: {
            name: "FormControl",
            emits: ["update:modelValue"],
            template:
              "<button data-emit-number type='button' @click='$emit(\"update:modelValue\", 42)'>更新</button>",
          },
        },
      },
    });

    await wrapper.get("[data-emit-number]").trigger("click");
    const value = wrapper.emitted("update:modelValue")?.[0]?.[0];
    expect(value).toBe(42);
    expect(typeof value).toBe("number");
    const source = readFileSync(resolve("src/components/ui/GbosField.vue"), "utf8");
    expect(source).toContain(
      'defineEmits<{ "update:modelValue": [value: string | number] }>();',
    );
  });

  it("StatusBadge 只显示调用方提供的 tone 和文字", () => {
    const wrapper = mount(StatusBadge, {
      props: { tone: "warning", label: "等待人工审核" },
    });

    expect(wrapper.get("span").text()).toBe("等待人工审核");
    expect(wrapper.get("span").attributes("data-tone")).toBe("warning");
  });

  it("ConfirmDialog 受控关闭并发出 confirm/cancel", async () => {
    const onConfirm = vi.fn();
    const { showModal, close } = installDialogStubs();
    const wrapper = mount(ConfirmDialog, {
      attachTo: document.body,
      props: {
        modelValue: true,
        title: "确认暂停连接器？",
        message: "暂停后不会继续同步新记录。",
        confirmLabel: "确认暂停",
        cancelLabel: "取消",
        onConfirm,
      },
      global: {
        stubs: { GbosButton: gbosButtonStub },
      },
    });

    await nextTick();
    expect(showModal).toHaveBeenCalledTimes(1);
    const dialog = wrapper.get("dialog");
    expect(dialog.attributes()).toMatchObject({
      open: "",
      role: "alertdialog",
      "aria-modal": "true",
    });
    expect(dialog.text()).toContain("确认暂停连接器？");
    expect(dialog.text()).toContain("暂停后不会继续同步新记录。");
    expect(document.activeElement?.textContent).toBe("取消");

    await wrapper.get("[data-action='confirm']").trigger("click");
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted("confirm")).toHaveLength(1);
    expect(wrapper.emitted("update:modelValue")?.at(-1)).toEqual([false]);

    await wrapper.setProps({ modelValue: false });
    expect(close).toHaveBeenCalledTimes(1);
    await wrapper.setProps({ modelValue: true });
    await nextTick();
    expect(showModal).toHaveBeenCalledTimes(2);
    await wrapper.get("[data-action='cancel']").trigger("click");
    expect(wrapper.emitted("cancel")).toHaveLength(1);
    expect(wrapper.emitted("update:modelValue")?.at(-1)).toEqual([false]);
    wrapper.unmount();
  });

  it("ConfirmDialog 直接卸载时关闭原生对话框并归还焦点", async () => {
    const trigger = document.createElement("button");
    trigger.textContent = "打开确认框";
    document.body.append(trigger);
    trigger.focus();
    const { showModal, close } = installDialogStubs();
    const wrapper = mount(ConfirmDialog, {
      attachTo: document.body,
      props: {
        modelValue: true,
        title: "确认操作？",
        message: "此操作需要明确确认。",
      },
      global: { stubs: { GbosButton: gbosButtonStub } },
    });

    await nextTick();
    expect(showModal).toHaveBeenCalledTimes(1);
    expect(document.activeElement).not.toBe(trigger);

    wrapper.unmount();

    expect(close).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(trigger);
    expect(wrapper.emitted("confirm")).toBeUndefined();
    expect(wrapper.emitted("cancel")).toBeUndefined();
  });
});

describe("数据展示组件", () => {
  it("OperationalList 从同一列与行数据渲染桌面列和移动标签", () => {
    const wrapper = mount(OperationalList, {
      props: {
        columns: [
          { key: "owner", label: "负责人" },
          { key: "status", label: "状态" },
        ],
        rows: [
          {
            id: "work-1",
            values: { owner: "王芳", status: "待跟进" },
          },
        ],
      },
    });

    expect(wrapper.findAll("thead th").map((cell) => cell.text())).toEqual([
      "负责人",
      "状态",
    ]);
    expect(wrapper.findAll("tbody td").map((cell) => cell.text())).toEqual([
      "王芳",
      "待跟进",
    ]);
    expect(
      wrapper
        .findAll("[data-mobile-list] [data-label]")
        .map((cell) => [cell.attributes("data-label"), cell.text()]),
    ).toEqual([
      ["负责人", "负责人王芳"],
      ["状态", "状态待跟进"],
    ]);
  });

  it("两个 EvidencePanel 实例生成互不重复的 aria-labelledby ID", () => {
    const wrapper = mount({
      template: `
        <div>
          <EvidencePanel title="证据 A" original-text="A" original-language="en" />
          <EvidencePanel title="证据 B" original-text="B" original-language="en" />
        </div>
      `,
      components: { EvidencePanel },
    });
    const ids = wrapper
      .findAll("[aria-labelledby]")
      .map((node) => node.attributes("aria-labelledby"));

    expect(ids.length).toBeGreaterThan(2);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) {
      expect(wrapper.find(`[id="${id}"]`).exists()).toBe(true);
    }
  });

  it("中文摘要在原文之前，阿拉伯语原文保留 lang 与 RTL", () => {
    const wrapper = mount(EvidencePanel, {
      props: {
        title: "客户反馈",
        summaryZh: "客户希望获得柑橘香调样品。",
        originalText: "نحتاج عينة برائحة الحمضيات",
        originalLanguage: "ar",
      },
    });

    expect(wrapper.text().indexOf("中文摘要")).toBeLessThan(
      wrapper.text().indexOf("原文"),
    );
    expect(wrapper.get("blockquote").attributes()).toMatchObject({
      lang: "ar",
      dir: "rtl",
    });
    expect(wrapper.text()).toContain("原始语言：阿拉伯语（ar）");
  });

  it("缺少中文摘要时明确说明尚未确认", () => {
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
});

describe("Frappe UI 公共导入边界", () => {
  it("包装层和样品页只使用 frappe-ui 公共导出", () => {
    const buttonSource = readFileSync(
      resolve("src/components/ui/GbosButton.vue"),
      "utf8",
    );
    const fieldSource = readFileSync(
      resolve("src/components/ui/GbosField.vue"),
      "utf8",
    );
    const sampleSource = readFileSync(
      resolve("src/views/SampleDetailView.vue"),
      "utf8",
    );

    expect(buttonSource).toMatch(/import\s*{\s*Button\s*}\s*from\s*"frappe-ui"/);
    expect(fieldSource).toMatch(
      /import\s*{\s*FormControl\s*}\s*from\s*"frappe-ui"/,
    );
    expect(sampleSource).toMatch(/import\s*{\s*Button\s*}\s*from\s*"frappe-ui"/);
    expect(`${buttonSource}\n${fieldSource}\n${sampleSource}`).not.toContain(
      "@frappe-ui/button",
    );
  });

  it("移除内部源码 alias 与旧声明，并扫描公共组件源", () => {
    const viteSource = readFileSync(resolve("vite.config.ts"), "utf8");
    const tailwindSource = readFileSync(resolve("tailwind.config.js"), "utf8");
    const declaration = resolve("src/frappe-ui.d.ts");

    expect(viteSource).not.toContain("@frappe-ui/button");
    expect(viteSource).not.toContain("node_modules/frappe-ui/src/components/Button");
    expect(existsSync(declaration)).toBe(true);
    const declarationSource = readFileSync(declaration, "utf8");
    expect(declarationSource).toContain('declare module "frappe-ui"');
    expect(declarationSource).not.toContain("@frappe-ui/button");
    expect(tailwindSource).toContain(
      "./node_modules/frappe-ui/src/components/**/*.{vue,ts}",
    );
  });
});
