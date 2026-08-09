<template>
  <div class="metrics-cockpit">
    <div
      class="metrics-source-banner"
      :class="{ 'metrics-source-banner--synthetic': dashboard.synthetic }"
      role="status"
      aria-live="polite"
    >
      <strong>{{ dashboard.synthetic ? "演示 / 合成数据" : "受治理正式数据" }}</strong>
      <span>
        {{
          dashboard.synthetic
            ? "仅用于功能演示，不代表真实经营结果。"
            : "指标已通过服务端治理质量门。"
        }}
      </span>
      <span class="metrics-source-banner__meta">
        {{ dashboard.site_id }} · 生成于
        <time :datetime="dashboard.generated_at">{{ formatTimestamp(dashboard.generated_at) }}</time>
      </span>
    </div>

    <ul class="metric-grid" aria-label="受治理经营指标">
      <li v-for="metric in dashboard.metrics" :key="metric.metric_key">
        <MetricTile :metric="metric" />
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type { MetricDashboardPayload } from "@/api/types";
import MetricTile from "@/components/data/MetricTile.vue";

defineProps<{ dashboard: DeepReadonly<MetricDashboardPayload> }>();

const timestampFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  timeZoneName: "short",
});
const formatTimestamp = (value: string) => timestampFormatter.format(new Date(value));
</script>

<style scoped>
.metrics-cockpit {
  min-width: 0;
}

.metrics-source-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  margin-bottom: 10px;
  padding: 10px 12px;
  border: 1px solid var(--gbos-accent);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-accent-text);
  background: rgb(15 159 143 / 10%);
}

.metrics-source-banner--synthetic {
  border-color: rgb(180 120 20 / 55%);
  color: rgb(120 74 8);
  background: rgb(245 177 54 / 16%);
}

.metrics-source-banner strong {
  flex: 0 0 auto;
  padding: 3px 7px;
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-surface);
  background: var(--gbos-accent-text);
  font-size: 12px;
  letter-spacing: 0.04em;
}

.metrics-source-banner--synthetic strong {
  background: rgb(120 74 8);
}

.metrics-source-banner__meta {
  width: 100%;
  color: inherit;
  font-size: 12px;
  overflow-wrap: anywhere;
}

.metric-grid {
  display: grid;
  min-width: 0;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.metric-grid > li {
  min-width: 0;
}

@media (max-width: 600px) {
  .metrics-source-banner {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
