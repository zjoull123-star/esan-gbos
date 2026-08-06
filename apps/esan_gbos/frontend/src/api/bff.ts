import type {
  ContractError,
  ContractErrorCode,
  ReviewCaseDetailPayload,
  ReviewCaseListPayload,
  ReviewCaseListQuery,
  ReviewDecisionCommand,
  SampleCreateCommand,
  SampleFeedbackCommand,
  SourcingCreateCommand,
  SuccessEnvelope,
  WorkItemListQuery,
  WorkItemTransitionCommand,
} from "./types";
import { readGbosBootstrap } from "@/bootstrap";

export const BFF_ENDPOINTS = {
  party360: "/api/method/esan_gbos.api.v1.party.get_360",
  workItemList: "/api/method/esan_gbos.api.v1.work_item.list",
  sampleStatus: "/api/method/esan_gbos.api.v1.sample.get_status",
  sourcingBoard: "/api/method/esan_gbos.api.v1.sourcing.get_board",
  sampleCreate: "/api/method/esan_gbos.api.v1.sample.create_project",
  sampleFeedback: "/api/method/esan_gbos.api.v1.sample.record_feedback",
  sourcingCreate: "/api/method/esan_gbos.api.v1.sourcing.create_from_demand",
  workItemTransition: "/api/method/esan_gbos.api.v1.work_item.transition",
} as const;

export const BFF_V2_ENDPOINTS = {
  reviewList: "/api/method/esan_gbos.api.v2.review_case.list",
  reviewGet: "/api/method/esan_gbos.api.v2.review_case.get",
  reviewDecide: "/api/method/esan_gbos.api.v2.review_case.decide",
} as const;

type ClientErrorCode =
  | ContractErrorCode
  | "csrf_missing"
  | "offline"
  | "network_error"
  | "schema_mismatch"
  | "invalid_response";

const ERROR_COPY: Record<ClientErrorCode, string> = {
  authentication_required: "登录已失效，请重新登录后再试。",
  permission_denied: "当前角色无权执行此操作。",
  method_not_allowed: "该操作不受支持。",
  invalid_dto: "提交内容不符合要求，请检查后重试。",
  invalid_query: "查询条件无效，请调整后重试。",
  invalid_cursor: "列表位置已失效，请刷新后重试。",
  not_found: "未找到请求的数据，可能已被移除。",
  scope_mismatch: "该数据不在当前团队或站点范围内。",
  revision_conflict: "数据已被他人更新，请刷新后重新操作。",
  invalid_transition: "当前状态不允许执行此操作。",
  idempotency_conflict: "重复请求与原操作不一致，请刷新后重试。",
  request_in_progress: "相同操作正在处理中，请稍后刷新。",
  validation_error: "数据校验失败，请检查后重试。",
  internal_error: "服务暂时不可用，请稍后重试。",
  csrf_missing: "安全会话信息缺失，请刷新页面后重试。",
  offline: "需要联网，请检查网络后重试。",
  network_error: "网络请求失败，请检查连接后重试。",
  schema_mismatch: "服务版本不兼容，请联系管理员。",
  invalid_response: "服务返回了无法识别的数据，请重试。",
};

const containsChinese = (value: string) => /[\u3400-\u9fff]/u.test(value);

export class BffError extends Error {
  readonly code: ClientErrorCode;
  readonly displayMessage: string;
  readonly requestId?: string;
  readonly status?: number;
  readonly details: Record<string, unknown>;

  constructor(
    code: ClientErrorCode,
    options: {
      message?: string;
      requestId?: string;
      status?: number;
      details?: Record<string, unknown>;
    } = {},
  ) {
    const displayMessage =
      options.message && containsChinese(options.message) ? options.message : ERROR_COPY[code];
    super(displayMessage);
    this.name = "BffError";
    this.code = code;
    this.displayMessage = displayMessage;
    this.requestId = options.requestId;
    this.status = options.status;
    this.details = options.details ?? {};
  }
}

export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

interface BffDependencies {
  fetcher?: Fetcher;
  isOnline?: () => boolean;
  getCsrfToken?: () => string;
}

const defaultOnline = () => typeof navigator === "undefined" || navigator.onLine;

const defaultCsrfToken = () => {
  const bootstrap = readGbosBootstrap();
  if (bootstrap?.csrf_token) {
    return bootstrap.csrf_token;
  }
  const host = globalThis as typeof globalThis & {
    frappe?: { csrf_token?: string };
  };
  return host.frappe?.csrf_token ?? "";
};

const defaultFetcher: Fetcher = (input, init) => globalThis.fetch(input, init);

const unwrapFrappePayload = (payload: unknown): unknown => {
  if (isRecord(payload) && "message" in payload) {
    return payload.message;
  }
  return payload;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isContractError = (value: unknown): value is ContractError =>
  isRecord(value) &&
  typeof value.code === "string" &&
  typeof value.message === "string" &&
  typeof value.request_id === "string" &&
  isRecord(value.details);

const normalizeEnvelope = <T>(payload: unknown, status: number): SuccessEnvelope<T> => {
  const value = unwrapFrappePayload(payload);
  if (!isRecord(value) || !("data" in value) || !isRecord(value.meta)) {
    throw new BffError("invalid_response", { status });
  }
  const requestId =
    typeof value.meta.request_id === "string" ? value.meta.request_id : undefined;
  if (value.meta.schema_version !== "1.0") {
    throw new BffError("schema_mismatch", { requestId, status });
  }
  if (!requestId) {
    throw new BffError("invalid_response", { status });
  }
  return value as unknown as SuccessEnvelope<T>;
};

const errorFromPayload = (payload: unknown, status: number): BffError => {
  const value = unwrapFrappePayload(payload);
  const candidate = isRecord(value) && "error" in value ? value.error : undefined;
  if (isContractError(candidate)) {
    return new BffError(candidate.code, {
      message: candidate.message,
      requestId: candidate.request_id,
      status,
      details: candidate.details,
    });
  }
  return new BffError("internal_error", { status });
};

const readJson = async (response: Response): Promise<unknown> => {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
};

const addQuery = (path: string, query: Record<string, string | number | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const suffix = search.toString();
  return suffix ? `${path}?${suffix}` : path;
};

const validateCommandControl = (command: {
  expected_revision: number;
  idempotency_key: string;
}) => {
  if (!Number.isInteger(command.expected_revision) || command.expected_revision < 0) {
    throw new BffError("validation_error", {
      message: "revision 必须是非负整数。",
    });
  }
  if (command.idempotency_key.length < 8 || command.idempotency_key.length > 256) {
    throw new BffError("validation_error", {
      message: "幂等键长度必须为 8 到 256 个字符。",
    });
  }
};

const toFormBody = (command: Record<string, unknown>) => {
  const body = new URLSearchParams();
  for (const [key, value] of Object.entries(command)) {
    if (value !== undefined && value !== null) {
      body.set(
        key,
        Array.isArray(value) || isRecord(value) ? JSON.stringify(value) : String(value),
      );
    }
  }
  return body.toString();
};

export const createIdempotencyKey = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `gbos-${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

export const createBffClient = (dependencies: BffDependencies = {}) => {
  const fetcher = dependencies.fetcher ?? defaultFetcher;
  const isOnline = dependencies.isOnline ?? defaultOnline;
  const getCsrfToken = dependencies.getCsrfToken ?? defaultCsrfToken;

  const request = async <T>(url: string, init: RequestInit): Promise<SuccessEnvelope<T>> => {
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
          ...init.headers,
        },
      });
    } catch (error) {
      if (error instanceof BffError) {
        throw error;
      }
      throw new BffError("network_error");
    }

    const payload = await readJson(response);
    if (!response.ok) {
      throw errorFromPayload(payload, response.status);
    }
    return normalizeEnvelope<T>(payload, response.status);
  };

  const get = <T>(url: string) => request<T>(url, { method: "GET" });

  const post = <T>(url: string, command: Record<string, unknown>) => {
    validateCommandControl(
      command as unknown as { expected_revision: number; idempotency_key: string },
    );
    const csrfToken = getCsrfToken();
    if (!csrfToken) {
      return Promise.reject(new BffError("csrf_missing"));
    }
    return request<T>(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Frappe-CSRF-Token": csrfToken,
      },
      body: toFormBody(command),
    });
  };

  return {
    getParty360: <T = unknown>(party: string) =>
      get<T>(addQuery(BFF_ENDPOINTS.party360, { party })),
    listWorkItems: <T = unknown>(query: WorkItemListQuery = {}) => {
      if (query.pageSize !== undefined && (query.pageSize < 1 || query.pageSize > 50)) {
        throw new Error("page_size 必须在 1 到 50 之间");
      }
      return get<T>(
        addQuery(BFF_ENDPOINTS.workItemList, {
          filters: query.filters ? JSON.stringify(query.filters) : undefined,
          cursor: query.cursor,
          page_size: query.pageSize,
        }),
      );
    },
    getSampleStatus: <T = unknown>(project: string) =>
      get<T>(addQuery(BFF_ENDPOINTS.sampleStatus, { project })),
    getSourcingBoard: <T = unknown>(team?: string) =>
      get<T>(addQuery(BFF_ENDPOINTS.sourcingBoard, { team })),
    createSampleProject: <T = unknown>(command: SampleCreateCommand) =>
      post<T>(BFF_ENDPOINTS.sampleCreate, command as unknown as Record<string, unknown>),
    recordSampleFeedback: <T = unknown>(command: SampleFeedbackCommand) =>
      post<T>(BFF_ENDPOINTS.sampleFeedback, command as unknown as Record<string, unknown>),
    createSourcingFromDemand: <T = unknown>(command: SourcingCreateCommand) =>
      post<T>(BFF_ENDPOINTS.sourcingCreate, command as unknown as Record<string, unknown>),
    transitionWorkItem: <T = unknown>(command: WorkItemTransitionCommand) =>
      post<T>(BFF_ENDPOINTS.workItemTransition, command as unknown as Record<string, unknown>),
    listReviewCases: (query: ReviewCaseListQuery = {}) => {
      if (query.pageSize !== undefined && (query.pageSize < 1 || query.pageSize > 50)) {
        throw new Error("page_size 必须在 1 到 50 之间");
      }
      return get<ReviewCaseListPayload>(
        addQuery(BFF_V2_ENDPOINTS.reviewList, {
          cursor: query.cursor,
          page_size: query.pageSize,
        }),
      );
    },
    getReviewCase: (name: string) =>
      get<ReviewCaseDetailPayload>(addQuery(BFF_V2_ENDPOINTS.reviewGet, { name })),
    decideReviewCase: (command: ReviewDecisionCommand) =>
      post<ReviewCaseDetailPayload>(
        BFF_V2_ENDPOINTS.reviewDecide,
        command as unknown as Record<string, unknown>,
      ),
  };
};

export type BffClient = ReturnType<typeof createBffClient>;
