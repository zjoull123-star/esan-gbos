<template>
  <section class="panel" aria-labelledby="thread-title">
    <h2 id="thread-title">
      会话与重复建议
    </h2>
    <p>当前接口未提供会话建议，也没有拒绝建议操作；本面板保持不可用。</p>
  </section>
</template>

<script setup lang="ts">
defineProps<{ suggestion?: { safe_label: string }; pending?: boolean }>();
</script>

<style scoped>
.panel { min-width: 0; overflow-wrap: anywhere; padding: 16px; border: 1px solid var(--gbos-border); border-radius: var(--gbos-radius-card); background: var(--gbos-surface); }
.panel h2 { margin-top: 0; font-size: 18px; }
</style>
