<template>
  <article class="evidence-card">
    <header>
      <p class="eyebrow">
        已治理内容
      </p>
      <h2>{{ title }}</h2>
    </header>

    <section aria-labelledby="summary-title">
      <h3 id="summary-title">
        中文摘要
      </h3>
      <p v-if="summaryZh">
        {{ summaryZh }}
      </p>
      <p v-else class="evidence-card__uncertain">
        暂无已确认中文摘要，请人工核对原文。
      </p>
    </section>

    <section aria-labelledby="original-title">
      <div class="evidence-card__original-heading">
        <h3 id="original-title">
          原文
        </h3>
        <span>原始语言：{{ languageLabel }}</span>
      </div>
      <blockquote :lang="originalLanguage" :dir="direction">
        {{ originalText }}
      </blockquote>
    </section>
  </article>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  title: string;
  summaryZh?: string;
  originalText: string;
  originalLanguage: string;
}>();

const LANGUAGE_NAMES: Record<string, string> = {
  ar: "阿拉伯语",
  en: "英语",
  es: "西班牙语",
  zh: "中文",
  "zh-CN": "简体中文",
};

const baseLanguage = computed(() => props.originalLanguage.split("-")[0] ?? "");
const languageLabel = computed(
  () =>
    `${LANGUAGE_NAMES[props.originalLanguage] ?? LANGUAGE_NAMES[baseLanguage.value] ?? "未知语言"}（${props.originalLanguage}）`,
);
const direction = computed(() =>
  ["ar", "fa", "he", "ur"].includes(baseLanguage.value) ? "rtl" : "ltr",
);
</script>
