<template>
  <section class="panel" aria-labelledby="business-link-title">
    <h2 id="business-link-title">
      业务关联
    </h2>
    <p>输入已有业务对象的受控引用（PTY-、CNT-、CRM-LEAD- 或 CRM-DEAL-）。团队权限由服务端根据当前会话判定。</p>
    <form autocomplete="off" @submit.prevent="submit">
      <label>业务对象引用<input v-model.trim="businessRef" name="business_ref" maxlength="140" required></label>
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
const emit = defineEmits<{ link: [businessRef: string] }>();
const businessRef = ref("");
const submit = () => {
  if (!businessRef.value) return;
  emit("link", businessRef.value);
  businessRef.value = "";
};
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.panel form, .panel label { display: grid; gap: 8px; }
.panel form { gap: 12px; }
.panel input { min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-control); }
</style>
