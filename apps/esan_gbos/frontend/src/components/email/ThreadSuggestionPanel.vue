<template>
  <section class="panel" aria-labelledby="thread-title">
    <h2 id="thread-title">
      会话与重复建议
    </h2>
    <p v-if="!suggestion">
      当前没有待人工判断的合并建议。
    </p>
    <template v-else>
      <p>{{ suggestion.safe_label }}</p>
      <div class="actions">
        <GbosButton type="button" :disabled="pending" @click="$emit('accept')">
          接受建议
        </GbosButton>
        <GbosButton intent="secondary" type="button" :disabled="pending" @click="$emit('reject')">
          拒绝建议
        </GbosButton>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import GbosButton from "@/components/ui/GbosButton.vue";
defineProps<{ suggestion?: { safe_label: string }; pending?: boolean }>();
defineEmits<{ accept: []; reject: [] }>();
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
