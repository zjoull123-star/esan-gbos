<template>
  <article
    class="metric-card metric-tile"
    :class="`metric-card--${metric.status}`"
    :data-metric-key="metric.metric_key"
  >
    <header class="metric-card__header">
      <div>
        <p class="metric-card__eyebrow">
          {{ metric.metric_key }}
        </p>
        <h2>{{ metric.display_name }}</h2>
      </div>
      <span class="metric-status" :class="`metric-status--${metric.status}`">
        {{ metric.status === "available" ? "可用" : "不可用" }}
      </span>
    </header>

    <p
      v-if="metric.status === 'available'"
      class="metric-value metric-tile__value"
      data-official-value
    >
      <strong>{{ formatNumber(metric.value) }}</strong>
      <span>{{ metric.unit }}</span>
    </p>
    <div v-else class="metric-unavailable metric-tile__unavailable" role="note">
      <strong>不显示正式数值</strong>
      <span>原因：{{ unavailableReason(metric.unavailable_reason) }}</span>
      <code>{{ metric.unavailable_reason }}</code>
    </div>

    <dl class="metric-tile__quality" aria-label="指标质量状态">
      <div>
        <dt>新鲜度</dt>
        <dd>
          {{ freshnessLabel(metric.freshness.status) }} ·
          {{ formatDuration(metric.freshness.age_seconds) }} /
          SLO {{ formatDuration(metric.freshness.slo_seconds) }}
        </dd>
      </div>
      <div>
        <dt>覆盖率</dt>
        <dd>
          {{ coverageLabel(metric.coverage.status) }} ·
          {{ formatPercent(metric.coverage.ratio) }}
          ({{ metric.coverage.included_count }}/{{ metric.coverage.total_count }})
        </dd>
      </div>
      <div>
        <dt>对账</dt>
        <dd>
          {{ reconciliationLabel(metric.reconciliation.status) }} ·
          差异 {{ formatNumber(metric.reconciliation.variance) }}
        </dd>
      </div>
    </dl>

    <details class="metric-lineage metric-tile__details">
      <summary>查看定义与来源链路</summary>
      <dl class="metric-facts">
        <div>
          <dt>统计窗口</dt>
          <dd>{{ formatWindow(metric.window) }}</dd>
        </div>
        <div>
          <dt>数据截至</dt>
          <dd><time :datetime="metric.as_of">{{ formatTimestamp(metric.as_of) }}</time></dd>
        </div>
        <div>
          <dt>查询时间</dt>
          <dd><time :datetime="metric.queried_at">{{ formatTimestamp(metric.queried_at) }}</time></dd>
        </div>
        <div>
          <dt>定义版本</dt>
          <dd>{{ metric.definition_version }}</dd>
        </div>
        <div>
          <dt>站点</dt>
          <dd>{{ metric.site_id }}</dd>
        </div>
        <div>
          <dt>来源模式</dt>
          <dd>{{ metric.source_mode === "synthetic" ? "演示 / 合成" : "正式 / 实时" }}</dd>
        </div>
        <div>
          <dt>对账引用</dt>
          <dd>{{ metric.reconciliation.reference }}</dd>
        </div>
        <div>
          <dt>对账时间</dt>
          <dd>
            <time :datetime="metric.reconciliation.checked_at">
              {{ formatTimestamp(metric.reconciliation.checked_at) }}
            </time>
          </dd>
        </div>
      </dl>
      <ol aria-label="完整来源链路">
        <li
          v-for="source in metric.source_lineage"
          :key="`${source.source_system}:${source.source_record_refs.join(':')}`"
        >
          <strong>{{ source.source_system }}</strong>
          <span>{{ source.evidence_status }} · {{ source.transformation_version }}</span>
          <span>
            获取于
            <time :datetime="source.retrieved_at">{{ formatTimestamp(source.retrieved_at) }}</time>
          </span>
          <code>{{ source.source_record_refs.join(" · ") }}</code>
        </li>
      </ol>
    </details>
  </article>
</template>

<script setup lang="ts">
import type { DeepReadonly } from "vue";

import type {
  GovernedMetric,
  MetricUnavailableReason,
  MetricWindow,
} from "@/api/types";

defineProps<{ metric: DeepReadonly<GovernedMetric> }>();

const numberFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 });
const percentFormatter = new Intl.NumberFormat("zh-CN", {
  style: "percent",
  maximumFractionDigits: 1,
});
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

const formatNumber = (value: number) => numberFormatter.format(value);
const formatPercent = (value: number) => percentFormatter.format(value);
const formatTimestamp = (value: string) => timestampFormatter.format(new Date(value));
const formatDuration = (seconds: number) => {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`;
  return `${Math.round(seconds / 86400)} 天`;
};

const grainLabels = {
  hour: "小时",
  day: "日",
  week: "周",
  month: "月",
  quarter: "季度",
  year: "年",
  instant: "时点",
} as const;
const formatWindow = (window: MetricWindow) => {
  if (window.type === "point_in_time") {
    return `时点 · ${formatTimestamp(window.start)}`;
  }
  const type = window.type === "calendar" ? "日历" : "滚动";
  return `${type}${grainLabels[window.grain]} · ${formatTimestamp(window.start)} 至 ${formatTimestamp(window.end)}`;
};

const freshnessLabel = (status: "fresh" | "stale" | "unknown") =>
  ({ fresh: "新鲜", stale: "已过期", unknown: "未知" })[status];
const coverageLabel = (status: "sufficient" | "insufficient" | "unknown") =>
  ({ sufficient: "充足", insufficient: "不足", unknown: "未知" })[status];
const reconciliationLabel = (status: "passed" | "failed" | "not_run") =>
  ({ passed: "已通过", failed: "未通过", not_run: "未执行" })[status];
const unavailableReason = (reason: MetricUnavailableReason) =>
  ({
    stale: "数据已过期",
    insufficient_coverage: "覆盖不足",
    reconciliation_failed: "对账未通过",
    source_unavailable: "数据来源不可用",
    definition_unavailable: "指标定义不可用",
    ungoverned_source: "来源未治理",
  })[reason];
</script>

<style scoped>
.metric-card {
  height: 100%;
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-top: 4px solid var(--gbos-accent);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.metric-card--unavailable {
  border-top-color: rgb(190 24 93 / 72%);
}

.metric-card__header {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.metric-card__header > div {
  min-width: 0;
}

.metric-card__eyebrow {
  margin: 0;
  color: var(--gbos-accent-text);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
  overflow-wrap: anywhere;
  text-transform: uppercase;
}

.metric-card h2 {
  margin: 4px 0 0;
  color: var(--gbos-text);
  font-size: 18px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.metric-status {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-accent-text);
  background: rgb(15 159 143 / 12%);
  font-size: 12px;
  font-weight: 800;
}

.metric-status--unavailable {
  color: rgb(159 18 57);
  background: rgb(190 24 93 / 10%);
}

.metric-value {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin: 14px 0;
}

.metric-value strong {
  color: var(--gbos-text);
  font-family: var(--gbos-font-sans);
  font-size: clamp(30px, 5vw, 46px);
  font-variant-numeric: tabular-nums;
  line-height: 1;
  overflow-wrap: anywhere;
}

.metric-value span {
  color: var(--gbos-muted);
  font-weight: 800;
}

.metric-unavailable {
  display: grid;
  gap: 6px;
  margin: 14px 0;
  padding: 12px;
  border: 1px solid rgb(190 24 93 / 38%);
  border-radius: var(--gbos-radius-control);
  color: rgb(159 18 57);
  background: rgb(190 24 93 / 7%);
}

.metric-unavailable span,
.metric-unavailable code {
  overflow-wrap: anywhere;
}

.metric-unavailable code {
  font-size: 12px;
}

.metric-tile__value,
.metric-tile__unavailable {
  margin: 14px 0;
}

.metric-tile__value strong {
  font-size: clamp(30px, 5vw, 46px);
}

.metric-tile__quality {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin: 0;
}

.metric-tile__quality > div {
  min-width: 0;
  padding: 8px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.metric-tile__quality dt {
  color: var(--gbos-muted);
  font-size: 11px;
  font-weight: 750;
}

.metric-tile__quality dd {
  margin: 3px 0 0;
  font-size: 12px;
  font-weight: 650;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.metric-tile__details .metric-facts {
  margin-top: 12px;
}

.metric-facts {
  display: grid;
  min-width: 0;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}

.metric-facts > div {
  min-width: 0;
  padding: 9px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.metric-facts dt {
  color: var(--gbos-muted);
  font-size: 11px;
  font-weight: 750;
}

.metric-facts dd {
  margin: 3px 0 0;
  color: var(--gbos-text);
  font-size: 12px;
  font-weight: 650;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.metric-lineage {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--gbos-border);
}

.metric-lineage summary {
  cursor: pointer;
  color: var(--gbos-accent-text);
  font-size: 13px;
  font-weight: 800;
}

.metric-lineage ol {
  display: grid;
  gap: 8px;
  margin: 10px 0 0;
  padding-inline-start: 20px;
}

.metric-lineage li {
  min-width: 0;
  padding: 9px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.metric-lineage li > * {
  display: block;
  overflow-wrap: anywhere;
}

.metric-lineage li span {
  margin-top: 3px;
  color: var(--gbos-muted);
  font-size: 12px;
}

.metric-lineage code {
  margin-top: 5px;
  color: var(--gbos-text);
  font-size: 11px;
}

@media (max-width: 520px) {
  .metric-tile__quality,
  .metric-facts {
    grid-template-columns: 1fr;
  }
}
</style>
