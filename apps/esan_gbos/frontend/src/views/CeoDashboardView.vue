<template>
  <section class="view ceo-dashboard">
    <DashboardTemplate>
      <template #header>
        <PageHeader
          title="经营总览"
          eyebrow="ESAN GBOS · Gate 5"
          description="查看经过新鲜度、覆盖率、对账和来源链路治理的经营指标。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="load">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #metrics>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="dashboard?.metrics.length === 0"
          @retry="load"
        >
          <MetricCockpit v-if="dashboard" :dashboard="dashboard" />
        </ResourceBoundary>
      </template>
    </DashboardTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import { useBffClient } from "@/api/injection";
import MetricCockpit from "@/components/MetricCockpit.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DashboardTemplate from "@/components/layout/DashboardTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getMetricDashboard();
  return response.data;
});
const dashboard = computed(() => resource.data.value);
const { state, message, requestId, load } = resource;
</script>
