<template>
  <form class="review-decision-form" @submit.prevent>
    <div class="review-decision-form__heading">
      <p>CASE DECISION ONLY</p>
      <h2>记录人工决定</h2>
    </div>
    <p class="review-decision-form__boundary">
      审核只会决定当前案件，不会直接修改客户、样品、报价、订单或任何外部系统。
    </p>
    <label :for="textareaId">审核说明</label>
    <textarea
      :id="textareaId"
      v-model="decisionNote"
      rows="5"
      maxlength="1000"
      required
      :aria-describedby="`${helpId} ${countId}`"
      :disabled="submitting || submitLocked"
      placeholder="说明批准或拒绝所依据的证据。"
    />
    <div class="review-decision-form__guidance">
      <p :id="helpId">
        请说明决定依据，至少 4 个字符；该说明会随案件决定保存。
      </p>
      <p :id="countId" aria-live="polite">
        {{ decisionNote.length }} / 1000
      </p>
    </div>
    <div class="review-decision-form__actions">
      <GbosButton
        intent="primary"
        type="button"
        data-decision="Approved"
        :loading="submitting || submitLocked"
        :disabled="!canSubmit"
        @click="submit('Approved')"
      >
        批准案件
      </GbosButton>
      <GbosButton
        intent="danger"
        type="button"
        data-decision="Rejected"
        :loading="submitting || submitLocked"
        :disabled="!canSubmit"
        @click="submit('Rejected')"
      >
        拒绝案件
      </GbosButton>
    </div>
  </form>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";

import GbosButton from "@/components/ui/GbosButton.vue";

const props = defineProps<{
  submitting: boolean;
  resetKey: number;
}>();
const emit = defineEmits<{
  decide: [decision: "Approved" | "Rejected", note: string];
}>();

const instanceId = useId();
const textareaId = `${instanceId}-review-decision-note`;
const helpId = `${instanceId}-review-decision-help`;
const countId = `${instanceId}-review-decision-count`;
const decisionNote = ref("");
const submitLocked = ref(false);
const normalizedNote = computed(() => decisionNote.value.trim());
const canSubmit = computed(
  () =>
    !props.submitting &&
    !submitLocked.value &&
    normalizedNote.value.length >= 4,
);

watch(
  () => props.resetKey,
  () => {
    decisionNote.value = "";
    submitLocked.value = false;
  },
);

watch(
  () => props.submitting,
  (submitting, wasSubmitting) => {
    if (wasSubmitting && !submitting) {
      submitLocked.value = false;
    }
  },
);

const submit = (decision: "Approved" | "Rejected") => {
  if (!canSubmit.value) {
    return;
  }
  submitLocked.value = true;
  emit("decide", decision, normalizedNote.value);
};
</script>

<style scoped>
.review-decision-form {
  display: grid;
  min-width: 0;
  gap: 12px;
  margin: 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.review-decision-form__heading p,
.review-decision-form__heading h2,
.review-decision-form__boundary,
.review-decision-form__guidance p {
  margin: 0;
}

.review-decision-form__heading p {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.review-decision-form__heading h2 {
  margin-top: 3px;
  color: var(--gbos-text);
  font-size: 19px;
  line-height: 1.35;
}

.review-decision-form__boundary {
  padding: 10px 12px;
  border: 1px solid var(--gbos-accent);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-canvas);
  font-size: 13px;
  line-height: 1.55;
}

.review-decision-form label {
  color: var(--gbos-text);
  font-size: 13px;
  font-weight: 700;
}

.review-decision-form textarea {
  width: 100%;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font: inherit;
  line-height: 1.55;
  resize: vertical;
}

.review-decision-form__guidance {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  color: var(--gbos-muted);
  font-size: 12px;
  line-height: 1.5;
}

.review-decision-form__guidance p:last-child {
  flex: 0 0 auto;
}

.review-decision-form__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

@media (max-width: 767px) {
  .review-decision-form__guidance {
    flex-direction: column;
    gap: 4px;
  }

  .review-decision-form__actions {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
