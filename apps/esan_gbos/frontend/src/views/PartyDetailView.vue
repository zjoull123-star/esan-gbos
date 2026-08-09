<template>
  <section class="view">
    <DetailCommandTemplate>
      <template #header>
        <PageHeader
          :eyebrow="`客户编号 · ${id}`"
          title="客户 360"
          description="仅显示当前 Frappe session 获授权的客户、CRM、产品、样品与需求上下文。"
        >
          <template #actions>
            <GbosButton intent="secondary" @click="load">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template v-if="profile" #facts>
        <ObjectSummary
          title="客户档案"
          :eyebrow="text(profile, 'name')"
          :fields="profileFields(profile)"
        />
      </template>

      <template #main>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="!hasPartyData"
          @retry="load"
        >
          <div class="party-detail">
            <DemoBanner v-if="hasFixtureData" />

            <section class="party-section" aria-labelledby="party-crm-title">
              <h2 id="party-crm-title">
                CRM 上下文
              </h2>
              <div v-if="crmSummaries.length" class="party-summary-grid">
                <ObjectSummary
                  v-for="summary in crmSummaries"
                  :key="summary.key"
                  :title="summary.title"
                  :eyebrow="summary.eyebrow"
                  :fields="summary.fields"
                  :heading-level="3"
                />
              </div>
              <p v-else class="party-section__empty">
                暂无获授权的 CRM 关联记录。
              </p>
            </section>

            <section class="party-section" aria-labelledby="party-briefs-title">
              <h2 id="party-briefs-title">
                产品简报
              </h2>
              <div v-if="productBriefs.length" class="party-summary-grid">
                <ObjectSummary
                  v-for="(brief, index) in productBriefs"
                  :key="text(brief, 'name') || `brief-${index}`"
                  :title="text(brief, 'title') || text(brief, 'name') || '产品简报'"
                  :eyebrow="text(brief, 'name')"
                  :fields="businessFields(brief)"
                  :heading-level="3"
                />
              </div>
              <p v-else class="party-section__empty">
                暂无产品简报。
              </p>
            </section>

            <section class="party-section" aria-labelledby="party-samples-title">
              <h2 id="party-samples-title">
                样品项目
              </h2>
              <div v-if="samples.length" class="party-summary-grid">
                <ObjectSummary
                  v-for="(sample, index) in samples"
                  :key="text(sample, 'name') || `sample-${index}`"
                  :title="text(sample, 'title') || text(sample, 'name') || '样品项目'"
                  :eyebrow="text(sample, 'name')"
                  :fields="sampleFields(sample)"
                  :heading-level="3"
                />
              </div>
              <p v-else class="party-section__empty">
                暂无样品项目。
              </p>
            </section>

            <section class="party-section" aria-labelledby="party-demands-title">
              <h2 id="party-demands-title">
                客户需求
              </h2>
              <div v-if="demands.length" class="party-summary-grid">
                <ObjectSummary
                  v-for="(demand, index) in demands"
                  :key="text(demand, 'name') || `demand-${index}`"
                  :title="text(demand, 'title') || text(demand, 'name') || '客户需求'"
                  :eyebrow="text(demand, 'name')"
                  :fields="businessFields(demand)"
                  :heading-level="3"
                />
              </div>
              <p v-else class="party-section__empty">
                暂无客户需求。
              </p>
            </section>
          </div>
        </ResourceBoundary>
      </template>
    </DetailCommandTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import DemoBanner from "@/components/DemoBanner.vue";
import ObjectSummary, {
  type ObjectSummaryField,
} from "@/components/data/ObjectSummary.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { isFixturePayload } from "@/presentation";

type DataRecord = Record<string, unknown>;

const props = defineProps<{ id: string }>();
const client = useBffClient();
const isRecord = (value: unknown): value is DataRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (record: DataRecord, key: string) => {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};
const number = (record: DataRecord, key: string) => {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
};
const recordAt = (record: DataRecord | undefined, key: string) => {
  const value = record?.[key];
  return isRecord(value) ? value : undefined;
};
const recordsAt = (record: DataRecord | undefined, key: string) => {
  const value = record?.[key];
  return Array.isArray(value) ? value.filter(isRecord) : [];
};
const field = (
  key: string,
  label: string,
  value: string | number | undefined,
  to?: string,
): ObjectSummaryField => ({ key, label, value, to });

const resource = useOnlineResource(async () => {
  const response = await client.getParty360(props.id);
  return isRecord(response.data) ? response.data : undefined;
});
const payload = computed(() => resource.data.value);
const profile = computed(() => recordAt(payload.value, "profile"));
const organization = computed(() => recordAt(payload.value, "organization"));
const contact = computed(() => recordAt(payload.value, "contact"));
const lead = computed(() => recordAt(payload.value, "lead"));
const deal = computed(() => recordAt(payload.value, "deal"));
const productBriefs = computed(() => recordsAt(payload.value, "product_briefs"));
const samples = computed(() => recordsAt(payload.value, "samples"));
const demands = computed(() => recordsAt(payload.value, "demands"));
const hasPartyData = computed(
  () =>
    Boolean(profile.value) ||
    Boolean(organization.value) ||
    Boolean(contact.value) ||
    Boolean(lead.value) ||
    Boolean(deal.value) ||
    productBriefs.value.length > 0 ||
    samples.value.length > 0 ||
    demands.value.length > 0,
);
const hasFixtureData = computed(() => isFixturePayload(payload.value));

const profileFields = (record: DataRecord): ObjectSummaryField[] => [
  field("party_name", "客户名称", text(record, "party_name")),
  field("team", "团队", text(record, "team")),
  field("origin", "来源", text(record, "origin")),
  field("business_status", "业务状态", text(record, "business_status")),
  field("review_status", "审核状态", text(record, "review_status")),
  field("revision", "版本", number(record, "revision")),
  field("modified", "更新时间", text(record, "modified")),
];
const businessFields = (record: DataRecord): ObjectSummaryField[] => [
  field("deal", "关联商机", text(record, "deal")),
  field("origin", "来源", text(record, "origin")),
  field("business_status", "业务状态", text(record, "business_status")),
  field("review_status", "审核状态", text(record, "review_status")),
  field("revision", "版本", number(record, "revision")),
];
const sampleFields = (record: DataRecord): ObjectSummaryField[] => {
  const name = text(record, "name");
  return [
    field(
      "sample_link",
      "详情",
      text(record, "title") || name,
      name ? `/gbos/sample/${encodeURIComponent(name)}` : undefined,
    ),
    ...businessFields(record),
  ];
};

const crmSummaries = computed(() => {
  const summaries: Array<{
    key: string;
    title: string;
    eyebrow?: string;
    fields: ObjectSummaryField[];
  }> = [];
  if (organization.value) {
    summaries.push({
      key: "organization",
      title:
        text(organization.value, "organization_name") ||
        text(organization.value, "name") ||
        "组织",
      eyebrow: "组织",
      fields: [
        field("name", "编号", text(organization.value, "name")),
        field("website", "网站", text(organization.value, "website")),
        field("territory", "区域", text(organization.value, "territory")),
        field("industry", "行业", text(organization.value, "industry")),
      ],
    });
  }
  if (contact.value) {
    summaries.push({
      key: "contact",
      title: text(contact.value, "full_name") || text(contact.value, "name") || "联系人",
      eyebrow: "联系人",
      fields: [
        field("name", "编号", text(contact.value, "name")),
        field("email_id", "邮箱", text(contact.value, "email_id")),
        field("mobile_no", "电话", text(contact.value, "mobile_no")),
      ],
    });
  }
  if (lead.value) {
    summaries.push({
      key: "lead",
      title: text(lead.value, "lead_name") || text(lead.value, "name") || "销售线索",
      eyebrow: "销售线索",
      fields: [
        field("name", "编号", text(lead.value, "name")),
        field("organization", "组织", text(lead.value, "organization")),
        field("status", "状态", text(lead.value, "status")),
        field("lead_owner", "负责人", text(lead.value, "lead_owner")),
      ],
    });
  }
  if (deal.value) {
    summaries.push({
      key: "deal",
      title: text(deal.value, "name") || "商机",
      eyebrow: "商机",
      fields: [
        field("organization", "组织", text(deal.value, "organization")),
        field("status", "状态", text(deal.value, "status")),
        field("deal_owner", "负责人", text(deal.value, "deal_owner")),
        field(
          "expected_deal_value",
          "预计金额",
          number(deal.value, "expected_deal_value"),
        ),
      ],
    });
  }
  return summaries;
});
const { state, message, requestId, load } = resource;

watch(
  () => props.id,
  () => void load(),
);
</script>

<style scoped>
.party-detail,
.party-section,
.party-summary-grid {
  display: grid;
  min-width: 0;
  gap: 12px;
}

.party-section {
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-canvas);
}

.party-section > h2,
.party-section__empty {
  margin: 0;
}

.party-section > h2 {
  color: var(--gbos-text);
  font-size: 18px;
}

.party-summary-grid {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr));
}

.party-section__empty {
  color: var(--gbos-muted);
  font-size: 13px;
}
</style>
