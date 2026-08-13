<template>
  <section class="panel" aria-labelledby="assignment-title">
    <h2 id="assignment-title">
      分配与 SLA
    </h2>
    <dl>
      <div><dt>当前业务负责人</dt><dd>{{ detail.assignee_label || "未分配" }}</dd></div>
      <div><dt>团队</dt><dd>{{ detail.team_label || "待确定" }}</dd></div>
      <div><dt>SLA 状态</dt><dd>当前接口未提供 SLA 字段</dd></div>
    </dl>
    <GbosButton v-if="detail.state === 'unassigned'" data-claim-inbox type="button" :disabled="pending" @click="$emit('claim')">
      认领
    </GbosButton>
    <p v-if="detail.state === 'closed'" class="seam-message">
      当前公开接口尚未提供重新打开操作；可由有权限的主管重新分配后继续处理。
    </p>
    <form data-reassign-form autocomplete="off" @submit.prevent="reassign">
      <label for="email-assignee-ref">负责人引用（留空可取消分配）</label>
      <input id="email-assignee-ref" v-model.trim="assigneeRef" data-assignee-ref maxlength="140">
      <GbosButton type="submit" :disabled="pending">
        重新分配
      </GbosButton>
    </form>
    <form v-if="allowedTransitions.length" class="transition-form" @submit.prevent="transition">
      <label for="email-target-state">处理状态</label>
      <select id="email-target-state" v-model="targetState">
        <option v-for="state in allowedTransitions" :key="state" :value="state">
          {{ stateLabel[state] }}
        </option>
      </select>
      <GbosButton type="submit" :disabled="pending">
        更新状态
      </GbosButton>
    </form>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watchEffect } from "vue";
import type { EmailInboxDetail, EmailInboxState } from "@/api/email-gateway-types";
import GbosButton from "@/components/ui/GbosButton.vue";
const props = defineProps<{ detail: EmailInboxDetail; pending?: boolean }>();
const emit = defineEmits<{ claim: []; reassign: [assigneeUserRef?: string]; transition: [state: EmailInboxState] }>();
const assigneeRef = ref("");
const transitionMap: Partial<Record<EmailInboxState, readonly EmailInboxState[]>> = {
  assigned: ["draft", "waiting_internal", "converted", "closed"],
  draft: ["assigned", "waiting_internal", "converted", "closed"],
};
const stateLabel: Partial<Record<EmailInboxState, string>> = {
  assigned: "处理中", draft: "草稿处理中", waiting_internal: "等待内部",
  converted: "已转化", closed: "已关闭",
};
const allowedTransitions = computed(() => transitionMap[props.detail.state] ?? []);
const targetState = ref<EmailInboxState>("draft");
watchEffect(() => {
  if (!allowedTransitions.value.includes(targetState.value)) {
    targetState.value = allowedTransitions.value[0] ?? "draft";
  }
});
const reassign = () => {
  emit("reassign", assigneeRef.value || undefined);
  assigneeRef.value = "";
};
const transition = () => {
  if (allowedTransitions.value.includes(targetState.value)) emit("transition", targetState.value);
};
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel dl { display: grid; gap: 8px; }
.panel dl div { display: flex; justify-content: space-between; gap: 12px; }
.panel dt { color: var(--gbos-muted); }
.panel dd { margin: 0; text-align: right; }
.seam-message { color: var(--gbos-muted); }
.panel form { display: grid; gap: 8px; margin-top: 12px; }
.panel input, .panel select { min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-control); }
</style>
