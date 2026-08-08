<template>
  <section class="view">
    <DetailCommandTemplate>
      <template #header>
        <PageHeader
          :eyebrow="`样品项目 · ${id}`"
          title="样品状态"
          description="项目、迭代、寄样与反馈均以在线 BFF 最新 revision 为准。"
        >
          <template #actions>
            <GbosButton intent="secondary" @click="load">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template v-if="project" #facts>
        <ObjectSummary
          :title="text(project, 'title') || text(project, 'name') || '样品项目'"
          :eyebrow="text(project, 'name')"
          :fields="projectFields"
        />
      </template>

      <template #main>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="!project"
          @retry="load"
        >
          <div class="sample-timelines">
            <DemoBanner v-if="hasFixtureData" />
            <Timeline title="样品迭代" :entries="iterationEntries" />
            <Timeline title="寄样记录" :entries="shipmentEntries" />
            <Timeline title="客户反馈" :entries="feedbackEntries" />
          </div>
        </ResourceBoundary>
      </template>

      <template v-if="state === 'ready' && project" #command>
        <form
          v-if="canRecordFeedback"
          class="sample-feedback"
          @submit.prevent="submitFeedback"
        >
          <div>
            <p class="sample-feedback__eyebrow">
              受控命令
            </p>
            <h2>记录客户样品反馈</h2>
            <p>提交使用当前项目 revision、一次性幂等键和 Frappe CSRF。</p>
          </div>
          <label for="feedback-summary">中文反馈摘要</label>
          <textarea
            id="feedback-summary"
            v-model.trim="feedbackSummary"
            name="summary"
            rows="5"
            maxlength="4000"
            required
            :disabled="submitting"
          />
          <p v-if="commandMessage" class="sample-feedback__success" role="status">
            {{ commandMessage }}
          </p>
          <p v-if="commandError" class="sample-feedback__error" role="alert">
            {{ commandError }}
          </p>
          <GbosButton
            type="submit"
            :loading="submitting"
            :disabled="!feedbackSummary || submitting"
          >
            提交反馈
          </GbosButton>
        </form>
        <aside v-else class="sample-feedback sample-feedback--readonly" role="note">
          <div>
            <p class="sample-feedback__eyebrow">
              反馈暂不可用
            </p>
            <h2>{{ feedbackUnavailableTitle }}</h2>
            <p>{{ feedbackUnavailableMessage }}</p>
          </div>
        </aside>
      </template>
    </DetailCommandTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import DemoBanner from "@/components/DemoBanner.vue";
import ObjectSummary, {
  type ObjectSummaryField,
} from "@/components/data/ObjectSummary.vue";
import Timeline, { type TimelineEntry } from "@/components/data/Timeline.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { isFixturePayload } from "@/presentation";
import { sessionState } from "@/session";

type DataRecord = Record<string, unknown>;

const props = defineProps<{ id: string }>();
const client = useBffClient();
const isRecord = (value: unknown): value is DataRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (record: DataRecord, key: string) => {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};
const number = (record: DataRecord, key: string) => {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
};
const recordAt = (record: DataRecord | undefined, key: string) => {
  const value = record?.[key];
  return isRecord(value) ? value : undefined;
};
const recordsAt = (record: DataRecord | undefined, key: string) => {
  const value = record?.[key];
  return Array.isArray(value) ? value.filter(isRecord) : [];
};
const field = (
  key: string,
  label: string,
  value: string | number | undefined,
  to?: string,
): ObjectSummaryField => ({ key, label, value, to });

const resource = useOnlineResource(async () => {
  const response = await client.getSampleStatus(props.id);
  return isRecord(response.data) ? response.data : undefined;
});
const payload = computed(() => resource.data.value);
const project = computed(() => recordAt(payload.value, "project"));
const iterations = computed(() => recordsAt(payload.value, "iterations"));
const shipments = computed(() => recordsAt(payload.value, "shipments"));
const feedback = computed(() => recordsAt(payload.value, "feedback"));
const currentRevision = computed(() =>
  project.value ? number(project.value, "revision") : undefined,
);
const sampleStatus = computed(() =>
  project.value ? text(project.value, "business_status") : undefined,
);
const hasFixtureData = computed(() => isFixturePayload(payload.value));
const projectFields = computed<ObjectSummaryField[]>(() => {
  const current = project.value;
  if (!current) return [];
  const party = text(current, "party_profile");
  return [
    field("team", "团队", text(current, "team")),
    field(
      "party_profile",
      "客户档案",
      party,
      party ? `/gbos/party/${encodeURIComponent(party)}` : undefined,
    ),
    field("product_brief", "产品简报", text(current, "product_brief")),
    field("origin", "来源", text(current, "origin")),
    field("business_status", "业务状态", sampleStatus.value),
    field("review_status", "审核状态", text(current, "review_status")),
    field("revision", "版本", currentRevision.value),
    field("modified", "更新时间", text(current, "modified")),
  ];
});
const iterationEntries = computed<TimelineEntry[]>(() =>
  iterations.value.map((item, index) => ({
    id: text(item, "name") || `iteration-${index}`,
    title:
      number(item, "iteration_number") !== undefined
        ? `第 ${number(item, "iteration_number")} 轮`
        : text(item, "name") || "样品迭代",
    fields: [
      field("summary", "摘要", text(item, "summary")),
      field("origin", "来源", text(item, "origin")),
      field("business_status", "业务状态", text(item, "business_status")),
      field("review_status", "审核状态", text(item, "review_status")),
      field("revision", "版本", number(item, "revision")),
    ],
  })),
);
const shipmentEntries = computed<TimelineEntry[]>(() =>
  shipments.value.map((item, index) => ({
    id: text(item, "name") || `shipment-${index}`,
    title: text(item, "carrier") || text(item, "name") || "寄样记录",
    timestamp: text(item, "shipped_on"),
    fields: [
      field("tracking_number", "运单号", text(item, "tracking_number")),
      field("delivered_on", "送达日期", text(item, "delivered_on")),
      field("origin", "来源", text(item, "origin")),
      field("business_status", "业务状态", text(item, "business_status")),
      field("revision", "版本", number(item, "revision")),
    ],
  })),
);
const feedbackEntries = computed<TimelineEntry[]>(() =>
  feedback.value.map((item, index) => ({
    id: text(item, "name") || `feedback-${index}`,
    title: text(item, "name") || "客户反馈",
    timestamp: text(item, "received_on"),
    fields: [
      field("summary", "反馈摘要", text(item, "summary")),
      field("rating", "评分", number(item, "rating")),
      field("origin", "来源", text(item, "origin")),
      field("review_status", "审核状态", text(item, "review_status")),
      field("revision", "版本", number(item, "revision")),
    ],
  })),
);

const feedbackRoleAllowed = computed(() =>
  sessionState.roles.some((role) =>
    ["GBOS Admin", "Sales Manager", "Sales User", "Product/R&D"].includes(role),
  ),
);
const canRecordFeedback = computed(
  () =>
    sampleStatus.value === "Sent" &&
    currentRevision.value !== undefined &&
    feedbackRoleAllowed.value,
);
const feedbackUnavailableTitle = computed(() =>
  feedbackRoleAllowed.value ? "先完成寄样" : "只读访问",
);
const feedbackUnavailableMessage = computed(() =>
  feedbackRoleAllowed.value
    ? `当前状态为 ${sampleStatus.value ?? "未知"}。请完成寄样并将项目推进到 Sent 后才能记录客户反馈。`
    : "当前角色不能记录客户反馈；请由获授权的销售或产品人员操作。",
);
const feedbackSummary = ref("");
const submitting = ref(false);
const commandMessage = ref("");
const commandError = ref("");
let commandGeneration = 0;

const submitFeedback = async () => {
  const revision = currentRevision.value;
  if (!canRecordFeedback.value || revision === undefined || submitting.value) {
    return;
  }
  const currentGeneration = ++commandGeneration;
  commandMessage.value = "";
  commandError.value = "";
  submitting.value = true;
  try {
    await client.recordSampleFeedback({
      project: props.id,
      summary: feedbackSummary.value,
      expected_revision: revision,
      idempotency_key: createIdempotencyKey(),
    });
    if (currentGeneration !== commandGeneration) return;
    feedbackSummary.value = "";
    commandMessage.value = "反馈已记录。请刷新以读取最新样品状态。";
  } catch (error) {
    if (currentGeneration !== commandGeneration) return;
    commandError.value =
      error instanceof BffError ? error.displayMessage : "提交失败，请刷新后重试。";
    if (
      error instanceof BffError &&
      (error.code === "revision_conflict" || error.code === "idempotency_conflict")
    ) {
      await load();
    }
  } finally {
    if (currentGeneration === commandGeneration) submitting.value = false;
  }
};

const { state, message, requestId, load } = resource;
watch(
  () => props.id,
  () => {
    commandGeneration += 1;
    feedbackSummary.value = "";
    commandMessage.value = "";
    commandError.value = "";
    submitting.value = false;
    void load();
  },
);
</script>

<style scoped>
.sample-timelines {
  display: grid;
  min-width: 0;
  gap: 12px;
}

.sample-feedback {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.sample-feedback__eyebrow,
.sample-feedback h2,
.sample-feedback p {
  margin: 0;
}

.sample-feedback__eyebrow {
  color: var(--gbos-accent-text);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.sample-feedback h2 {
  margin-top: 3px;
  font-size: 18px;
}

.sample-feedback p,
.sample-feedback label {
  color: var(--gbos-muted);
  font-size: 13px;
  line-height: 1.5;
}

.sample-feedback label {
  font-weight: 700;
}

.sample-feedback textarea {
  width: 100%;
  min-height: 112px;
  resize: vertical;
  padding: 10px 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font-family: var(--gbos-font-sans);
  font-size: 14px;
  line-height: 1.5;
}

.sample-feedback__success,
.sample-feedback__error {
  padding: 10px;
  border-radius: var(--gbos-radius-control);
}

.sample-feedback__success {
  color: var(--gbos-accent-text) !important;
  background: rgb(15 159 143 / 10%);
}

.sample-feedback__error {
  color: rgb(159 18 57) !important;
  background: rgb(190 24 93 / 8%);
}

.sample-feedback--readonly {
  background: var(--gbos-canvas);
}

@media (max-width: 767px) {
  .sample-feedback textarea {
    min-height: 132px;
  }
}
</style>
