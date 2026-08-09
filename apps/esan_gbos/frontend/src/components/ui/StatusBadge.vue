<template>
  <span class="status-badge" :class="`status-badge--${tone}`" :data-tone="tone">
    {{ label }}
  </span>
</template>

<script setup lang="ts">
defineProps<{
  tone: "neutral" | "info" | "success" | "warning" | "danger";
  label: string;
}>();
</script>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  line-height: 1.3;
  white-space: nowrap;
}

.status-badge--neutral {
  color: var(--gbos-text);
  border-color: var(--gbos-border);
  background: var(--gbos-canvas);
}

.status-badge--info {
  color: #174ea6;
  background: #e8f0fe;
}

.status-badge--success {
  color: var(--gbos-accent-text);
  background: #e8f7f4;
}

.status-badge--warning {
  color: #8a5a00;
  background: #fff4d6;
}

.status-badge--danger {
  color: #a61b1b;
  background: #fde8e8;
}
</style>
