import { inject, type InjectionKey } from "vue";

import { readGbosBootstrap } from "../bootstrap";
import { BffError } from "./bff";
import type {
  EmailBusinessMode,
  EmailConnectorHealth,
  EmailConnectorHealthPayload,
  EmailFreshnessState,
  EmailInboxDetail,
  EmailInboxDetailPayload,
  EmailInboxItem,
  EmailInboxListPayload,
  EmailInboxListQuery,
  EmailInboxState,
  EmailIdentityState,
  EmailMailbox,
  EmailMailboxListPayload,
  EmailMailboxListQuery,
  EmailMailboxPayload,
  EmailMailboxStatus,
  EmailMailboxStatusCommand,
  EmailMailboxUpsertCommand,
  EmailProviderKind,
  V5SuccessEnvelope,
} from "./email-gateway-types";

export const EMAIL_GATEWAY_ENDPOINTS = {
  mailboxList: "/api/method/esan_gbos.api.v5.email_admin.list",
  mailboxGet: "/api/method/esan_gbos.api.v5.email_admin.get",
  mailboxUpsert: "/api/method/esan_gbos.api.v5.email_admin.upsert",
  mailboxSetStatus: "/api/method/esan_gbos.api.v5.email_admin.set_status",
  connectorHealth:
    "/api/method/esan_gbos.api.v5.email_admin.get_connector_health",
  inboxList: "/api/method/esan_gbos.api.v5.email_inbox.list",
  inboxGet: "/api/method/esan_gbos.api.v5.email_inbox.get",
} as const;

export type EmailGatewayFetcher = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

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
) => {
  const expected = new Set(required);
  return (
    required.every((key) => key in value) &&
    Object.keys(value).every((key) => expected.has(key))
  );
};

const boundedText = (value: unknown, maximum = 500): value is string =>
  typeof value === "string" &&
  value.trim().length > 0 &&
  value.length <= maximum;

const optionalText = (value: unknown, maximum = 240) =>
  value === null || boundedText(value, maximum);

const nonNegativeInteger = (value: unknown) =>
  Number.isInteger(value) && Number(value) >= 0;

const providerKinds = new Set<EmailProviderKind>([
  "fake",
  "imap_smtp",
  "wecom_app_mail",
]);
const businessModes = new Set<EmailBusinessMode>([
  "primary",
  "selective_archive",
  "migration",
]);
const businessPurposes = new Set([
  "business_operations",
  "observation_processing",
  "entity_resolution",
  "customer_service",
  "sales_follow_up",
  "procurement_coordination",
  "product_sample_management",
  "risk_review",
  "metric_reporting",
  "audit_compliance",
]);
const teamRefPattern = /^TEM-[0-9A-HJKMNP-TV-Z]{26}$/;
const connectorRefPattern = /^OCI-[0-9A-HJKMNP-TV-Z]{26}$/;
const credentialRefPattern = /^secretref:v1\/[A-Za-z0-9][A-Za-z0-9._/-]*$/;
const mailboxStatuses = new Set<EmailMailboxStatus>([
  "draft",
  "active",
  "paused",
  "revoked",
  "error",
]);
const inboxStates = new Set<EmailInboxState>(["identity_pending", "unassigned"]);
const identityStates = new Set<EmailIdentityState>([
  "unknown",
  "confirmed",
  "revoked",
]);
const healthStates = new Set([
  "healthy",
  "degraded",
  "paused",
  "revoked",
  "unknown",
]);
const freshnessStates = new Set<EmailFreshnessState>(["fresh", "stale", "unknown"]);

const invalidResponse = (requestId?: string): never => {
  throw new BffError("invalid_response", { requestId });
};

const parseMailbox = (value: unknown, requestId?: string): EmailMailbox => {
  const keys = [
    "mailbox_ref",
    "display_label",
    "provider_kind",
    "business_mode",
    "business_purpose",
    "default_team_label",
    "account_owner_label",
    "inbound_enabled",
    "outbound_enabled",
    "status",
    "config_revision",
  ] as const;
  if (
    !isRecord(value) ||
    !closedKeys(value, keys) ||
    !boundedText(value.mailbox_ref, 140) ||
    !boundedText(value.display_label, 240) ||
    typeof value.provider_kind !== "string" ||
    !providerKinds.has(value.provider_kind as EmailProviderKind) ||
    typeof value.business_mode !== "string" ||
    !businessModes.has(value.business_mode as EmailBusinessMode) ||
    !boundedText(value.business_purpose, 80) ||
    !optionalText(value.default_team_label) ||
    !optionalText(value.account_owner_label) ||
    typeof value.inbound_enabled !== "boolean" ||
    value.outbound_enabled !== false ||
    typeof value.status !== "string" ||
    !mailboxStatuses.has(value.status as EmailMailboxStatus) ||
    !nonNegativeInteger(value.config_revision)
  ) {
    return invalidResponse(requestId);
  }
  return value as unknown as EmailMailbox;
};

const parseInboxItem = (value: unknown, requestId?: string): EmailInboxItem => {
  const keys = [
    "inbox_item_ref",
    "mailbox_label",
    "mailbox_role",
    "received_at",
    "state",
    "safe_summary",
    "team_label",
    "revision",
  ] as const;
  if (
    !isRecord(value) ||
    !closedKeys(value, keys) ||
    !boundedText(value.inbox_item_ref, 140) ||
    !boundedText(value.mailbox_label, 240) ||
    typeof value.mailbox_role !== "string" ||
    !businessModes.has(value.mailbox_role as EmailBusinessMode) ||
    !boundedText(value.received_at, 64) ||
    typeof value.state !== "string" ||
    !inboxStates.has(value.state as EmailInboxState) ||
    !boundedText(value.safe_summary) ||
    !optionalText(value.team_label) ||
    !nonNegativeInteger(value.revision)
  ) {
    return invalidResponse(requestId);
  }
  return value as unknown as EmailInboxItem;
};

const parseInboxDetail = (value: unknown, requestId?: string): EmailInboxDetail => {
  if (!isRecord(value)) {
    return invalidResponse(requestId);
  }
  const { assignee_label: assigneeLabel, identity_state: identityState, ...summary } = value;
  if (
    !closedKeys(value, [
      "inbox_item_ref",
      "mailbox_label",
      "mailbox_role",
      "received_at",
      "state",
      "safe_summary",
      "team_label",
      "revision",
      "assignee_label",
      "identity_state",
    ]) ||
    !optionalText(assigneeLabel) ||
    typeof identityState !== "string" ||
    !identityStates.has(identityState as EmailIdentityState)
  ) {
    return invalidResponse(requestId);
  }
  return {
    ...parseInboxItem(summary, requestId),
    assignee_label: assigneeLabel as string | null,
    identity_state: identityState as EmailIdentityState,
  };
};

const parseHealth = (value: unknown, requestId?: string): EmailConnectorHealth => {
  const keys = [
    "mailbox_ref",
    "mailbox_label",
    "status",
    "freshness",
    "backlog",
    "last_success_at",
    "safe_error_code",
  ] as const;
  if (
    !isRecord(value) ||
    !closedKeys(value, keys) ||
    !boundedText(value.mailbox_ref, 140) ||
    !boundedText(value.mailbox_label, 240) ||
    typeof value.status !== "string" ||
    !healthStates.has(value.status) ||
    typeof value.freshness !== "string" ||
    !freshnessStates.has(value.freshness as EmailFreshnessState) ||
    !nonNegativeInteger(value.backlog) ||
    !optionalText(value.last_success_at, 64) ||
    !optionalText(value.safe_error_code, 80)
  ) {
    return invalidResponse(requestId);
  }
  return value as unknown as EmailConnectorHealth;
};

const unwrap = (payload: unknown) =>
  isRecord(payload) && "message" in payload ? payload.message : payload;

const normalizeEnvelope = <T>(payload: unknown, status: number): V5SuccessEnvelope<T> => {
  const value = unwrap(payload);
  if (!isRecord(value) || !("data" in value) || !isRecord(value.meta)) {
    return invalidResponse();
  }
  const requestId =
    typeof value.meta.request_id === "string" ? value.meta.request_id : undefined;
  if (value.meta.schema_version !== "5.0") {
    throw new BffError("schema_mismatch", { requestId, status });
  }
  if (!requestId) {
    return invalidResponse();
  }
  return value as unknown as V5SuccessEnvelope<T>;
};

const addQuery = (path: string, values: Record<string, string | number | undefined>) => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined) {
      query.set(key, String(value));
    }
  }
  return query.size ? `${path}?${query}` : path;
};

const formBody = (command: Record<string, unknown>) => {
  const result = new URLSearchParams();
  for (const [key, value] of Object.entries(command)) {
    if (value !== undefined) {
      result.set(key, String(value));
    }
  }
  return result.toString();
};

const defaultCsrfToken = () => {
  const bootstrap = readGbosBootstrap();
  if (bootstrap?.csrf_token) {
    return bootstrap.csrf_token;
  }
  const host = globalThis as typeof globalThis & { frappe?: { csrf_token?: string } };
  return host.frappe?.csrf_token ?? "";
};

export const createEmailGatewayClient = (
  dependencies: EmailGatewayDependencies = {},
) => {
  const fetcher = dependencies.fetcher ?? globalThis.fetch.bind(globalThis);
  const isOnline = dependencies.isOnline ?? (() => navigator.onLine);
  const getCsrfToken = dependencies.getCsrfToken ?? defaultCsrfToken;

  const request = async <T>(url: string, init: RequestInit) => {
    if (!isOnline()) {
      throw new BffError("offline");
    }
    let response: Response;
    try {
      response = await fetcher(url, {
        ...init,
        credentials: "same-origin",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Cache-Control": "no-store",
          Pragma: "no-cache",
          ...init.headers,
        },
      });
    } catch {
      throw new BffError("network_error");
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      return invalidResponse();
    }
    if (!response.ok) {
      throw new BffError(
        response.status === 403 ? "permission_denied" : "internal_error",
        { status: response.status },
      );
    }
    return normalizeEnvelope<T>(payload, response.status);
  };

  const get = <T>(url: string) => request<T>(url, { method: "GET" });
  const post = <T>(url: string, command: Record<string, unknown>) => {
    if (
      !Number.isInteger(command.expected_revision) ||
      Number(command.expected_revision) < 0 ||
      !boundedText(command.idempotency_key, 256) ||
      String(command.idempotency_key).length < 8
    ) {
      throw new BffError("validation_error");
    }
    const csrfToken = getCsrfToken();
    if (!csrfToken) {
      throw new BffError("csrf_missing");
    }
    return request<T>(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Frappe-CSRF-Token": csrfToken,
      },
      body: formBody(command),
    });
  };

  return {
    listMailboxes: async (query: EmailMailboxListQuery = {}) => {
      const response = await get<unknown>(
        addQuery(EMAIL_GATEWAY_ENDPOINTS.mailboxList, {
          cursor: query.cursor,
          page_size: query.pageSize,
        }),
      );
      const value = response.data;
      if (
        !isRecord(value) ||
        !closedKeys(value, ["mailboxes", "next_cursor"]) ||
        !Array.isArray(value.mailboxes) ||
        !(value.next_cursor === null || boundedText(value.next_cursor, 512))
      ) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: {
          mailboxes: value.mailboxes.map((item) =>
            parseMailbox(item, response.meta.request_id),
          ),
          next_cursor: value.next_cursor,
        },
      } as V5SuccessEnvelope<EmailMailboxListPayload>;
    },
    getMailbox: async (mailboxRef: string) => {
      const response = await get<unknown>(
        addQuery(EMAIL_GATEWAY_ENDPOINTS.mailboxGet, { mailbox_ref: mailboxRef }),
      );
      if (!isRecord(response.data) || !closedKeys(response.data, ["mailbox"])) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: { mailbox: parseMailbox(response.data.mailbox, response.meta.request_id) },
      } as V5SuccessEnvelope<EmailMailboxPayload>;
    },
    upsertMailbox: async (command: EmailMailboxUpsertCommand) => {
      if (
        !boundedText(command.display_label, 240) ||
        !providerKinds.has(command.provider_kind) ||
        !businessModes.has(command.business_mode) ||
        !businessPurposes.has(command.business_purpose) ||
        !boundedText(command.provider_account_ref, 256) ||
        !connectorRefPattern.test(command.observer_connector_instance_ref) ||
        !teamRefPattern.test(command.default_team_ref) ||
        !boundedText(command.account_owner_user_ref, 140) ||
        !Number.isInteger(command.priority) ||
        command.priority < 0 ||
        command.priority > 1000 ||
        !credentialRefPattern.test(command.credential_ref) ||
        command.credential_ref.length > 128 ||
        typeof command.inbound_enabled !== "boolean" ||
        command.outbound_enabled !== false ||
        (command.mailbox_ref !== undefined && !boundedText(command.mailbox_ref, 140))
      ) {
        throw new BffError("validation_error");
      }
      const response = await post<unknown>(
        EMAIL_GATEWAY_ENDPOINTS.mailboxUpsert,
        command as unknown as Record<string, unknown>,
      );
      if (!isRecord(response.data) || !closedKeys(response.data, ["mailbox"])) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: { mailbox: parseMailbox(response.data.mailbox, response.meta.request_id) },
      } as V5SuccessEnvelope<EmailMailboxPayload>;
    },
    setMailboxStatus: async (command: EmailMailboxStatusCommand) => {
      const response = await post<unknown>(
        EMAIL_GATEWAY_ENDPOINTS.mailboxSetStatus,
        command as unknown as Record<string, unknown>,
      );
      if (!isRecord(response.data) || !closedKeys(response.data, ["mailbox"])) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: { mailbox: parseMailbox(response.data.mailbox, response.meta.request_id) },
      } as V5SuccessEnvelope<EmailMailboxPayload>;
    },
    listConnectorHealth: async () => {
      const response = await get<unknown>(EMAIL_GATEWAY_ENDPOINTS.connectorHealth);
      if (
        !isRecord(response.data) ||
        !closedKeys(response.data, ["connector_health"]) ||
        !Array.isArray(response.data.connector_health)
      ) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: {
          connector_health: response.data.connector_health.map((item) =>
            parseHealth(item, response.meta.request_id),
          ),
        },
      } as V5SuccessEnvelope<EmailConnectorHealthPayload>;
    },
    listInbox: async (query: EmailInboxListQuery = {}) => {
      const response = await get<unknown>(
        addQuery(EMAIL_GATEWAY_ENDPOINTS.inboxList, {
          state: query.state,
          cursor: query.cursor,
          page_size: query.pageSize,
        }),
      );
      if (
        !isRecord(response.data) ||
        !closedKeys(response.data, ["inbox_items", "next_cursor"]) ||
        !Array.isArray(response.data.inbox_items) ||
        !(response.data.next_cursor === null || boundedText(response.data.next_cursor, 512))
      ) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: {
          inbox_items: response.data.inbox_items.map((item) =>
            parseInboxItem(item, response.meta.request_id),
          ),
          next_cursor: response.data.next_cursor,
        },
      } as V5SuccessEnvelope<EmailInboxListPayload>;
    },
    getInboxItem: async (inboxItemRef: string) => {
      const response = await get<unknown>(
        addQuery(EMAIL_GATEWAY_ENDPOINTS.inboxGet, {
          inbox_item_ref: inboxItemRef,
        }),
      );
      if (!isRecord(response.data) || !closedKeys(response.data, ["inbox_item"])) {
        return invalidResponse(response.meta.request_id);
      }
      return {
        ...response,
        data: {
          inbox_item: parseInboxDetail(
            response.data.inbox_item,
            response.meta.request_id,
          ),
        },
      } as V5SuccessEnvelope<EmailInboxDetailPayload>;
    },
  };
};

export type EmailGatewayClient = ReturnType<typeof createEmailGatewayClient>;
export const EMAIL_GATEWAY_CLIENT_KEY: InjectionKey<EmailGatewayClient> = Symbol(
  "gbos-email-gateway-client",
);

export const useEmailGatewayClient = () =>
  inject(EMAIL_GATEWAY_CLIENT_KEY, null) ?? createEmailGatewayClient();
