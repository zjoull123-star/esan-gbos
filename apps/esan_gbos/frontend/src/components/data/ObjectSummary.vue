<template>
  <article class="object-summary">
    <header class="object-summary__header">
      <p v-if="eyebrow" class="object-summary__eyebrow">
        {{ eyebrow }}
      </p>
      <component :is="headingTag">
        {{ title }}
      </component>
    </header>

    <dl v-if="visibleFields.length" class="object-summary__fields">
      <div v-for="field in visibleFields" :key="field.key">
        <dt>{{ field.label }}</dt>
        <dd>
          <RouterLink v-if="field.to" class="object-summary__link" :to="field.to">
            {{ field.value }}
          </RouterLink>
          <template v-else>
            {{ field.value }}
          </template>
        </dd>
      </div>
    </dl>
  </article>
</template>

<script lang="ts">
export interface ObjectSummaryField {
  key: string;
  label: string;
  value?: string | number | null;
  to?: string;
}
</script>

<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    title: string;
    eyebrow?: string;
    fields: readonly ObjectSummaryField[];
    headingLevel?: 2 | 3;
  }>(),
  {
    eyebrow: undefined,
    headingLevel: 2,
  },
);

const headingTag = computed<"h2" | "h3">(() =>
  props.headingLevel === 3 ? "h3" : "h2",
);
const visibleFields = computed(() =>
  props.fields.filter(
    (field) =>
      field.value !== undefined &&
      field.value !== null &&
      (typeof field.value !== "string" || field.value.trim().length > 0),
  ),
);
</script>

<style scoped>
.object-summary {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.object-summary__eyebrow,
.object-summary h2,
.object-summary h3 {
  margin: 0;
}

.object-summary__eyebrow {
  color: var(--gbos-accent-text);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.06em;
  overflow-wrap: anywhere;
  text-transform: uppercase;
}

.object-summary h2,
.object-summary h3 {
  margin-top: 3px;
  font-size: 17px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.object-summary__fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 8px;
  margin: 12px 0 0;
}

.object-summary__fields > div {
  min-width: 0;
  padding: 9px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.object-summary dt,
.object-summary dd {
  margin: 0;
}

.object-summary dt {
  color: var(--gbos-muted);
  font-size: 11px;
  font-weight: 700;
}

.object-summary dd {
  margin-top: 3px;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.object-summary__link {
  color: var(--gbos-accent-text);
  font-weight: 750;
}
</style>
