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
    records.push(
      ...recordsAt(value.lanes, lane).map((record) =>
        withSection(record, label, { sourcing_lane: lane }),
      ),
    );
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
