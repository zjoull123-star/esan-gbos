import { inject, type InjectionKey } from "vue";

import { readGbosBootstrap } from "../bootstrap";
import { BffError } from "./bff";
import type {
  EmailBusinessLinkCommand,
  EmailBusinessMode,
  EmailConnectorHealth,
  EmailConnectorHealthPayload,
  EmailConversationResult,
  EmailDraftResult,
  EmailDraftSaveCommand,
  EmailFreshnessState,
  EmailInboxClaimCommand,
  EmailInboxCommandResult,
  EmailInboxDetail,
  EmailInboxDetailPayload,
  EmailInboxItem,
  EmailInboxListPayload,
  EmailInboxListQuery,
  EmailInboxMergeCommand,
  EmailInboxReassignCommand,
  EmailInboxSplitCommand,
  EmailInboxState,
  EmailInboxTransitionCommand,
  EmailIdentityState,
  EmailMailbox,
  EmailMailboxListPayload,
  EmailMailboxListQuery,
  EmailMailboxPayload,
  EmailMailboxStatus,
  EmailMailboxStatusCommand,
  EmailMailboxUpsertCommand,
  EmailProviderKind,
  EmailRevealCommand,
  EmailRevealResult,
  EmailRoutingRule,
  EmailRoutingRuleListPayload,
  EmailRoutingRulePayload,
  EmailRoutingRuleUpsertCommand,
  EmailSlaPolicy,
  EmailSlaPolicyListPayload,
  EmailSlaPolicyListQuery,
  EmailSlaPolicyPayload,
  EmailSlaPolicyUpsertCommand,
  V5SuccessEnvelope,
} from "./email-gateway-types";

export const EMAIL_GATEWAY_ENDPOINTS = {
  mailboxList: "/api/method/esan_gbos.api.v5.email_admin.list_mailboxes",
  mailboxGet: "/api/method/esan_gbos.api.v5.email_admin.get_mailbox",
  ruleList: "/api/method/esan_gbos.api.v5.email_admin.list_rules",
  connectorHealth: "/api/method/esan_gbos.api.v5.email_admin.connector_health",
  mailboxUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert_mailbox",
  mailboxSetStatus: "/api/method/esan_gbos.api.v5.email_admin.set_mailbox_status",
  ruleUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert_rule",
  slaPolicyList: "/api/method/esan_gbos.api.v5.email_admin.list_sla_policies",
  slaPolicyUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert_sla_policy",
  inboxList: "/api/method/esan_gbos.api.v5.email_inbox.list",
  inboxGet: "/api/method/esan_gbos.api.v5.email_inbox.get",
  inboxClaim: "/api/method/esan_gbos.api.v5.email_inbox.claim",
  inboxReassign: "/api/method/esan_gbos.api.v5.email_inbox.reassign",
  inboxTransition: "/api/method/esan_gbos.api.v5.email_inbox.transition",
  inboxMerge: "/api/method/esan_gbos.api.v5.email_inbox.merge",
  inboxSplit: "/api/method/esan_gbos.api.v5.email_inbox.split",
  inboxLinkBusiness: "/api/method/esan_gbos.api.v5.email_inbox.link_business",
  inboxSaveDraft: "/api/method/esan_gbos.api.v5.email_inbox.save_draft",
  inboxReveal: "/api/method/esan_gbos.api.v5.email_inbox.reveal",
} as const;

export type EmailGatewayFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface EmailGatewayDependencies {
  fetcher?: EmailGatewayFetcher;
  isOnline?: () => boolean;
  getCsrfToken?: () => string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const closedKeys = (
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
) => {
  const allowed = new Set([...required, ...optional]);
  return required.every((key) => key in value) && Object.keys(value).every((key) => allowed.has(key));
};

const boundedText = (value: unknown, maximum = 500): value is string =>
  typeof value === "string" && value.trim().length > 0 && value.length <= maximum;
const optionalText = (value: unknown, maximum = 240) => value === null || boundedText(value, maximum);
const nonNegativeInteger = (value: unknown) => Number.isInteger(value) && Number(value) >= 0;
const safeProjectionText = (value: unknown, maximum = 500): value is string =>
  boundedText(value, maximum) &&
  !/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/u.test(value) &&
  !/(?:\+?\d[\d ()-]{7,}\d)/u.test(value) &&
  !/(?:secretref:|extid:|protected-ref|provider[_ -]message|raw message body)/iu.test(value);
const optionalSafeText = (value: unknown, maximum = 240) =>
  value === null || safeProjectionText(value, maximum);
const opaqueRef = (value: unknown, maximum = 140): value is string =>
  boundedText(value, maximum) && !/[?&#@\s]/u.test(value);
const commandRef = (value: unknown, maximum = 140): value is string =>
  boundedText(value, maximum) && !/[?&#\s]/u.test(value);
const safeCursor = (value: unknown): value is string =>
  boundedText(value, 512) && !/[?&#\s]/u.test(value) &&
  ![...value].some((character) => character.charCodeAt(0) <= 31 || character.charCodeAt(0) === 127);
const unique = (values: readonly string[]) => new Set(values).size === values.length;

const providerKinds = new Set<EmailProviderKind>(["fake", "imap_smtp", "wecom_app_mail"]);
const businessModes = new Set<EmailBusinessMode>(["primary", "selective_archive", "migration"]);
const businessPurposes = new Set([
  "business_operations", "observation_processing", "entity_resolution", "customer_service",
  "sales_follow_up", "procurement_coordination", "product_sample_management", "risk_review",
  "metric_reporting", "audit_compliance",
]);
const mailboxStatuses = new Set<EmailMailboxStatus>(["draft", "active", "paused", "revoked", "error"]);
const inboxStates = new Set<EmailInboxState>([
  "identity_pending", "unassigned", "assigned", "draft", "waiting_internal", "waiting_customer",
  "converted", "closed", "quarantined", "send_queued", "send_uncertain",
]);
const identityStates = new Set<EmailIdentityState>(["unknown", "confirmed", "revoked"]);
const healthStates = new Set(["healthy", "degraded", "paused", "revoked", "unknown"]);
const freshnessStates = new Set<EmailFreshnessState>(["fresh", "stale", "unknown"]);
const inboxSorts = new Set(["received_at_desc", "sla_due_at_asc"]);
const teamRefPattern = /^TEM-[0-9A-HJKMNP-TV-Z]{26}$/u;
const connectorRefPattern = /^OCI-[0-9A-HJKMNP-TV-Z]{26}$/u;
const credentialRefPattern = /^secretref:v1\/[A-Za-z0-9][A-Za-z0-9._/-]*$/u;
const mailboxAddressPattern = /^[^@\s]+@[^@\s]+\.[^@\s]+$/u;
const rfc3339Pattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-](\d{2}):(\d{2}))$/u;

const timezoneAwareRfc3339 = (value: unknown): value is string => {
  if (typeof value !== "string" || value.length > 64) return false;
  const match = rfc3339Pattern.exec(value);
  if (!match) return false;
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, offsetHourText, offsetMinuteText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  const offsetHour = Number(offsetHourText ?? 0);
  const offsetMinute = Number(offsetMinuteText ?? 0);
  const calendar = new Date(Date.UTC(year, month - 1, day));
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 &&
    calendar.getUTCFullYear() === year && calendar.getUTCMonth() === month - 1 && calendar.getUTCDate() === day &&
    hour <= 23 && minute <= 59 && second <= 59 && offsetHour <= 23 && offsetMinute <= 59 &&
    !Number.isNaN(Date.parse(value));
};

const invalidResponse = (requestId?: string): never => {
  throw new BffError("invalid_response", { requestId });
};
const validationError = (): never => { throw new BffError("validation_error"); };

const parseMailbox = (value: unknown, requestId?: string): EmailMailbox => {
  const keys = [
    "mailbox_ref", "display_label", "provider_kind", "business_mode", "business_purpose",
    "default_team_label", "account_owner_label", "inbound_enabled", "outbound_enabled", "status",
    "config_revision",
  ] as const;
  if (
    !isRecord(value) || !closedKeys(value, keys) || !opaqueRef(value.mailbox_ref) ||
    !safeProjectionText(value.display_label, 240) || typeof value.provider_kind !== "string" ||
    !providerKinds.has(value.provider_kind as EmailProviderKind) || typeof value.business_mode !== "string" ||
    !businessModes.has(value.business_mode as EmailBusinessMode) ||
    typeof value.business_purpose !== "string" || !businessPurposes.has(value.business_purpose) ||
    !optionalSafeText(value.default_team_label) || !optionalSafeText(value.account_owner_label) ||
    typeof value.inbound_enabled !== "boolean" || value.outbound_enabled !== false ||
    typeof value.status !== "string" || !mailboxStatuses.has(value.status as EmailMailboxStatus) ||
    !nonNegativeInteger(value.config_revision)
  ) invalidResponse(requestId);
  return value as unknown as EmailMailbox;
};

const parseInboxItem = (value: unknown, requestId?: string): EmailInboxItem => {
  const keys = [
    "inbox_item_ref", "mailbox_label", "mailbox_role", "received_at", "state", "safe_summary",
    "team_label", "revision",
  ] as const;
  if (
    !isRecord(value) || !closedKeys(value, keys) || !opaqueRef(value.inbox_item_ref) ||
    !safeProjectionText(value.mailbox_label, 240) || typeof value.mailbox_role !== "string" ||
    !businessModes.has(value.mailbox_role as EmailBusinessMode) || !boundedText(value.received_at, 64) ||
    Number.isNaN(Date.parse(value.received_at)) || typeof value.state !== "string" ||
    !inboxStates.has(value.state as EmailInboxState) || !safeProjectionText(value.safe_summary) ||
    !optionalSafeText(value.team_label) || !nonNegativeInteger(value.revision)
  ) invalidResponse(requestId);
  return value as unknown as EmailInboxItem;
};

const parseInboxDetail = (value: unknown, requestId?: string): EmailInboxDetail => {
  const record = isRecord(value) ? value : invalidResponse(requestId);
  const { assignee_label: assigneeLabel, identity_state: identityState, ...summary } = record;
  if (
    !closedKeys(record, [
      "inbox_item_ref", "mailbox_label", "mailbox_role", "received_at", "state", "safe_summary",
      "team_label", "revision", "assignee_label", "identity_state",
    ]) || !optionalSafeText(assigneeLabel) || typeof identityState !== "string" ||
    !identityStates.has(identityState as EmailIdentityState)
  ) invalidResponse(requestId);
  return { ...parseInboxItem(summary, requestId), assignee_label: assigneeLabel as string | null, identity_state: identityState as EmailIdentityState };
};

const parseHealth = (value: unknown, requestId?: string): EmailConnectorHealth => {
  if (
    !isRecord(value) || !closedKeys(value, [
      "mailbox_ref", "mailbox_label", "status", "freshness", "backlog", "last_success_at", "safe_error_code",
    ]) || !opaqueRef(value.mailbox_ref) || !safeProjectionText(value.mailbox_label, 240) ||
    typeof value.status !== "string" || !healthStates.has(value.status) ||
    typeof value.freshness !== "string" || !freshnessStates.has(value.freshness as EmailFreshnessState) ||
    !nonNegativeInteger(value.backlog) || !optionalText(value.last_success_at, 64) ||
    !optionalSafeText(value.safe_error_code, 80)
  ) invalidResponse(requestId);
  return value as unknown as EmailConnectorHealth;
};

const parseRule = (value: unknown, requestId?: string): EmailRoutingRule => {
  if (
    !isRecord(value) || !closedKeys(value, [
      "rule_ref", "mailbox_ref", "team_label", "owner_label", "priority", "revision", "enabled",
    ]) || !opaqueRef(value.rule_ref) || !opaqueRef(value.mailbox_ref) ||
    !optionalSafeText(value.team_label) || !optionalSafeText(value.owner_label) ||
    !nonNegativeInteger(value.priority) || !nonNegativeInteger(value.revision) || typeof value.enabled !== "boolean"
  ) invalidResponse(requestId);
  return value as unknown as EmailRoutingRule;
};

const parseSlaPolicy = (value: unknown, requestId?: string): EmailSlaPolicy => {
  if (
    !isRecord(value) || !closedKeys(value, [
      "policy_ref", "revision", "first_response_duration_seconds", "effective_at",
    ]) || !opaqueRef(value.policy_ref) || !nonNegativeInteger(value.revision) ||
    !Number.isInteger(value.first_response_duration_seconds) ||
    Number(value.first_response_duration_seconds) < 60 ||
    Number(value.first_response_duration_seconds) > 604_800 ||
    !timezoneAwareRfc3339(value.effective_at)
  ) invalidResponse(requestId);
  return value as unknown as EmailSlaPolicy;
};

const parseInboxCommand = (value: unknown, requestId?: string): EmailInboxCommandResult => {
  const record = isRecord(value) ? value : invalidResponse(requestId);
  const minimal = ["inbox_item_ref", "state", "revision"] as const;
  const full = [
    ...minimal, "team_label", "assignee_label", "conversation_ref", "business_links",
  ] as const;
  if (
    !(closedKeys(record, minimal) || closedKeys(record, full)) || !opaqueRef(record.inbox_item_ref) ||
    typeof record.state !== "string" || !inboxStates.has(record.state as EmailInboxState) ||
    !nonNegativeInteger(record.revision)
  ) invalidResponse(requestId);
  if ("business_links" in record) {
    if (!Array.isArray(record.business_links) || !record.business_links.every((item) => opaqueRef(item)) || !unique(record.business_links)) invalidResponse(requestId);
    if (!optionalSafeText(record.team_label) || !optionalSafeText(record.assignee_label) ||
      !(record.conversation_ref === null || opaqueRef(record.conversation_ref))) invalidResponse(requestId);
  }
  return record as unknown as EmailInboxCommandResult;
};

const parseConversation = (value: unknown, requestId?: string): EmailConversationResult => {
  if (
    !isRecord(value) || !closedKeys(value, ["conversation_ref", "team_label", "lifecycle_state", "inbox_item_refs", "revision"]) ||
    !opaqueRef(value.conversation_ref) || !optionalSafeText(value.team_label) ||
    !safeProjectionText(value.lifecycle_state, 64) || !Array.isArray(value.inbox_item_refs) ||
    !value.inbox_item_refs.every((item) => opaqueRef(item)) || !unique(value.inbox_item_refs) ||
    !nonNegativeInteger(value.revision)
  ) invalidResponse(requestId);
  return value as unknown as EmailConversationResult;
};

const parseDraft = (value: unknown, requestId?: string): EmailDraftResult => {
  if (!isRecord(value) || !closedKeys(value, ["draft_ref", "revision", "state"]) ||
    !opaqueRef(value.draft_ref) || !nonNegativeInteger(value.revision) || value.state !== "editable") invalidResponse(requestId);
  return value as unknown as EmailDraftResult;
};

const unwrap = (payload: unknown) => isRecord(payload) && "message" in payload ? payload.message : payload;
const supportedErrorCodes = {
  authentication_required: "authentication_required", permission_denied: "permission_denied",
  csrf_failed: "csrf_failed", method_not_allowed: "method_not_allowed", invalid_dto: "invalid_dto",
  invalid_query: "invalid_query", invalid_cursor: "invalid_cursor", not_found: "not_found",
  scope_mismatch: "scope_mismatch", identity_mismatch: "identity_mismatch",
  suggestion_mismatch: "suggestion_mismatch", candidate_ineligible: "candidate_ineligible",
  reviewer_ineligible: "reviewer_ineligible", revision_conflict: "revision_conflict",
  authority_conflict: "revision_conflict",
  invalid_transition: "invalid_transition", idempotency_conflict: "idempotency_conflict",
  request_in_progress: "request_in_progress", validation_error: "validation_error",
  internal_error: "internal_error",
} as const;

const errorFromPayload = (payload: unknown, status: number) => {
  const envelope = unwrap(payload);
  const candidate = isRecord(envelope) ? envelope.error : undefined;
  if (
    !isRecord(candidate) || !closedKeys(candidate, ["code", "message", "request_id", "details"]) ||
    typeof candidate.code !== "string" || !(candidate.code in supportedErrorCodes) ||
    !boundedText(candidate.message, 500) || !boundedText(candidate.request_id, 140) || !isRecord(candidate.details)
  ) return new BffError("internal_error", { status });
  const code = supportedErrorCodes[candidate.code as keyof typeof supportedErrorCodes];
  return new BffError(code, { requestId: candidate.request_id, status });
};

const normalizeEnvelope = <T>(payload: unknown, status: number): V5SuccessEnvelope<T> => {
  const value = unwrap(payload);
  const record = isRecord(value) ? value : invalidResponse();
  if (!closedKeys(record, ["data", "meta"]) || !isRecord(record.meta)) invalidResponse();
  const meta = isRecord(record.meta) ? record.meta : invalidResponse();
  if (!closedKeys(meta, ["request_id", "schema_version"], ["next_cursor", "replayed", "original_request_id"])) invalidResponse();
  const requestId = boundedText(meta.request_id, 140) ? meta.request_id : undefined;
  if (meta.schema_version !== "5.0") throw new BffError("schema_mismatch", { requestId, status });
  if (!requestId || !(meta.next_cursor === undefined || meta.next_cursor === null || boundedText(meta.next_cursor, 512)) ||
    !(meta.replayed === undefined || typeof meta.replayed === "boolean") ||
    !(meta.original_request_id === undefined || meta.original_request_id === null || boundedText(meta.original_request_id, 140))) invalidResponse(requestId);
  return record as unknown as V5SuccessEnvelope<T>;
};

const addQuery = (path: string, values: Record<string, string | number | undefined>) => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined) query.set(key, String(value));
  return query.size ? `${path}?${query}` : path;
};
const formBody = (command: Record<string, unknown>) => {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(command)) {
    if (value !== undefined) result.set(key, Array.isArray(value) ? JSON.stringify(value) : String(value));
  }
  return result.toString();
};
const defaultCsrfToken = () => {
  const bootstrap = readGbosBootstrap();
  if (bootstrap?.csrf_token) return bootstrap.csrf_token;
  const host = globalThis as typeof globalThis & { frappe?: { csrf_token?: string } };
  return host.frappe?.csrf_token ?? "";
};

export const createEmailGatewayClient = (dependencies: EmailGatewayDependencies = {}) => {
  const fetcher = dependencies.fetcher ?? globalThis.fetch.bind(globalThis);
  const isOnline = dependencies.isOnline ?? (() => typeof navigator === "undefined" || navigator.onLine);
  const getCsrfToken = dependencies.getCsrfToken ?? defaultCsrfToken;

  const request = async <T>(url: string, init: RequestInit) => {
    if (!isOnline()) throw new BffError("offline");
    let response: Response;
    try {
      response = await fetcher(url, {
        ...init, credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json", "Cache-Control": "no-store", Pragma: "no-cache", ...init.headers },
      });
    } catch { throw new BffError("network_error"); }
    let payload: unknown;
    try { payload = await response.json(); } catch { invalidResponse(); }
    if (!response.ok) throw errorFromPayload(payload, response.status);
    return normalizeEnvelope<T>(payload, response.status);
  };
  const get = <T>(url: string) => request<T>(url, { method: "GET" });
  const post = <T>(url: string, command: Record<string, unknown>) => {
    const csrfToken = getCsrfToken();
    if (!csrfToken) throw new BffError("csrf_missing");
    return request<T>(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Frappe-CSRF-Token": csrfToken },
      body: formBody(command),
    });
  };
  const validateRevision = (command: { expected_revision: number; idempotency_key: string }) => {
    if (!nonNegativeInteger(command.expected_revision) || !boundedText(command.idempotency_key, 256) || command.idempotency_key.length < 8) validationError();
  };
  const parseSingle = <K extends string, T>(response: V5SuccessEnvelope<unknown>, key: K, parser: (value: unknown, requestId?: string) => T) => {
    const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
    if (!closedKeys(data, [key])) invalidResponse(response.meta.request_id);
    return { ...response, data: { [key]: parser(data[key], response.meta.request_id) } } as V5SuccessEnvelope<Record<K, T>>;
  };

  return {
    listMailboxes: async (query: EmailMailboxListQuery = {}) => {
      if (query.pageSize !== undefined && (!Number.isInteger(query.pageSize) || query.pageSize < 1 || query.pageSize > 50)) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.mailboxList, { cursor: query.cursor, page_size: query.pageSize }));
      const value = response.data;
      const data = isRecord(value) ? value : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["mailboxes", "next_cursor"]) ||
        !(data.next_cursor === null || boundedText(data.next_cursor, 512))) invalidResponse(response.meta.request_id);
      const rows = Array.isArray(data.mailboxes) ? data.mailboxes : invalidResponse(response.meta.request_id);
      const mailboxes = rows.map((item) => parseMailbox(item, response.meta.request_id));
      if (!unique(mailboxes.map((item) => item.mailbox_ref))) invalidResponse(response.meta.request_id);
      return { ...response, data: { mailboxes, next_cursor: data.next_cursor } } as V5SuccessEnvelope<EmailMailboxListPayload>;
    },
    getMailbox: async (mailboxRef: string) => {
      if (!opaqueRef(mailboxRef)) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.mailboxGet, { mailbox_ref: mailboxRef }));
      return parseSingle(response, "mailbox", parseMailbox) as V5SuccessEnvelope<EmailMailboxPayload>;
    },
    upsertMailbox: async (command: EmailMailboxUpsertCommand) => {
      validateRevision(command);
      if (!boundedText(command.canonical_mailbox_address, 254) ||
        command.canonical_mailbox_address !== command.canonical_mailbox_address.trim() ||
        !mailboxAddressPattern.test(command.canonical_mailbox_address) ||
        !safeProjectionText(command.display_label, 240) || !providerKinds.has(command.provider_kind) ||
        !businessModes.has(command.business_mode) || !businessPurposes.has(command.business_purpose) ||
        !boundedText(command.provider_account_ref, 256) || !connectorRefPattern.test(command.observer_connector_instance_ref) ||
        !teamRefPattern.test(command.default_team_ref) || !boundedText(command.account_owner_user_ref, 140) ||
        !Number.isInteger(command.priority) || command.priority < 0 || command.priority > 1000 ||
        !credentialRefPattern.test(command.credential_ref) || command.credential_ref.length > 128 ||
        typeof command.inbound_enabled !== "boolean" || command.outbound_enabled !== false ||
        (command.mailbox_ref !== undefined && !opaqueRef(command.mailbox_ref))) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.mailboxUpsert, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "mailbox", parseMailbox) as V5SuccessEnvelope<EmailMailboxPayload>;
      if (result.data.mailbox.config_revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    setMailboxStatus: async (command: EmailMailboxStatusCommand) => {
      validateRevision(command);
      if (!opaqueRef(command.mailbox_ref) || !new Set(["enable", "pause", "revoke"]).has(command.action)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.mailboxSetStatus, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "mailbox", parseMailbox) as V5SuccessEnvelope<EmailMailboxPayload>;
      if (result.data.mailbox.config_revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    listRules: async (pageSize = 25) => {
      if (!Number.isInteger(pageSize) || pageSize < 1 || pageSize > 50) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.ruleList, { page_size: pageSize }));
      const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["rules"])) invalidResponse(response.meta.request_id);
      const rows = Array.isArray(data.rules) ? data.rules : invalidResponse(response.meta.request_id);
      const rules = rows.map((item) => parseRule(item, response.meta.request_id));
      if (!unique(rules.map((item) => item.rule_ref))) invalidResponse(response.meta.request_id);
      return { ...response, data: { rules } } as V5SuccessEnvelope<EmailRoutingRuleListPayload>;
    },
    upsertRule: async (command: EmailRoutingRuleUpsertCommand) => {
      validateRevision(command);
      if (!teamRefPattern.test(command.team_ref) || !opaqueRef(command.mailbox_ref) || !boundedText(command.owner_user_ref, 140) ||
        !Number.isInteger(command.priority) || command.priority < 0 || command.priority > 1000 || typeof command.enabled !== "boolean" ||
        (command.rule_ref !== undefined && !opaqueRef(command.rule_ref))) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.ruleUpsert, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "rule", parseRule) as V5SuccessEnvelope<EmailRoutingRulePayload>;
      if (result.data.rule.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    listSlaPolicies: async (query: EmailSlaPolicyListQuery) => {
      if (!isRecord(query) || !closedKeys(query, ["mailboxRef"], ["cursor", "pageSize"]) ||
        !opaqueRef(query.mailboxRef) ||
        (query.cursor !== undefined && !safeCursor(query.cursor)) ||
        (query.pageSize !== undefined && (!Number.isInteger(query.pageSize) || Number(query.pageSize) < 1 || Number(query.pageSize) > 50))) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.slaPolicyList, {
        mailbox_ref: query.mailboxRef as string,
        cursor: query.cursor as string | undefined,
        page_size: query.pageSize as number | undefined,
      }));
      const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["mailbox_ref", "sla_policies", "next_cursor"]) ||
        data.mailbox_ref !== query.mailboxRef || !opaqueRef(data.mailbox_ref) ||
        !(data.next_cursor === null || safeCursor(data.next_cursor))) invalidResponse(response.meta.request_id);
      const rows = Array.isArray(data.sla_policies) ? data.sla_policies : invalidResponse(response.meta.request_id);
      const slaPolicies = rows.map((item) => parseSlaPolicy(item, response.meta.request_id));
      if (!unique(slaPolicies.map((item) => item.policy_ref)) || !unique(slaPolicies.map((item) => String(item.revision)))) invalidResponse(response.meta.request_id);
      return { ...response, data: {
        mailbox_ref: data.mailbox_ref,
        sla_policies: slaPolicies,
        next_cursor: data.next_cursor,
      } } as V5SuccessEnvelope<EmailSlaPolicyListPayload>;
    },
    upsertSlaPolicy: async (command: EmailSlaPolicyUpsertCommand) => {
      if (!isRecord(command) || !closedKeys(command, [
        "mailbox_ref", "first_response_duration_seconds", "effective_at", "expected_revision", "idempotency_key",
      ])) validationError();
      validateRevision(command);
      if (!opaqueRef(command.mailbox_ref) || !Number.isInteger(command.first_response_duration_seconds) ||
        command.first_response_duration_seconds < 60 || command.first_response_duration_seconds > 604_800 ||
        !timezoneAwareRfc3339(command.effective_at)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.slaPolicyUpsert, command as unknown as Record<string, unknown>);
      const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["sla_policy"])) invalidResponse(response.meta.request_id);
      const policyRecord = isRecord(data.sla_policy) ? data.sla_policy : invalidResponse(response.meta.request_id);
      if (!closedKeys(policyRecord, [
          "policy_ref", "mailbox_ref", "revision", "first_response_duration_seconds", "effective_at",
        ]) || policyRecord.mailbox_ref !== command.mailbox_ref || !opaqueRef(policyRecord.mailbox_ref)) invalidResponse(response.meta.request_id);
      const { mailbox_ref: mailboxRef, ...policyValue } = policyRecord;
      const slaPolicy = { ...parseSlaPolicy(policyValue, response.meta.request_id), mailbox_ref: mailboxRef };
      if (slaPolicy.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return { ...response, data: { sla_policy: slaPolicy } } as V5SuccessEnvelope<EmailSlaPolicyPayload>;
    },
    listConnectorHealth: async () => {
      const response = await get<unknown>(EMAIL_GATEWAY_ENDPOINTS.connectorHealth);
      const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["connector_health"])) invalidResponse(response.meta.request_id);
      const rows = Array.isArray(data.connector_health) ? data.connector_health : invalidResponse(response.meta.request_id);
      const connectorHealth = rows.map((item) => parseHealth(item, response.meta.request_id));
      if (!unique(connectorHealth.map((item) => item.mailbox_ref))) invalidResponse(response.meta.request_id);
      return { ...response, data: { connector_health: connectorHealth } } as V5SuccessEnvelope<EmailConnectorHealthPayload>;
    },
    listInbox: async (query: EmailInboxListQuery = {}) => {
      if ((query.pageSize !== undefined && (!Number.isInteger(query.pageSize) || query.pageSize < 1 || query.pageSize > 50)) ||
        (query.state !== undefined && !inboxStates.has(query.state)) ||
        (query.sort !== undefined && !inboxSorts.has(query.sort)) ||
        (query.mailboxRef !== undefined && !opaqueRef(query.mailboxRef))) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.inboxList, {
        state: query.state, mailbox_ref: query.mailboxRef, sort: query.sort, cursor: query.cursor, page_size: query.pageSize,
      }));
      const data = isRecord(response.data) ? response.data : invalidResponse(response.meta.request_id);
      if (!closedKeys(data, ["inbox_items", "next_cursor"]) ||
        !(data.next_cursor === null || boundedText(data.next_cursor, 512))) invalidResponse(response.meta.request_id);
      const rows = Array.isArray(data.inbox_items) ? data.inbox_items : invalidResponse(response.meta.request_id);
      const inboxItems = rows.map((item) => parseInboxItem(item, response.meta.request_id));
      if (!unique(inboxItems.map((item) => item.inbox_item_ref))) invalidResponse(response.meta.request_id);
      return { ...response, data: { inbox_items: inboxItems, next_cursor: data.next_cursor } } as V5SuccessEnvelope<EmailInboxListPayload>;
    },
    getInboxItem: async (inboxItemRef: string) => {
      if (!opaqueRef(inboxItemRef)) validationError();
      const response = await get<unknown>(addQuery(EMAIL_GATEWAY_ENDPOINTS.inboxGet, { inbox_item_ref: inboxItemRef }));
      return parseSingle(response, "inbox_item", parseInboxDetail) as V5SuccessEnvelope<EmailInboxDetailPayload>;
    },
    claimInbox: async (command: EmailInboxClaimCommand) => {
      validateRevision(command); if (!opaqueRef(command.inbox_item_ref)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxClaim, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "inbox_item", parseInboxCommand);
      if (result.data.inbox_item.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    reassignInbox: async (command: EmailInboxReassignCommand) => {
      validateRevision(command); if (!opaqueRef(command.inbox_item_ref) ||
        (command.assignee_user_ref !== undefined && !commandRef(command.assignee_user_ref))) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxReassign, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "inbox_item", parseInboxCommand);
      if (result.data.inbox_item.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    transitionInbox: async (command: EmailInboxTransitionCommand) => {
      validateRevision(command); if (!opaqueRef(command.inbox_item_ref) || !inboxStates.has(command.target_state)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxTransition, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "inbox_item", parseInboxCommand);
      if (result.data.inbox_item.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    mergeInbox: async (command: EmailInboxMergeCommand) => {
      if (!opaqueRef(command.suggestion_ref) || !opaqueRef(command.left_inbox_item_ref) ||
        !nonNegativeInteger(command.expected_suggestion_revision) || !nonNegativeInteger(command.expected_left_revision) ||
        !nonNegativeInteger(command.expected_right_revision) || !boundedText(command.idempotency_key, 256) || command.idempotency_key.length < 8) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxMerge, command as unknown as Record<string, unknown>);
      return parseSingle(response, "conversation", parseConversation);
    },
    splitConversation: async (command: EmailInboxSplitCommand) => {
      validateRevision(command); if (!opaqueRef(command.conversation_ref) || command.moved_inbox_item_refs.length === 0 ||
        !command.moved_inbox_item_refs.every((item) => opaqueRef(item)) || !unique(command.moved_inbox_item_refs)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxSplit, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "conversation", parseConversation);
      if (result.data.conversation.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    linkBusiness: async (command: EmailBusinessLinkCommand) => {
      validateRevision(command); if (!opaqueRef(command.inbox_item_ref) || !opaqueRef(command.business_ref)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxLinkBusiness, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "inbox_item", parseInboxCommand);
      if (result.data.inbox_item.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    saveDraft: async (command: EmailDraftSaveCommand) => {
      validateRevision(command); const contentBytes = new TextEncoder().encode(command.content).byteLength;
      if (!opaqueRef(command.inbox_item_ref) || !opaqueRef(command.draft_ref) || !boundedText(command.content, 131_072) || contentBytes > 131_072) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxSaveDraft, command as unknown as Record<string, unknown>);
      const result = parseSingle(response, "draft", parseDraft);
      if (result.data.draft.revision <= command.expected_revision) invalidResponse(response.meta.request_id);
      return result;
    },
    revealEvidence: async (command: EmailRevealCommand) => {
      if (!opaqueRef(command.inbox_item_ref) || !opaqueRef(command.evidence_ref)) validationError();
      const response = await post<unknown>(EMAIL_GATEWAY_ENDPOINTS.inboxReveal, command as unknown as Record<string, unknown>);
      if (!isRecord(response.data) || !closedKeys(response.data, ["content", "media_type"]) ||
        typeof response.data.content !== "string" || response.data.content.length > 131_072 ||
        !boundedText(response.data.media_type, 120)) invalidResponse(response.meta.request_id);
      return { ...response, data: response.data as unknown as EmailRevealResult };
    },
  };
};

export type EmailGatewayClient = ReturnType<typeof createEmailGatewayClient>;
export const EMAIL_GATEWAY_CLIENT_KEY: InjectionKey<EmailGatewayClient> = Symbol("gbos-email-gateway-client");
export const useEmailGatewayClient = () => inject(EMAIL_GATEWAY_CLIENT_KEY, null) ?? createEmailGatewayClient();
