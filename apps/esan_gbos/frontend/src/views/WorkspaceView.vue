<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          ESAN GBOS · {{ config.gate }}
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
    <StatePanel v-else-if="isEmpty" kind="empty" @retry="load" />
    <MetricCockpit
      v-else-if="workspace === 'ceo' && dashboard"
      :dashboard="dashboard"
    />
    <RecordGrid v-else :records="records" />
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import type { MetricDashboardPayload } from "@/api/types";
import MetricCockpit from "@/components/MetricCockpit.vue";
import RecordGrid from "@/components/RecordGrid.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import {
  flattenSourcingBoardPayload,
  recordsFromPayload,
} from "@/presentation";

type Workspace = "ceo" | "sales" | "purchase" | "product";

const props = defineProps<{ workspace: Workspace }>();
const client = useBffClient();

const WORKSPACES = {
  ceo: {
    title: "经营总览",
    description: "查看经过新鲜度、覆盖率、对账和来源链路治理的经营指标。",
    gate: "Gate 5",
    load: () => client.getMetricDashboard(),
  },
  sales: {
    title: "销售协同",
    description: "跟进客户、需求、样品和销售工作项。",
    gate: "Gate 4",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
  purchase: {
    title: "采购协同",
    description: "查看授权需求摘要、候选供应商和询源进度。",
    gate: "Gate 4",
    load: () => client.getSourcingBoard(),
  },
  product: {
    title: "产品与样品",
    description: "协同 Product Brief、样品迭代、寄样与反馈。",
    gate: "Gate 4",
    load: () => client.listWorkItems({ pageSize: 25 }),
  },
} satisfies Record<
  Workspace,
  { title: string; description: string; gate: string; load: () => Promise<unknown> }
>;

const config = computed(() => WORKSPACES[props.workspace]);
const resource = useOnlineResource(async () => {
  const response = await config.value.load();
  return response.data;
});
const dashboard = computed(() =>
  props.workspace === "ceo"
    ? (resource.data.value as MetricDashboardPayload | undefined)
    : undefined,
);
const records = computed(() =>
  props.workspace === "ceo"
    ? []
    : props.workspace === "purchase"
    ? flattenSourcingBoardPayload(resource.data.value).records
    : recordsFromPayload(resource.data.value),
);
const isEmpty = computed(() =>
  props.workspace === "ceo"
    ? dashboard.value?.metrics.length === 0
    : records.value.length === 0,
);
const { state, message, requestId, load } = resource;
watch(
  () => props.workspace,
  () => {
    void load();
  },
);
</script>
