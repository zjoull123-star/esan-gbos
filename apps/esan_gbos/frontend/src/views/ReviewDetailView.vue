<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          ESAN GBOS · Gate 4
        </p>
        <h1>审核案件</h1>
        <p>核验证据与冻结快照后记录人工决定；系统不会在本页修改业务主体。</p>
      </div>
      <RouterLink class="button button--secondary" to="/gbos/review">
        返回队列
      </RouterLink>
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
    <template v-else-if="reviewCase">
      <DemoBanner v-if="reviewCase.origin === 'Fixture'" />
      <p v-if="decisionMessage" class="notice notice--success" role="status">
        {{ decisionMessage }}
      </p>
      <p v-if="decisionError" class="notice notice--error" role="alert">
        {{ decisionError }}
      </p>

      <div class="review-layout">
        <article class="evidence-card">
          <p class="eyebrow">
            {{ statusLabel }}
          </p>
          <h2>{{ reviewCase.title }}</h2>
          <dl class="status-list">
            <div>
              <dt>案件编号</dt>
              <dd>{{ reviewCase.name }}</dd>
            </div>
            <div>
              <dt>主体</dt>
              <dd>{{ reviewCase.subject.doctype }} · {{ reviewCase.subject.name }}</dd>
            </div>
            <div>
              <dt>案件 / 主体版本</dt>
              <dd>{{ reviewCase.case_revision }} / {{ reviewCase.subject.revision }}</dd>
            </div>
            <div>
              <dt>策略</dt>
              <dd>{{ reviewCase.policy_reference }}</dd>
            </div>
          </dl>
          <section>
            <h3>冻结主体摘要</h3>
            <p>
              {{ snapshotTitle }}
            </p>
            <p>{{ snapshotSummary }}</p>
          </section>
          <section>
            <h3>证据引用</h3>
            <ul class="evidence-ref-list">
              <li v-for="evidence in reviewCase.evidence" :key="evidence.reference">
                <strong>{{ evidence.reference }}</strong>
                <span>{{ evidence.evidence_type }}</span>
              </li>
            </ul>
          </section>
        </article>

        <ReviewDecisionForm
          v-if="reviewCase.review_status === 'Pending'"
          :submitting="submitting"
          :reset-key="formResetKey"
          @decide="decide"
        />
        <article v-else class="command-card">
          <h2>案件已结束</h2>
          <p>{{ reviewCase.decision_note || "该案件已有人工决定。" }}</p>
        </article>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { ReviewCaseDetailPayload } from "@/api/types";
import DemoBanner from "@/components/DemoBanner.vue";
import ReviewDecisionForm from "@/components/ReviewDecisionForm.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const submitting = ref(false);
const formResetKey = ref(0);
const decisionError = ref("");
const decisionMessage = ref("");
const decidedPayload = ref<ReviewCaseDetailPayload>();

const resource = useOnlineResource(async () => {
  decidedPayload.value = undefined;
  const response = await client.getReviewCase(props.id);
  return response.data;
});
const reviewCase = computed(
  () => decidedPayload.value?.case ?? resource.data.value?.case,
);
const statusLabel = computed(() => {
  const labels = {
    Pending: "待审核",
    Approved: "已批准",
    Rejected: "已拒绝",
    Superseded: "已取代",
  } as const;
  return reviewCase.value ? labels[reviewCase.value.review_status] : "";
});
const snapshotTitle = computed(() => {
  const value = reviewCase.value?.subject.snapshot.title;
  return typeof value === "string" ? value : reviewCase.value?.subject.name ?? "";
});
const snapshotSummary = computed(() => {
  const value = reviewCase.value?.subject.snapshot.summary_zh;
  return typeof value === "string" ? value : "未提供可展示的中文摘要。";
});
const { state, message, requestId, load } = resource;

watch(
  () => props.id,
  () => {
    decisionError.value = "";
    decisionMessage.value = "";
    formResetKey.value += 1;
    void load();
  },
);

const decide = async (decision: "Approved" | "Rejected", note: string) => {
  const current = reviewCase.value;
  if (!current || submitting.value) {
    return;
  }
  decisionError.value = "";
  decisionMessage.value = "";
  submitting.value = true;
  try {
    const response = await client.decideReviewCase({
      name: current.name,
      decision,
      decision_note: note,
      expected_revision: current.case_revision,
      expected_subject_revision: current.subject.revision,
      idempotency_key: createIdempotencyKey(),
      subject_payload_sha256: current.subject.payload_hash,
      evidence_refs: current.evidence.map((evidence) => evidence.reference),
      policy_version: current.policy_reference,
      expected_case_payload_hash: current.case_payload_hash,
    });
    decidedPayload.value = response.data;
    decisionMessage.value = "审核决定已记录。";
    formResetKey.value += 1;
  } catch (error) {
    if (error instanceof BffError) {
      decisionError.value = error.displayMessage;
      if (error.status === 409 || error.code === "revision_conflict") {
        formResetKey.value += 1;
        await load();
      }
    } else {
      decisionError.value = "暂时无法提交审核决定，请稍后重试。";
    }
  } finally {
    submitting.value = false;
  }
};
</script>
