import type {
  GovernedMetric,
  MetricCoverage,
  MetricDashboardPayload,
  MetricFreshness,
  MetricReconciliation,
  MetricSourceLineage,
  MetricSourceMode,
  MetricWindow,
} from "./types";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const hasExactKeys = (
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
) => {
  const allowed = new Set([...required, ...optional]);
  return (
    required.every((key) => key in value) &&
    Object.keys(value).every((key) => allowed.has(key))
  );
};

const isNonEmptyString = (value: unknown, maxLength = 256): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= maxLength;

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

const isNonNegativeInteger = (value: unknown): value is number =>
  Number.isInteger(value) && Number(value) >= 0;

const isTimestamp = (value: unknown): value is string =>
  typeof value === "string" &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/u.test(
    value,
  ) &&
  Number.isFinite(Date.parse(value));

const oneOf = <T extends string>(
  value: unknown,
  allowed: readonly T[],
): value is T => typeof value === "string" && allowed.includes(value as T);

const SOURCE_MODES = ["synthetic", "live"] as const;
const WINDOW_TYPES = ["rolling", "calendar", "point_in_time"] as const;
const WINDOW_GRAINS = [
  "hour",
  "day",
  "week",
  "month",
  "quarter",
  "year",
  "instant",
] as const;
const UNAVAILABLE_REASONS = [
  "stale",
  "insufficient_coverage",
  "reconciliation_failed",
  "source_unavailable",
  "definition_unavailable",
  "ungoverned_source",
] as const;

const isWindow = (value: unknown): value is MetricWindow => {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["type", "grain", "start", "end"]) ||
    !oneOf(value.type, WINDOW_TYPES) ||
    !oneOf(value.grain, WINDOW_GRAINS) ||
    !isTimestamp(value.start) ||
    !isTimestamp(value.end)
  ) {
    return false;
  }
  if (value.type === "point_in_time") {
    return (
      value.grain === "instant" &&
      Date.parse(value.start) === Date.parse(value.end)
    );
  }
  if (value.grain === "instant") {
    return false;
  }
  return Date.parse(value.start) < Date.parse(value.end);
};

const isFreshness = (value: unknown): value is MetricFreshness =>
  isRecord(value) &&
  hasExactKeys(value, ["status", "age_seconds", "slo_seconds"]) &&
  oneOf(value.status, ["fresh", "stale", "unknown"]) &&
  isNonNegativeInteger(value.age_seconds) &&
  Number.isInteger(value.slo_seconds) &&
  Number(value.slo_seconds) > 0;

const isCoverage = (value: unknown): value is MetricCoverage =>
  isRecord(value) &&
  hasExactKeys(value, [
    "status",
    "ratio",
    "included_count",
    "total_count",
  ]) &&
  oneOf(value.status, ["sufficient", "insufficient", "unknown"]) &&
  isFiniteNumber(value.ratio) &&
  value.ratio >= 0 &&
  value.ratio <= 1 &&
  isNonNegativeInteger(value.included_count) &&
  isNonNegativeInteger(value.total_count) &&
  value.included_count <= value.total_count;

const isReconciliation = (value: unknown): value is MetricReconciliation =>
  isRecord(value) &&
  hasExactKeys(value, ["status", "checked_at", "reference", "variance"]) &&
  oneOf(value.status, ["passed", "failed", "not_run"]) &&
  isTimestamp(value.checked_at) &&
  isNonEmptyString(value.reference) &&
  isFiniteNumber(value.variance);

const isLineage = (
  value: unknown,
  sourceMode: MetricSourceMode,
): value is MetricSourceLineage => {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "source_system",
      "source_record_refs",
      "retrieved_at",
      "transformation_version",
      "evidence_status",
    ]) ||
    !isNonEmptyString(value.source_system, 80) ||
    !Array.isArray(value.source_record_refs) ||
    value.source_record_refs.length === 0 ||
    !value.source_record_refs.every(
      (reference) =>
        isNonEmptyString(reference, 256) &&
        /^[A-Za-z0-9][A-Za-z0-9._:-]*$/u.test(reference),
    ) ||
    new Set(value.source_record_refs).size !== value.source_record_refs.length ||
    !isTimestamp(value.retrieved_at) ||
    !isNonEmptyString(value.transformation_version, 80) ||
    !oneOf(value.evidence_status, [
      "synthetic",
      "unverified",
      "verified",
      "partial",
    ])
  ) {
    return false;
  }
  return sourceMode === "synthetic"
    ? value.evidence_status === "synthetic"
    : value.evidence_status !== "synthetic";
};

const isMetric = (
  value: unknown,
  dashboardMode: MetricSourceMode,
  dashboardSite: string,
): value is GovernedMetric => {
  if (!isRecord(value)) {
    return false;
  }
  const status = value.status;
  const common = [
    "schema_version",
    "metric_key",
    "display_name",
    "definition_version",
    "site_id",
    "status",
    "as_of",
    "queried_at",
    "window",
    "freshness",
    "coverage",
    "reconciliation",
    "source_lineage",
    "source_mode",
    "synthetic",
    "governed_sources",
  ];
  const required =
    status === "available"
      ? [...common, "value", "unit"]
      : status === "unavailable"
        ? [...common, "unavailable_reason"]
        : common;
  if (
    !hasExactKeys(value, required) ||
    !oneOf(value.source_mode, SOURCE_MODES)
  ) {
    return false;
  }
  const sourceMode = value.source_mode;
  if (
    value.schema_version !== "3.0" ||
    !isNonEmptyString(value.metric_key, 80) ||
    !/^[a-z][a-z0-9_.]{2,79}$/u.test(value.metric_key) ||
    !isNonEmptyString(value.display_name, 160) ||
    !isNonEmptyString(value.definition_version, 80) ||
    !isNonEmptyString(value.site_id, 140) ||
    !/^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$/u.test(value.site_id) ||
    value.site_id !== dashboardSite ||
    sourceMode !== dashboardMode ||
    typeof value.synthetic !== "boolean" ||
    value.synthetic !== (sourceMode === "synthetic") ||
    typeof value.governed_sources !== "boolean" ||
    !isTimestamp(value.as_of) ||
    !isTimestamp(value.queried_at) ||
    !isWindow(value.window) ||
    !isFreshness(value.freshness) ||
    !isCoverage(value.coverage) ||
    !isReconciliation(value.reconciliation) ||
    !Array.isArray(value.source_lineage) ||
    value.source_lineage.length === 0 ||
    !value.source_lineage.every((entry) => isLineage(entry, sourceMode))
  ) {
    return false;
  }
  if (status === "available") {
    return (
      isFiniteNumber(value.value) &&
      isNonEmptyString(value.unit, 80) &&
      value.freshness.status === "fresh" &&
      value.coverage.status === "sufficient" &&
      value.reconciliation.status === "passed" &&
      value.governed_sources
    );
  }
  return (
    status === "unavailable" &&
    oneOf(value.unavailable_reason, UNAVAILABLE_REASONS) &&
    !("value" in value) &&
    !("unit" in value)
  );
};

export const parseMetricDashboard = (
  value: unknown,
): MetricDashboardPayload | undefined => {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "site_id",
      "source_mode",
      "synthetic",
      "generated_at",
      "metrics",
    ]) ||
    value.schema_version !== "3.0" ||
    !isNonEmptyString(value.site_id, 140) ||
    !/^[A-Za-z0-9][A-Za-z0-9.-]{0,139}$/u.test(value.site_id) ||
    !isTimestamp(value.generated_at) ||
    !oneOf(value.source_mode, SOURCE_MODES)
  ) {
    return undefined;
  }
  const sourceMode = value.source_mode;
  if (
    typeof value.synthetic !== "boolean" ||
    value.synthetic !== (sourceMode === "synthetic") ||
    !Array.isArray(value.metrics) ||
    value.metrics.length > 20 ||
    !value.metrics.every((metric) =>
      isMetric(metric, sourceMode, value.site_id as string),
    )
  ) {
    return undefined;
  }
  const metricKeys = value.metrics.map((metric) =>
    isRecord(metric) ? metric.metric_key : undefined,
  );
  if (new Set(metricKeys).size !== metricKeys.length) {
    return undefined;
  }
  return value as unknown as MetricDashboardPayload;
};
