<template>
  <section class="panel" aria-labelledby="assignment-title">
    <h2 id="assignment-title">
      分配与 SLA
    </h2>
    <dl>
      <div><dt>当前业务负责人</dt><dd>{{ detail.assignee_label || "未分配" }}</dd></div>
      <div><dt>团队</dt><dd>{{ detail.team_label || "待确定" }}</dd></div>
      <div><dt>SLA 状态</dt><dd><span aria-hidden="true">{{ slaSymbol }}</span> {{ slaLabel }}</dd></div>
    </dl>
    <GbosButton v-if="detail.state === 'unassigned'" data-claim-inbox type="button" :disabled="pending" @click="$emit('claim')">
      认领
    </GbosButton>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { EmailInboxDetail } from "@/api/email-gateway-types";
import GbosButton from "@/components/ui/GbosButton.vue";
const props = defineProps<{ detail: EmailInboxDetail; pending?: boolean }>();
defineEmits<{ claim: [] }>();
const urgent = computed(() => ["identity_pending", "unassigned", "send_uncertain"].includes(props.detail.state));
const slaLabel = computed(() => urgent.value ? "需要尽快处理" : "按当前队列处理");
const slaSymbol = computed(() => urgent.value ? "!" : "✓");
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel dl { display: grid; gap: 8px; }
.panel dl div { display: flex; justify-content: space-between; gap: 12px; }
.panel dt { color: var(--gbos-muted); }
.panel dd { margin: 0; text-align: right; }
</style>
