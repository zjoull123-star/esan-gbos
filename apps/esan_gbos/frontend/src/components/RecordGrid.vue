<template>
  <div class="record-region">
    <DemoBanner v-if="hasFixtureData" />
    <ul class="record-grid">
      <template v-for="(record, index) in records" :key="recordKey(record, index)">
        <li v-if="originalText(record)">
          <EvidenceCard
            :title="title(record)"
            :summary-zh="summary(record)"
            :original-text="originalText(record) ?? ''"
            :original-language="language(record)"
          />
        </li>
        <li v-else>
          <article class="record-card">
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
              <div v-if="candidateStatus(record)">
                <dt>候选状态</dt>
                <dd>{{ candidateStatus(record) }}</dd>
              </div>
              <div v-if="candidatePrice(record)">
                <dt>报价</dt>
                <dd>{{ candidatePrice(record) }}</dd>
              </div>
              <div v-if="candidateLeadTime(record)">
                <dt>预计交期</dt>
                <dd>{{ candidateLeadTime(record) }}</dd>
              </div>
            </dl>
            <a v-if="detailLink(record)" class="text-link" :href="detailLink(record)">
              查看详情
            </a>
          </article>
        </li>
      </template>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

import DemoBanner from "./DemoBanner.vue";
import EvidenceCard from "./EvidenceCard.vue";
import { isFixturePayload, numberField, textField } from "@/presentation";

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
    "supplier_name",
    "name",
  ) ?? "未命名记录";
const summary = (record: Record<string, unknown>) =>
  textField(record, "summary_zh", "chinese_summary", "summary", "description", "notes");
const originalText = (record: Record<string, unknown>) =>
  textField(record, "original_text", "source_text");
const language = (record: Record<string, unknown>) =>
  textField(record, "original_language") ?? "und";
const businessStatus = (record: Record<string, unknown>) =>
  textField(record, "business_status", "status");
const reviewStatus = (record: Record<string, unknown>) =>
  textField(record, "review_status");
const candidateStatus = (record: Record<string, unknown>) =>
  textField(record, "candidate_status");
const candidatePrice = (record: Record<string, unknown>) => {
  const value = numberField(record, "quoted_price");
  if (value === undefined) {
    return undefined;
  }
  const currency = textField(record, "currency");
  return currency ? `${value} ${currency}` : String(value);
};
const candidateLeadTime = (record: Record<string, unknown>) => {
  const days = numberField(record, "lead_time_days");
  return days === undefined ? undefined : `${days} 天`;
};
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
  const referenceDoctype = textField(record, "reference_doctype");
  const referenceName = textField(record, "reference_name");
  if (referenceDoctype === "GBOS Party Profile" && referenceName) {
    return `/gbos/party/${encodeURIComponent(referenceName)}`;
  }
  if (referenceDoctype === "GBOS Sample Project" && referenceName) {
    return `/gbos/sample/${encodeURIComponent(referenceName)}`;
  }
  const section = textField(record, "presentation_section");
  const name = textField(record, "name");
  if (section === "客户档案" && name) {
    return `/gbos/party/${encodeURIComponent(name)}`;
  }
  if (section === "样品项目" && name) {
    return `/gbos/sample/${encodeURIComponent(name)}`;
  }
  return undefined;
};
</script>
