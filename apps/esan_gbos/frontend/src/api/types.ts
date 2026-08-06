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
