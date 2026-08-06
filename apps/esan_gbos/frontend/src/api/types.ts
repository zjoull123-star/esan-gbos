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

export type ContractErrorCode =
  | "authentication_required"
  | "permission_denied"
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
