<template>
  <section class="timeline" :aria-labelledby="titleId">
    <h2 :id="titleId">
      {{ title }}
    </h2>
    <p v-if="entries.length === 0" class="timeline__empty">
      暂无记录
    </p>
    <ol v-else>
      <li v-for="entry in entries" :key="entry.id">
        <article>
          <header>
            <h3>
              <RouterLink v-if="entry.to" class="timeline__link" :to="entry.to">
                {{ entry.title }}
              </RouterLink>
              <template v-else>
                {{ entry.title }}
              </template>
            </h3>
            <time v-if="entry.timestamp" :datetime="entry.timestamp">
              {{ entry.timestamp }}
            </time>
          </header>
          <dl v-if="visibleFields(entry).length">
            <div v-for="field in visibleFields(entry)" :key="field.key">
              <dt>{{ field.label }}</dt>
              <dd>{{ field.value }}</dd>
            </div>
          </dl>
        </article>
      </li>
    </ol>
  </section>
</template>

<script lang="ts">
import type { ObjectSummaryField } from "@/components/data/ObjectSummary.vue";

export interface TimelineEntry {
  id: string;
  title: string;
  timestamp?: string;
  to?: string;
  fields: readonly ObjectSummaryField[];
}
</script>

<script setup lang="ts">
import { useId } from "vue";

defineProps<{
  title: string;
  entries: readonly TimelineEntry[];
}>();

const titleId = `${useId()}-timeline-title`;
const visibleFields = (entry: TimelineEntry) =>
  entry.fields.filter(
    (field) =>
      field.value !== undefined &&
      field.value !== null &&
      (typeof field.value !== "string" || field.value.trim().length > 0),
  );
</script>

<style scoped>
.timeline {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.timeline > h2,
.timeline__empty,
.timeline ol,
.timeline h3,
.timeline dl,
.timeline dt,
.timeline dd {
  margin: 0;
}

.timeline > h2 {
  font-size: 17px;
}

.timeline__empty {
  margin-top: 10px;
  color: var(--gbos-muted);
  font-size: 13px;
}

.timeline ol {
  display: grid;
  gap: 10px;
  margin-top: 12px;
  padding: 0;
  list-style: none;
}

.timeline li {
  position: relative;
  min-width: 0;
  padding-inline-start: 18px;
}

.timeline li::before {
  position: absolute;
  top: 7px;
  inset-inline-start: 0;
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--gbos-accent);
  content: "";
}

.timeline li + li {
  padding-top: 10px;
  border-top: 1px solid var(--gbos-border);
}

.timeline article,
.timeline article > header {
  min-width: 0;
}

.timeline article > header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.timeline h3 {
  font-size: 14px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.timeline time {
  flex: 0 0 auto;
  color: var(--gbos-muted);
  font-size: 11px;
}

.timeline dl {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
  margin-top: 8px;
}

.timeline dl > div {
  min-width: 0;
}

.timeline dt {
  color: var(--gbos-muted);
  font-size: 11px;
  font-weight: 700;
}

.timeline dd {
  margin-top: 2px;
  font-size: 13px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.timeline__link {
  color: var(--gbos-accent-text);
}

@media (max-width: 520px) {
  .timeline article > header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
