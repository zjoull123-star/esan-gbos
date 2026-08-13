<template>
  <section class="panel" aria-labelledby="business-link-title">
    <h2 id="business-link-title">
      业务关联
    </h2>
    <p>客户 Party / Contact 与商机链接须在当前团队权限内人工确认。</p>
    <form autocomplete="off" @submit.prevent="submit">
      <label>业务对象引用<input v-model.trim="businessRef" name="business_ref" maxlength="140" required></label>
      <label>授权团队引用<input v-model.trim="teamRef" name="authority_team_ref" maxlength="140" required></label>
      <GbosButton type="submit" :disabled="pending">
        保存业务关联
      </GbosButton>
    </form>
  </section>
</template>

<script setup lang="ts">
import { ref } from "vue";
import GbosButton from "@/components/ui/GbosButton.vue";
defineProps<{ pending?: boolean }>();
const emit = defineEmits<{ link: [value: { businessRef: string; teamRef: string }] }>();
const businessRef = ref("");
const teamRef = ref("");
const submit = () => {
  if (!businessRef.value || !teamRef.value) return;
  emit("link", { businessRef: businessRef.value, teamRef: teamRef.value });
};
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel form, .panel label { display: grid; gap: 8px; }
.panel form { gap: 12px; }
.panel input { min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-control); }
</style>
