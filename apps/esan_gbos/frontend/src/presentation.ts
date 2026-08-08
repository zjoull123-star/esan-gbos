const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const recordsAt = (
  value: Record<string, unknown>,
  key: string,
): Record<string, unknown>[] => {
  const candidate = value[key];
  return Array.isArray(candidate) ? candidate.filter(isRecord) : [];
};

const withSection = (
  record: Record<string, unknown>,
  section: string,
  extra: Record<string, unknown> = {},
): Record<string, unknown> => ({
  ...record,
  ...extra,
  presentation_section: section,
});

export const isFixturePayload = (value: unknown, depth = 0): boolean => {
  if (depth > 6) {
    return false;
  }
  if (Array.isArray(value)) {
    return value.some((item) => isFixturePayload(item, depth + 1));
  }
  if (!isRecord(value)) {
    return false;
  }
  if (value.origin === "Fixture") {
    return true;
  }
  return Object.values(value).some((item) => isFixturePayload(item, depth + 1));
};

export const recordsFromPayload = (value: unknown): Record<string, unknown>[] => {
  if (Array.isArray(value)) {
    return value.filter(isRecord);
  }
  if (!isRecord(value)) {
    return [];
  }
  for (const key of ["items", "rows", "results", "work_items", "events"]) {
    const candidate = value[key];
    if (Array.isArray(candidate)) {
      return candidate.filter(isRecord);
    }
  }
  return Object.keys(value).length ? [value] : [];
};

export const flattenSampleStatusPayload = (
  value: unknown,
): {
  records: Record<string, unknown>[];
  revision: number | undefined;
  businessStatus: string | undefined;
} => {
  if (!isRecord(value) || !isRecord(value.project)) {
    const records = recordsFromPayload(value);
    const project = records[0];
    return {
      records,
      revision: project ? numberField(project, "revision") : undefined,
      businessStatus: project
        ? textField(project, "business_status", "status")
        : undefined,
    };
  }

  const project = value.project;
  const sections = [
    { key: "iterations", label: "样品迭代" },
    { key: "shipments", label: "寄样记录" },
    { key: "feedback", label: "客户反馈" },
  ] as const;
  const records = [withSection(project, "样品项目")];
  for (const section of sections) {
    records.push(
      ...recordsAt(value, section.key).map((record) =>
        withSection(record, section.label),
      ),
    );
  }

  return {
    records,
    revision: numberField(project, "revision"),
    businessStatus: textField(project, "business_status", "status"),
  };
};

const SOURCING_LANES = [
  ["Draft", "草稿"],
  ["Invited", "已邀请"],
  ["Collecting", "收集中"],
  ["Evaluating", "评估中"],
  ["Selected", "已选定"],
  ["Closed", "已关闭"],
  ["Cancelled", "已取消"],
] as const;

export const flattenSourcingBoardPayload = (
  value: unknown,
): { records: Record<string, unknown>[]; total: number | undefined } => {
  if (!isRecord(value) || !isRecord(value.lanes)) {
    return {
      records: recordsFromPayload(value),
      total: isRecord(value) ? numberField(value, "total") : undefined,
    };
  }

  const records: Record<string, unknown>[] = [];
  for (const [lane, label] of SOURCING_LANES) {
    for (const event of recordsAt(value.lanes, lane)) {
      records.push(withSection(event, label, { sourcing_lane: lane }));
      const eventName = textField(event, "name");
      records.push(
        ...recordsAt(event, "candidates").map((candidate) =>
          withSection(candidate, `${label} · 候选供应商`, {
            sourcing_event: eventName,
            sourcing_lane: lane,
          }),
        ),
      );
    }
  }
  return { records, total: numberField(value, "total") };
};

const PARTY_SINGLETON_SECTIONS = [
  ["profile", "客户档案"],
  ["organization", "组织"],
  ["contact", "联系人"],
  ["lead", "销售线索"],
  ["deal", "商机"],
] as const;

const PARTY_COLLECTION_SECTIONS = [
  ["product_briefs", "产品简报"],
  ["samples", "样品项目"],
  ["demands", "客户需求"],
] as const;

export const flattenParty360Payload = (
  value: unknown,
): { records: Record<string, unknown>[] } => {
  if (
    !isRecord(value) ||
    ![...PARTY_SINGLETON_SECTIONS, ...PARTY_COLLECTION_SECTIONS].some(
      ([key]) => key in value,
    )
  ) {
    return { records: recordsFromPayload(value) };
  }

  const records: Record<string, unknown>[] = [];
  for (const [key, label] of PARTY_SINGLETON_SECTIONS) {
    const record = value[key];
    if (isRecord(record)) {
      records.push(withSection(record, label));
    }
  }
  for (const [key, label] of PARTY_COLLECTION_SECTIONS) {
    records.push(
      ...recordsAt(value, key).map((record) => withSection(record, label)),
    );
  }
  return { records };
};

export const textField = (
  record: Record<string, unknown>,
  ...fields: string[]
): string | undefined => {
  for (const field of fields) {
    const value = record[field];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return undefined;
};

export const numberField = (
  record: Record<string, unknown>,
  field: string,
): number | undefined => {
  const value = record[field];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
};

export interface WorkItemPresentation {
  name?: string;
  title?: string;
  team?: string;
  assigned_to?: string;
  priority?: string;
  due_date?: string;
  origin?: string;
  business_status?: string;
  review_status?: string;
  revision?: number;
  reference_doctype?: string;
  reference_name?: string;
  modified?: string;
}

export interface SourcingCandidatePresentation {
  supplier_name?: string | null;
  external_supplier_id?: string | null;
  quoted_price?: number | null;
  currency?: string | null;
  lead_time_days?: number | null;
  candidate_status?: string | null;
  notes?: string | null;
}

export interface SourcingEventPresentation {
  name?: string;
  title?: string;
  team?: string;
  demand_signal?: string;
  selected_supplier?: string;
  owner_user?: string;
  origin?: string;
  business_status?: string;
  review_status?: string;
  revision?: number;
  modified?: string;
  candidates: readonly SourcingCandidatePresentation[];
}

export interface SourcingLanePresentation {
  key: (typeof SOURCING_LANES)[number][0];
  label: (typeof SOURCING_LANES)[number][1];
  events: readonly SourcingEventPresentation[];
}

const projectTextFields = <T extends readonly string[]>(
  record: Record<string, unknown>,
  fields: T,
) =>
  Object.fromEntries(
    fields.map((field) => [field, textField(record, field)]),
  ) as Record<T[number], string | undefined>;

export const workItemsFromPayload = (value: unknown): WorkItemPresentation[] => {
  const records = Array.isArray(value)
    ? value.filter(isRecord)
    : isRecord(value)
      ? recordsAt(value, "items")
      : [];
  return records.map((record) => ({
    ...projectTextFields(record, [
      "name",
      "title",
      "team",
      "assigned_to",
      "priority",
      "due_date",
      "origin",
      "business_status",
      "review_status",
      "reference_doctype",
      "reference_name",
      "modified",
    ] as const),
    revision: numberField(record, "revision"),
  }));
};

export const workItemReferenceLink = (
  item: WorkItemPresentation,
): { href: string; label: string } | undefined => {
  if (!item.reference_name) {
    return undefined;
  }
  if (item.reference_doctype === "GBOS Party Profile") {
    return {
      href: `/gbos/party/${encodeURIComponent(item.reference_name)}`,
      label: "查看相关客户",
    };
  }
  if (item.reference_doctype === "GBOS Sample Project") {
    return {
      href: `/gbos/sample/${encodeURIComponent(item.reference_name)}`,
      label: "查看相关样品",
    };
  }
  return undefined;
};

const sourcingCandidateFromRecord = (
  record: Record<string, unknown>,
): SourcingCandidatePresentation => ({
  ...projectTextFields(record, [
    "supplier_name",
    "external_supplier_id",
    "currency",
    "candidate_status",
    "notes",
  ] as const),
  quoted_price: numberField(record, "quoted_price"),
  lead_time_days: numberField(record, "lead_time_days"),
});

const sourcingEventFromRecord = (
  record: Record<string, unknown>,
): SourcingEventPresentation => ({
  ...projectTextFields(record, [
    "name",
    "title",
    "team",
    "demand_signal",
    "selected_supplier",
    "owner_user",
    "origin",
    "business_status",
    "review_status",
    "modified",
  ] as const),
  revision: numberField(record, "revision"),
  candidates: recordsAt(record, "candidates").map(sourcingCandidateFromRecord),
});

export const sourcingLanesFromPayload = (
  value: unknown,
): SourcingLanePresentation[] => {
  const lanes = isRecord(value) && isRecord(value.lanes) ? value.lanes : {};
  return SOURCING_LANES.map(([key, label]) => ({
    key,
    label,
    events: recordsAt(lanes, key).map(sourcingEventFromRecord),
  }));
};

export const formatQuotedPrice = (
  candidate: SourcingCandidatePresentation,
): string => {
  if (candidate.quoted_price === undefined || candidate.quoted_price === null) {
    return "";
  }
  return candidate.currency
    ? `${candidate.quoted_price} ${candidate.currency}`
    : String(candidate.quoted_price);
};

export const formatLeadTimeDays = (
  candidate: SourcingCandidatePresentation,
): string =>
  candidate.lead_time_days === undefined || candidate.lead_time_days === null
    ? ""
    : `${candidate.lead_time_days} 天`;
