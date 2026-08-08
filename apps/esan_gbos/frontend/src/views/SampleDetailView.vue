<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          样品项目 · {{ id }}
        </p>
        <h1>样品状态</h1>
        <p>状态与反馈均以在线 BFF 最新 revision 为准。</p>
      </div>
      <button class="button button--secondary" type="button" @click="load">
        刷新
      </button>
    </header>
    <StatePanel v-if="state === 'loading' || state === 'idle'" kind="loading" />
    <StatePanel
      v-else-if="state === 'offline'"
      kind="offline"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'permission'"
      kind="permission"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'error'"
      kind="error"
      :message="message"
      :request-id="requestId"
      @retry="load"
    />
    <template v-else>
      <StatePanel v-if="records.length === 0" kind="empty" @retry="load" />
      <RecordGrid v-else :records="records" />

      <form
        v-if="canRecordFeedback"
        class="command-card"
        @submit.prevent="submitFeedback"
      >
        <div>
          <p class="eyebrow">
            受控命令
          </p>
          <h2>记录客户样品反馈</h2>
          <p>提交时会带当前 revision、一次性幂等键和 Frappe CSRF。</p>
        </div>
        <label for="feedback-summary">中文反馈摘要</label>
        <textarea
          id="feedback-summary"
          v-model.trim="feedbackSummary"
          name="summary"
          rows="4"
          maxlength="4000"
          required
        />
        <p v-if="commandMessage" class="form-message" role="status">
          {{ commandMessage }}
        </p>
        <Button
          type="submit"
          theme="blue"
          variant="solid"
          :loading="submitting"
          :disabled="!feedbackSummary || submitting"
        >
          提交反馈
        </Button>
      </form>
      <aside v-else class="command-card" role="note">
        <div>
          <p class="eyebrow">
            反馈暂不可用
          </p>
          <h2>{{ feedbackUnavailableTitle }}</h2>
          <p>{{ feedbackUnavailableMessage }}</p>
        </div>
      </aside>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Button } from "frappe-ui";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import RecordGrid from "@/components/RecordGrid.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { flattenSampleStatusPayload } from "@/presentation";
import { sessionState } from "@/session";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getSampleStatus(props.id);
  return response.data;
});
const presentation = computed(() =>
  flattenSampleStatusPayload(resource.data.value),
);
const records = computed(() => presentation.value.records);
const currentRevision = computed(() => presentation.value.revision ?? 0);
const sampleStatus = computed(() => presentation.value.businessStatus);
const feedbackRoleAllowed = computed(() =>
  sessionState.roles.some((role) =>
    ["GBOS Admin", "Sales Manager", "Sales User", "Product/R&D"].includes(role),
  ),
);
const canRecordFeedback = computed(
  () => sampleStatus.value === "Sent" && feedbackRoleAllowed.value,
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
let commandGeneration = 0;

const submitFeedback = async () => {
  if (!canRecordFeedback.value) {
    return;
  }
  const currentGeneration = ++commandGeneration;
  commandMessage.value = "";
  submitting.value = true;
  try {
    await client.recordSampleFeedback({
      project: props.id,
      summary: feedbackSummary.value,
      expected_revision: currentRevision.value,
      idempotency_key: createIdempotencyKey(),
    });
    if (currentGeneration !== commandGeneration) {
      return;
    }
    feedbackSummary.value = "";
    commandMessage.value = "反馈已记录。请刷新以读取最新样品状态。";
  } catch (error) {
    if (currentGeneration !== commandGeneration) {
      return;
    }
    commandMessage.value =
      error instanceof BffError ? error.displayMessage : "提交失败，请刷新后重试。";
  } finally {
    if (currentGeneration === commandGeneration) {
      submitting.value = false;
    }
  }
};

const { state, message, requestId, load } = resource;
watch(
  () => props.id,
  () => {
    commandGeneration += 1;
    feedbackSummary.value = "";
    commandMessage.value = "";
    submitting.value = false;
    void load();
  },
);
</script>
