<template>
  <div class="queue-tabs" role="tablist" aria-label="邮件处理队列" @keydown="moveFocus">
    <button
      v-for="queue in queues"
      :key="queue.value"
      type="button"
      role="tab"
      :aria-selected="modelValue === queue.value"
      :tabindex="modelValue === queue.value ? 0 : -1"
      @click="$emit('update:modelValue', queue.value)"
    >
      {{ queue.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
import type { EmailInboxQueue } from "@/api/email-gateway-types";

defineProps<{ modelValue: EmailInboxQueue }>();
const emit = defineEmits<{ "update:modelValue": [value: EmailInboxQueue] }>();

const queues: readonly { value: EmailInboxQueue; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "identity_pending", label: "身份待确认" },
  { value: "unassigned", label: "待分配" },
  { value: "first_reply_due", label: "首次回复将到期" },
  { value: "draft", label: "草稿" },
  { value: "send_failure_uncertain", label: "发送失败或不确定" },
  { value: "waiting_customer", label: "等待客户" },
  { value: "waiting_internal", label: "等待内部" },
  { value: "converted", label: "已转化" },
  { value: "closed", label: "已关闭" },
  { value: "quarantine", label: "隔离区" },
];

const moveFocus = (event: KeyboardEvent) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const host = event.currentTarget as HTMLElement;
  const tabs = [...host.querySelectorAll<HTMLButtonElement>("[role='tab']")];
  const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
  if (current < 0) return;
  event.preventDefault();
  const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
    (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  tabs[next]?.focus();
  const queue = queues[next];
  if (queue) emit("update:modelValue", queue.value);
};
</script>

<style scoped>
.queue-tabs { display: flex; flex-wrap: wrap; max-width: 100%; gap: 8px; padding: 2px; }
.queue-tabs button { flex: 0 0 auto; min-height: 40px; padding: 8px 12px; border: 1px solid var(--gbos-border); border-radius: 999px; background: var(--gbos-surface); color: var(--gbos-text); }
.queue-tabs button[aria-selected="true"] { border-color: var(--gbos-accent); box-shadow: inset 0 0 0 1px var(--gbos-accent); font-weight: 700; }
</style>
