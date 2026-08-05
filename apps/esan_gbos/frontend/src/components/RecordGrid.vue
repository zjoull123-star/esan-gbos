<template>
  <div class="record-region">
    <DemoBanner v-if="hasFixtureData" />
    <div class="record-grid" role="list">
      <template v-for="(record, index) in records" :key="recordKey(record, index)">
        <div v-if="originalText(record)" role="listitem">
          <EvidenceCard
            :title="title(record)"
            :summary-zh="summary(record)"
            :original-text="originalText(record) ?? ''"
            :original-language="language(record)"
          />
        </div>
        <article v-else class="record-card" role="listitem">
          <header>
            <p class="eyebrow">
              {{ identifier(record) }}
            </p>
            <h2>{{ title(record) }}</h2>
          </header>
          <p v-if="summary(record)">
            {{ summary(record) }}
          </p>
          <dl class="status-list">
            <div v-if="businessStatus(record)">
              <dt>业务状态</dt>
              <dd>{{ businessStatus(record) }}</dd>
            </div>
            <div v-if="reviewStatus(record)">
              <dt>审核状态</dt>
              <dd>{{ reviewStatus(record) }}</dd>
            </div>
          </dl>
          <a v-if="detailLink(record)" class="text-link" :href="detailLink(record)">
            查看详情
          </a>
        </article>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

import DemoBanner from "./DemoBanner.vue";
import EvidenceCard from "./EvidenceCard.vue";
import { isFixturePayload, textField } from "@/presentation";

const props = defineProps<{ records: Record<string, unknown>[] }>();

const hasFixtureData = computed(() => isFixturePayload(props.records));
const identifier = (record: Record<string, unknown>) => {
  const section = textField(record, "presentation_section");
  const id = textField(record, "name", "id");
  return [section, id].filter(Boolean).join(" · ") || "GBOS 记录";
};
const title = (record: Record<string, unknown>) =>
  textField(
    record,
    "title",
    "display_name",
    "organization_name",
    "full_name",
    "lead_name",
    "party_name",
    "name",
  ) ?? "未命名记录";
const summary = (record: Record<string, unknown>) =>
  textField(record, "summary_zh", "chinese_summary", "summary", "description");
const originalText = (record: Record<string, unknown>) =>
  textField(record, "original_text", "source_text");
const language = (record: Record<string, unknown>) =>
  textField(record, "original_language") ?? "und";
const businessStatus = (record: Record<string, unknown>) =>
  textField(record, "business_status", "status");
const reviewStatus = (record: Record<string, unknown>) =>
  textField(record, "review_status");
const recordKey = (record: Record<string, unknown>, index: number) =>
  `${identifier(record)}-${index}`;
const detailLink = (record: Record<string, unknown>) => {
  const party = textField(record, "party_profile", "party");
  if (party) {
    return `/gbos/party/${encodeURIComponent(party)}`;
  }
  const project = textField(record, "sample_project", "project");
  if (project) {
    return `/gbos/sample/${encodeURIComponent(project)}`;
  }
  return undefined;
};
</script>
