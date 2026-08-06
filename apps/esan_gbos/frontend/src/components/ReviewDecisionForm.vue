<template>
  <form class="command-card" @submit.prevent>
    <h2>记录人工决定</h2>
    <p>
      审核只会决定当前案件，不会直接修改客户、样品、报价、订单或任何外部系统。
    </p>
    <label for="review-decision-note">审核说明</label>
    <textarea
      id="review-decision-note"
      v-model.trim="decisionNote"
      rows="5"
      maxlength="1000"
      required
      :disabled="submitting"
      placeholder="说明批准或拒绝所依据的证据，至少 4 个字符。"
    />
    <div class="review-actions">
      <button
        class="button button--primary"
        type="button"
        data-decision="Approved"
        :disabled="!canSubmit"
        @click="submit('Approved')"
      >
        批准案件
      </button>
      <button
        class="button button--danger"
        type="button"
        data-decision="Rejected"
        :disabled="!canSubmit"
        @click="submit('Rejected')"
      >
        拒绝案件
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = defineProps<{
  submitting: boolean;
  resetKey: number;
}>();
const emit = defineEmits<{
  decide: [decision: "Approved" | "Rejected", note: string];
}>();

const decisionNote = ref("");
const canSubmit = computed(() => !props.submitting && decisionNote.value.length >= 4);

watch(
  () => props.resetKey,
  () => {
    decisionNote.value = "";
  },
);

const submit = (decision: "Approved" | "Rejected") => {
  if (canSubmit.value) {
    emit("decide", decision, decisionNote.value);
  }
};
</script>
