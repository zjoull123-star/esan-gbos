<template>
  <header class="page-header">
    <div class="page-header__copy">
      <p v-if="eyebrow" class="page-header__eyebrow">
        {{ eyebrow }}
      </p>
      <h1>{{ title }}</h1>
      <p v-if="description" class="page-header__description">
        {{ description }}
      </p>
    </div>
    <div v-if="$slots.actions" class="page-header__actions" data-region="actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{
  title: string;
  eyebrow?: string;
  description?: string;
}>();
</script>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
}

.page-header__copy {
  min-width: 0;
}

.page-header__eyebrow,
.page-header__description,
.page-header h1 {
  margin: 0;
}

.page-header__eyebrow {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.page-header h1 {
  margin-top: 4px;
  color: var(--gbos-text);
  font-size: clamp(24px, 3vw, 32px);
  line-height: 1.2;
}

.page-header__description {
  max-width: 72ch;
  margin-top: 6px;
  color: var(--gbos-muted);
  font-size: 14px;
  line-height: 1.55;
}

.page-header__actions {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 767px) {
  .page-header {
    flex-direction: column;
  }

  .page-header__actions {
    width: 100%;
    justify-content: flex-start;
  }
}
</style>
