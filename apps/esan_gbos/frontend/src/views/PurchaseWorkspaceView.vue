<template>
  <section class="view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="ESAN GBOS · Gate 4"
          title="采购询源工作台"
          description="按真实询源阶段查看事件和候选供应商报价快照，仅呈现服务端原始字段。"
        >
          <template #actions>
            <GbosButton data-refresh intent="secondary" @click="load">
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
          :empty="isEmpty"
          @retry="load"
        >
          <SourcingComparison :lanes="lanes" />
        </ResourceBoundary>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import { useBffClient } from "@/api/injection";
import SourcingComparison from "@/components/data/SourcingComparison.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { sourcingLanesFromPayload } from "@/presentation";

const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getSourcingBoard();
  return sourcingLanesFromPayload(response.data);
});
const lanes = computed(() => resource.data.value ?? []);
const isEmpty = computed(() =>
  lanes.value.every((lane) => lane.events.length === 0),
);
const { state, message, requestId, load } = resource;
</script>
