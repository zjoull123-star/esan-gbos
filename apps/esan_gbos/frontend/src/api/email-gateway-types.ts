export type EmailProviderKind = "fake" | "imap_smtp" | "wecom_app_mail";
export type EmailBusinessMode = "primary" | "selective_archive" | "migration";
export type EmailBusinessPurpose =
  | "business_operations"
  | "observation_processing"
  | "entity_resolution"
  | "customer_service"
  | "sales_follow_up"
  | "procurement_coordination"
  | "product_sample_management"
  | "risk_review"
  | "metric_reporting"
  | "audit_compliance";
export type EmailMailboxStatus = "draft" | "active" | "paused" | "revoked" | "error";
export type EmailMailboxAction = "enable" | "pause" | "revoke";
export type EmailInboxState =
  | "identity_pending"
  | "unassigned"
  | "assigned"
  | "draft"
  | "waiting_internal"
  | "waiting_customer"
  | "converted"
  | "closed"
  | "quarantined"
  | "send_queued"
  | "send_uncertain";
export type EmailInboxSort = "received_at_desc" | "sla_due_at_asc";
export type EmailInboxQueue =
  | "all"
  | "identity_pending"
  | "unassigned"
  | "first_reply_due"
  | "draft"
  | "send_failure_uncertain"
  | "waiting_customer"
  | "waiting_internal"
  | "converted"
  | "closed"
  | "quarantine";
export type EmailIdentityState = "unknown" | "confirmed" | "revoked";
export type EmailConnectorHealthState = "healthy" | "degraded" | "paused" | "revoked" | "unknown";
export type EmailFreshnessState = "fresh" | "stale" | "unknown";

export interface EmailMailbox {
  mailbox_ref: string;
  display_label: string;
  provider_kind: EmailProviderKind;
  business_mode: EmailBusinessMode;
  business_purpose: EmailBusinessPurpose;
  default_team_label: string | null;
  account_owner_label: string | null;
  inbound_enabled: boolean;
  outbound_enabled: false;
  status: EmailMailboxStatus;
  config_revision: number;
}

export interface EmailMailboxListPayload {
  mailboxes: EmailMailbox[];
  next_cursor: string | null;
}

export interface EmailMailboxPayload { mailbox: EmailMailbox }
export interface EmailMailboxListQuery { cursor?: string; pageSize?: number }

export interface EmailMailboxUpsertCommand {
  canonical_mailbox_address: string;
  mailbox_ref?: string;
  display_label: string;
  provider_kind: EmailProviderKind;
  business_mode: EmailBusinessMode;
  business_purpose: EmailBusinessPurpose;
  provider_account_ref: string;
  observer_connector_instance_ref: string;
  default_team_ref: string;
  account_owner_user_ref: string;
  priority: number;
  credential_ref: string;
  inbound_enabled: boolean;
  outbound_enabled: false;
  expected_revision: number;
  idempotency_key: string;
}

export interface EmailMailboxStatusCommand {
  mailbox_ref: string;
  action: EmailMailboxAction;
  expected_revision: number;
  idempotency_key: string;
}

export interface EmailRoutingRule {
  rule_ref: string;
  mailbox_ref: string;
  team_label: string | null;
  owner_label: string | null;
  priority: number;
  revision: number;
  enabled: boolean;
}

export interface EmailRoutingRulePayload { rule: EmailRoutingRule }
export interface EmailRoutingRuleListPayload { rules: EmailRoutingRule[] }
export interface EmailRoutingRuleUpsertCommand {
  rule_ref?: string;
  team_ref: string;
  mailbox_ref: string;
  owner_user_ref: string;
  priority: number;
  enabled: boolean;
  expected_revision: number;
  idempotency_key: string;
}

export interface EmailInboxItem {
  inbox_item_ref: string;
  mailbox_label: string;
  mailbox_role: EmailBusinessMode;
  received_at: string;
  state: EmailInboxState;
  safe_summary: string;
  team_label: string | null;
  revision: number;
}

export interface EmailInboxDetail extends EmailInboxItem {
  assignee_label: string | null;
  identity_state: EmailIdentityState;
}

export interface EmailInboxListPayload { inbox_items: EmailInboxItem[]; next_cursor: string | null }
export interface EmailInboxDetailPayload { inbox_item: EmailInboxDetail }
export interface EmailInboxListQuery {
  state?: EmailInboxState;
  mailboxRef?: string;
  sort?: EmailInboxSort;
  cursor?: string;
  pageSize?: number;
}

export interface RevisionCommand {
  expected_revision: number;
  idempotency_key: string;
}
export interface EmailInboxClaimCommand extends RevisionCommand { inbox_item_ref: string }
export interface EmailInboxReassignCommand extends RevisionCommand {
  inbox_item_ref: string;
  assignee_user_ref?: string;
}
export interface EmailInboxTransitionCommand extends RevisionCommand {
  inbox_item_ref: string;
  target_state: EmailInboxState;
}
export interface EmailInboxMergeCommand {
  suggestion_ref: string;
  left_inbox_item_ref: string;
  expected_suggestion_revision: number;
  expected_left_revision: number;
  expected_right_revision: number;
  idempotency_key: string;
}
export interface EmailInboxSplitCommand extends RevisionCommand {
  conversation_ref: string;
  moved_inbox_item_refs: string[];
}
export interface EmailBusinessLinkCommand extends RevisionCommand {
  inbox_item_ref: string;
  business_ref: string;
}
export interface EmailDraftSaveCommand extends RevisionCommand {
  inbox_item_ref: string;
  draft_ref: string;
  content: string;
}
export interface EmailRevealCommand { inbox_item_ref: string; evidence_ref: string }

export interface EmailInboxCommandResult {
  inbox_item_ref: string;
  state: EmailInboxState;
  team_label?: string | null;
  assignee_label?: string | null;
  conversation_ref?: string | null;
  business_links?: string[];
  revision: number;
}
export interface EmailConversationResult {
  conversation_ref: string;
  team_label: string | null;
  lifecycle_state: string;
  inbox_item_refs: string[];
  revision: number;
}
export interface EmailDraftResult { draft_ref: string; revision: number; state: "editable" }
export interface EmailRevealResult { content: string; media_type: string }

export interface EmailConnectorHealth {
  mailbox_ref: string;
  mailbox_label: string;
  status: EmailConnectorHealthState;
  freshness: EmailFreshnessState;
  backlog: number;
  last_success_at: string | null;
  safe_error_code: string | null;
}
export interface EmailConnectorHealthPayload { connector_health: EmailConnectorHealth[] }

export interface V5SuccessEnvelope<T> {
  data: T;
  meta: {
    request_id: string;
    schema_version: "5.0";
    next_cursor?: string | null;
    replayed?: boolean;
    original_request_id?: string | null;
  };
}
