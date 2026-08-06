<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          ESAN GBOS · Gate 1
        </p>
        <h1>{{ config.title }}</h1>
        <p>{{ config.description }}</p>
      </div>
      <button class="button button--secondary" type="button" @click="load">
        刷新
      </button>
    </header>

    <StatePanel v-if="state === 'loading' || state === 'idle'" kind="loading" />
    <StatePanel
      v-else-if="state === 'offline'"
      kind="offline"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'permission'"
      kind="permission"
      :message="message"
      @retry="load"
    />
    <StatePanel
      v-else-if="state === 'error'"
      kind="error"
      :message="message"
      :request-id="requestId"
      @retry="load"
    />
    <StatePanel
      v-else-if="records.length === 0"
      kind="empty"
      @retry="load"
    />
    <RecordGrid v-else :records="records" />
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import RecordGrid from "@/components/RecordGrid.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import {
  flattenSourcingBoardPayload,
  recordsFromPayload,
} from "@/presentation";

type Workspace = "ceo" | "sales" | "purchase" | "product" | "review";

const props = defineProps<{ workspace: Workspace }>();
const client = useBffClient();

const WORKSPACES = {
  ceo: {
    title: "经营总览",
    description: "查看跨团队工作项、到期风险和审核结果。",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
  sales: {
    title: "销售协同",
    description: "跟进客户、需求、样品和销售工作项。",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
  purchase: {
    title: "采购协同",
    description: "查看授权需求摘要、候选供应商和询源进度。",
    load: () => client.getSourcingBoard(),
  },
  product: {
    title: "产品与样品",
    description: "协同 Product Brief、样品迭代、寄样与反馈。",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
  review: {
    title: "审核队列",
    description:
      "Gate 1 为只读审核队列，仅显示后端授权给当前审核人的关联工作项；正式审核命令将在 Gate 4 提供。",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
} satisfies Record<Workspace, { title: string; description: string; load: () => Promise<unknown> }>;

const config = computed(() => WORKSPACES[props.workspace]);
const resource = useOnlineResource(async () => {
  const response = await config.value.load();
  return response.data;
});
const records = computed(() =>
  props.workspace === "purchase"
    ? flattenSourcingBoardPayload(resource.data.value).records
    : recordsFromPayload(resource.data.value),
);
const { state, message, requestId, load } = resource;
watch(
  () => props.workspace,
  () => {
    void load();
  },
);
</script>
