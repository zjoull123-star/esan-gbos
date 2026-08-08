<template>
  <article class="evidence-panel" :aria-labelledby="titleId">
    <header>
      <p class="evidence-panel__eyebrow">
        已治理内容
      </p>
      <h2 :id="titleId">
        {{ title }}
      </h2>
    </header>

    <section :aria-labelledby="summaryId">
      <h3 :id="summaryId">
        中文摘要
      </h3>
      <p v-if="summaryZh">
        {{ summaryZh }}
      </p>
      <p v-else class="evidence-panel__uncertain">
        暂无已确认中文摘要，请人工核对原文。
      </p>
    </section>

    <section :aria-labelledby="originalId">
      <div class="evidence-panel__original-heading">
        <h3 :id="originalId">
          原文
        </h3>
        <span>原始语言：{{ languageLabel }}</span>
      </div>
      <blockquote
        v-if="originalText"
        :lang="originalLanguage"
        :dir="direction"
      >
        {{ originalText }}
      </blockquote>
      <p v-else class="evidence-panel__uncertain">
        暂无可显示的原文。
      </p>
    </section>
  </article>
</template>

<script setup lang="ts">
import { computed, useId } from "vue";

const props = defineProps<{
  title: string;
  summaryZh?: string;
  originalText?: string;
  originalLanguage: string;
}>();

const instanceId = useId();
const titleId = `${instanceId}-evidence-title`;
const summaryId = `${instanceId}-evidence-summary`;
const originalId = `${instanceId}-evidence-original`;

const languageNames: Record<string, string> = {
  ar: "阿拉伯语",
  en: "英语",
  es: "西班牙语",
  fa: "波斯语",
  he: "希伯来语",
  ur: "乌尔都语",
  zh: "中文",
  "zh-CN": "简体中文",
};

const baseLanguage = computed(() => props.originalLanguage.split("-")[0] ?? "");
const languageLabel = computed(
  () =>
    `${languageNames[props.originalLanguage] ?? languageNames[baseLanguage.value] ?? "未知语言"}（${props.originalLanguage}）`,
);
const direction = computed(() =>
  ["ar", "fa", "he", "ur"].includes(baseLanguage.value) ? "rtl" : "ltr",
);
</script>

<style scoped>
.evidence-panel {
  display: grid;
  gap: 16px;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.evidence-panel header,
.evidence-panel section {
  min-width: 0;
}

.evidence-panel__eyebrow,
.evidence-panel h2,
.evidence-panel h3,
.evidence-panel p,
.evidence-panel blockquote {
  margin: 0;
}

.evidence-panel__eyebrow {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
}

.evidence-panel h2 {
  margin-top: 4px;
  font-size: 18px;
  line-height: 1.4;
}

.evidence-panel h3 {
  font-size: 14px;
  line-height: 1.4;
}

.evidence-panel section > p,
.evidence-panel blockquote {
  margin-top: 8px;
  color: var(--gbos-text);
  font-size: 14px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.evidence-panel__original-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.evidence-panel__original-heading span {
  color: var(--gbos-muted);
  font-size: 12px;
}

.evidence-panel blockquote {
  padding: 12px;
  border-inline-start: 3px solid var(--gbos-accent);
  border-radius: 0 var(--gbos-radius-control) var(--gbos-radius-control) 0;
  background: var(--gbos-canvas);
}

.evidence-panel__uncertain {
  color: #8a5a00 !important;
}
</style>
