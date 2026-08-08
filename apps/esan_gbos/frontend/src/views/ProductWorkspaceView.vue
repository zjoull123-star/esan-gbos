<template>
  <section class="view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="ESAN GBOS · Gate 4"
          title="产品与样品工作项"
          description="仅展示当前权限范围内的工作项，不将工作项列表描述为产品简报或样品索引。"
        >
          <template #actions>
            <GbosButton
              v-if="cursor"
              data-first-page
              intent="secondary"
              @click="goFirstPage"
            >
              回到首页
            </GbosButton>
            <GbosButton data-refresh intent="secondary" @click="refresh">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="items.length === 0"
          @retry="refresh"
        >
          <div data-work-items>
            <DemoBanner v-if="hasFixtureData" />
            <OperationalList :columns="columns" :rows="rows" />
          </div>
        </ResourceBoundary>
      </template>

      <template #pagination>
        <GbosButton
          v-if="state === 'ready' && nextCursor"
          data-next-page
          intent="secondary"
          @click="goNextPage"
        >
          下一页
        </GbosButton>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { useBffClient } from "@/api/injection";
import DemoBanner from "@/components/DemoBanner.vue";
import OperationalList, {
  type OperationalColumn,
  type OperationalRow,
} from "@/components/data/OperationalList.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { isFixturePayload, workItemsFromPayload } from "@/presentation";

const columns = [
  { key: "title", label: "工作项 / 下一动作" },
  { key: "name", label: "编号" },
  { key: "team", label: "团队" },
  { key: "assigned_to", label: "负责人" },
  { key: "priority", label: "优先级" },
  { key: "due_date", label: "到期日" },
  { key: "business_status", label: "业务状态" },
  { key: "review_status", label: "审核状态" },
  { key: "revision", label: "版本" },
  { key: "reference", label: "相关记录" },
  { key: "modified", label: "更新时间" },
] as const satisfies readonly OperationalColumn[];

const client = useBffClient();
const cursor = ref<string>();
const resource = useOnlineResource(async () => {
  const response = await client.listWorkItems({
    cursor: cursor.value,
    pageSize: 25,
  });
  return {
    items: workItemsFromPayload(response.data),
    nextCursor: response.meta.next_cursor ?? undefined,
  };
});
const items = computed(() => resource.data.value?.items ?? []);
const nextCursor = computed(() => resource.data.value?.nextCursor);
const rows = computed<OperationalRow[]>(() =>
  items.value.map((item, index) => ({
    id: item.name ?? `work-item-${index}`,
    values: {
      title: item.title,
      name: item.name,
      team: item.team,
      assigned_to: item.assigned_to,
      priority: item.priority,
      due_date: item.due_date,
      business_status: item.business_status,
      review_status: item.review_status,
      revision: item.revision,
      reference: [item.reference_doctype, item.reference_name]
        .filter(Boolean)
        .join(" · "),
      modified: item.modified,
    },
  })),
);
const hasFixtureData = computed(() => isFixturePayload(items.value));
const { state, message, requestId, load } = resource;

const refresh = () => load();
const goFirstPage = () => {
  cursor.value = undefined;
  return load();
};
const goNextPage = () => {
  if (!nextCursor.value) {
    return;
  }
  cursor.value = nextCursor.value;
  return load();
};
</script>
