export interface SuccessMeta {
  request_id: string;
  schema_version: "1.0";
  replayed?: boolean;
  original_request_id?: string;
  next_cursor?: string | null;
  page_size?: number;
}

export interface SuccessEnvelope<T> {
  data: T;
  meta: SuccessMeta;
}

export interface V4SuccessMeta {
  request_id: string;
  schema_version: "4.0";
  replayed?: boolean;
  original_request_id?: string;
  next_cursor?: string | null;
  page_size?: number;
}

export interface V4SuccessEnvelope<T> {
  data: T;
  meta: V4SuccessMeta;
}

export type ContractErrorCode =
  | "authentication_required"
  | "permission_denied"
  | "csrf_failed"
  | "method_not_allowed"
  | "invalid_dto"
  | "invalid_query"
  | "invalid_cursor"
  | "not_found"
  | "scope_mismatch"
  | "revision_conflict"
  | "invalid_transition"
  | "idempotency_conflict"
  | "request_in_progress"
  | "validation_error"
  | "internal_error";

export interface ContractError {
  code: ContractErrorCode;
  message: string;
  request_id: string;
  details: Record<string, unknown>;
}

export interface WorkItemFilters {
  team?: string;
  business_status?: string;
  assigned_to?: string;
  priority?: string;
  due_date?: string;
}

export interface WorkItemListQuery {
  filters?: WorkItemFilters;
  cursor?: string;
  pageSize?: number;
}

export interface SampleCreateCommand {
  team: string;
  title: string;
  expected_revision: 0;
  idempotency_key: string;
  party_profile?: string;
  product_brief?: string;
  deal?: string;
  origin?: "Manual" | "Fixture" | "Integration" | "AI";
}

export interface SampleFeedbackCommand {
  project: string;
  summary: string;
  expected_revision: number;
  idempotency_key: string;
  rating?: number;
  received_on?: string;
}

export interface SourcingCreateCommand {
  demand: string;
  expected_revision: number;
  idempotency_key: string;
}

export interface WorkItemTransitionCommand {
  name: string;
  to_status: "In Progress" | "Blocked" | "Done" | "Cancelled";
  expected_revision: number;
  idempotency_key: string;
  reason?: string;
}

export interface ReviewEvidenceRef {
  evidence_type: string;
  reference: string;
  revision?: number;
  payload_hash?: string;
}

export interface ReviewSubjectSnapshot {
  doctype: string;
  name: string;
  revision: number;
  payload_hash: string;
  snapshot: Record<string, unknown>;
}

export interface ReviewCaseSummary {
  name: string;
  title: string;
  team?: string;
  assigned_reviewer: string;
  review_status: "Pending" | "Approved" | "Rejected" | "Superseded";
  case_revision: number;
  case_payload_hash: string;
  subject: ReviewSubjectSnapshot;
  evidence: ReviewEvidenceRef[];
  policy_reference: string;
  origin?: "Manual" | "Fixture" | "Integration" | "AI";
  decided_at?: string | null;
  decision_note?: string | null;
}

export interface ReviewDecision {
  name: string;
  review_case?: string;
  decision: "Approved" | "Rejected";
  reviewer?: string;
  reason?: string;
  request_id?: string;
  decided_at?: string;
}

export interface ReviewCaseDetailPayload {
  case: ReviewCaseSummary;
  decision?: ReviewDecision | null;
}

export interface ReviewCaseListPayload {
  cases: ReviewCaseSummary[];
  total: number;
  page_size?: number;
  next_cursor?: string | null;
}

export interface ReviewCaseListQuery {
  cursor?: string;
  pageSize?: number;
}

export interface ReviewDecisionCommand {
  name: string;
  decision: "Approved" | "Rejected";
  decision_note: string;
  expected_revision: number;
  expected_subject_revision: number;
  idempotency_key: string;
  subject_payload_sha256: string;
  evidence_refs: string[];
  policy_version: string;
  expected_case_payload_hash?: string;
}

export type MetricSourceMode = "synthetic" | "live";
export type MetricStatus = "available" | "unavailable";
export type MetricUnavailableReason =
  | "stale"
  | "insufficient_coverage"
  | "reconciliation_failed"
  | "source_unavailable"
  | "definition_unavailable"
  | "ungoverned_source";

export interface MetricWindow {
  type: "rolling" | "calendar" | "point_in_time";
  grain: "hour" | "day" | "week" | "month" | "quarter" | "year" | "instant";
  start: string;
  end: string;
}

export interface MetricFreshness {
  status: "fresh" | "stale" | "unknown";
  age_seconds: number;
  slo_seconds: number;
}

export interface MetricCoverage {
  status: "sufficient" | "insufficient" | "unknown";
  ratio: number;
  included_count: number;
  total_count: number;
}

export interface MetricReconciliation {
  status: "passed" | "failed" | "not_run";
  checked_at: string;
  reference: string;
  variance: number;
}

export interface MetricSourceLineage {
  source_system: string;
  source_record_refs: string[];
  retrieved_at: string;
  transformation_version: string;
  evidence_status: "synthetic" | "unverified" | "verified" | "partial";
}

interface MetricBase {
  schema_version: "3.0";
  metric_key: string;
  display_name: string;
  definition_version: string;
  site_id: string;
  as_of: string;
  queried_at: string;
  window: MetricWindow;
  freshness: MetricFreshness;
  coverage: MetricCoverage;
  reconciliation: MetricReconciliation;
  source_lineage: MetricSourceLineage[];
  source_mode: MetricSourceMode;
  synthetic: boolean;
  governed_sources: boolean;
}

export interface AvailableMetric extends MetricBase {
  status: "available";
  value: number;
  unit: string;
  unavailable_reason?: never;
}

export interface UnavailableMetric extends MetricBase {
  status: "unavailable";
  value?: never;
  unit?: never;
  unavailable_reason: MetricUnavailableReason;
}

export type GovernedMetric = AvailableMetric | UnavailableMetric;

export interface MetricDashboardPayload {
  schema_version: "3.0";
  site_id: string;
  source_mode: MetricSourceMode;
  synthetic: boolean;
  generated_at: string;
  metrics: GovernedMetric[];
}

export type ConnectorState = "enabled" | "paused" | "error" | "disabled";
export type FreshnessState = "fresh" | "stale" | "unknown";

export interface ConnectorStatus {
  instance_id: string;
  channel: string;
  status: ConnectorState;
  checkpoint_version: number;
  backlog: number;
  last_success_at: string | null;
  safe_error_code: string | null;
  freshness: FreshnessState;
  revision: number;
}

export interface ConnectorListPayload {
  connectors: ConnectorStatus[];
}

export interface ConnectorCommand {
  instance_id: string;
  expected_revision: number;
  idempotency_key: string;
}

export interface CommunicationSummary {
  observation_id: string;
  channel: string;
  occurred_at: string;
  summary_zh: string;
  original_language: string;
  classification: string;
  review_status: string;
  team_ref: string | null;
  party_ref: string | null;
  evidence_count: number;
}

export interface EvidenceLocator {
  ref: string;
  locator: string;
}

export interface FactProposal {
  status: string;
  confidence: number;
  type: string;
  value_display: string;
}

export interface AssociationSuggestion {
  type: string;
  target_ref: string;
  confidence: number;
}

export interface V4ModelMetadata {
  name: "deepseek-v4-flash";
  version: string;
}

export interface CommunicationDetail extends CommunicationSummary {
  evidence: EvidenceLocator[];
  fact_proposals: FactProposal[];
  association_suggestions: AssociationSuggestion[];
  model: V4ModelMetadata;
  raw_access_allowed: boolean;
  original_text?: string;
}

export interface CommunicationListPayload {
  communications: CommunicationSummary[];
  next_cursor: string | null;
}

export interface CommunicationDetailPayload {
  communication: CommunicationDetail;
}

export interface CommunicationListQuery {
  channel?: string;
  classification?: string;
  reviewStatus?: string;
  cursor?: string;
  pageSize?: number;
}

export interface UsageCost {
  currency: "USD";
  amount: number | null;
  state: "known" | "unknown";
}

export interface ModelUsage {
  model: "deepseek-v4-flash";
  period: string;
  tokens: number;
  cost: UsageCost;
  soft_limit: number;
  hard_limit: number;
  state: "normal" | "soft_limit" | "hard_limit" | "unknown";
}

export type AiDraftKind =
  | "Work Item"
  | "Review Case"
  | "CEO Informal Observation";

export interface AiDraft {
  draft_id: string;
  kind: AiDraftKind;
  status: "AI Draft" | "Pending";
  origin: "AI";
  subject: string;
  evidence: EvidenceLocator[];
  model: V4ModelMetadata;
  revision: number;
}

export interface AiDraftListPayload {
  drafts: AiDraft[];
  next_cursor: string | null;
}

export interface AiDraftDetailPayload {
  draft: AiDraft;
}

export interface AiDraftListQuery {
  status?: "AI Draft" | "Pending";
  cursor?: string;
  pageSize?: number;
}

export interface AiDraftSubmitCommand {
  draft_id: string;
  expected_revision: number;
  idempotency_key: string;
}
