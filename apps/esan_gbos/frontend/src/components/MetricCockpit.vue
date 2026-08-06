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
        <time :datetime="dashboard.generated_at">{{
          formatTimestamp(dashboard.generated_at)
        }}</time>
      </span>
    </div>

    <ul class="metric-grid" aria-label="受治理经营指标">
      <li v-for="metric in dashboard.metrics" :key="metric.metric_key">
        <article
          class="metric-card"
          :class="`metric-card--${metric.status}`"
          :data-metric-key="metric.metric_key"
        >
          <header class="metric-card__header">
            <div>
              <p class="eyebrow">
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
            class="metric-value"
            data-official-value
          >
            <strong>{{ formatNumber(metric.value) }}</strong>
            <span>{{ metric.unit }}</span>
          </p>
          <div v-else class="metric-unavailable" role="note">
            <strong>不显示正式数值</strong>
            <span>
              原因：{{ unavailableReason(metric.unavailable_reason) }}
              <code>{{ metric.unavailable_reason }}</code>
            </span>
          </div>

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
              <dd>
                <time :datetime="metric.queried_at">{{
                  formatTimestamp(metric.queried_at)
                }}</time>
              </dd>
            </div>
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
                {{ metric.reconciliation.reference }}
                · 差异 {{ formatNumber(metric.reconciliation.variance) }}
              </dd>
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
          </dl>

          <details class="metric-lineage">
            <summary>查看来源链路（{{ metric.source_lineage.length }}）</summary>
            <ol>
              <li
                v-for="source in metric.source_lineage"
                :key="`${source.source_system}:${source.source_record_refs.join(':')}`"
              >
                <strong>{{ source.source_system }}</strong>
                <span>
                  {{ source.evidence_status }} · {{ source.transformation_version }}
                </span>
                <span>
                  获取于
                  <time :datetime="source.retrieved_at">{{
                    formatTimestamp(source.retrieved_at)
                  }}</time>
                </span>
                <code>{{ source.source_record_refs.join(" · ") }}</code>
              </li>
            </ol>
          </details>
        </article>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import type {
  MetricDashboardPayload,
  MetricUnavailableReason,
  MetricWindow,
} from "@/api/types";

defineProps<{ dashboard: MetricDashboardPayload }>();

const numberFormatter = new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 4,
});
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
  const range = ` · ${formatTimestamp(window.start)} 至 ${formatTimestamp(window.end)}`;
  return `${type}${grainLabels[window.grain]}${range}`;
};

const formatDuration = (seconds: number) => {
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`;
  return `${Math.round(seconds / 86400)} 天`;
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
