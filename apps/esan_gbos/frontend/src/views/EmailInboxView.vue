<template>
  <section class="email-inbox-view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="CRM · GOVERNED EMAIL"
          title="统一邮件收件箱"
          description="按授权团队展示安全投影；身份、分配、会话建议与草稿均由人工控制。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="load">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #filters>
        <InboxQueueTabs :model-value="selectedQueue" @update:model-value="selectQueue" />
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="boundaryMessage"
          :request-id="requestId"
          :empty="items.length === 0"
          @retry="load"
        >
          <InboxItemList :items="items" />
        </ResourceBoundary>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { useEmailGatewayClient } from "@/api/email-gateway";
import type { EmailInboxListQuery, EmailInboxQueue, EmailInboxState } from "@/api/email-gateway-types";
import InboxItemList from "@/components/email/InboxItemList.vue";
import InboxQueueTabs from "@/components/email/InboxQueueTabs.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useEmailGatewayClient();
const selectedQueue = ref<EmailInboxQueue>("all");
const queueState: Partial<Record<EmailInboxQueue, EmailInboxState>> = {
  identity_pending: "identity_pending", unassigned: "unassigned", draft: "draft",
  send_failure_uncertain: "send_uncertain", waiting_customer: "waiting_customer",
  waiting_internal: "waiting_internal", converted: "converted", closed: "closed", quarantine: "quarantined",
};
const query = computed<EmailInboxListQuery>(() => ({
  state: queueState[selectedQueue.value],
  sort: selectedQueue.value === "first_reply_due" ? "sla_due_at_asc" : "received_at_desc",
  pageSize: 25,
}));
const resource = useOnlineResource(async () => (await client.listInbox(query.value)).data);
const items = computed(() => resource.data.value?.inbox_items ?? []);
const boundaryMessage = computed(() => resource.state.value === "ready" && items.value.length === 0
  ? "当前队列没有可展示的安全邮件摘要。" : resource.message.value);
const { state, requestId, load } = resource;
const selectQueue = (queue: EmailInboxQueue) => { selectedQueue.value = queue; void load(); };
</script>

<style scoped>
.email-inbox-view { min-width: 0; }
@media (max-width: 767px) { .email-inbox-view { width: 100%; } }
</style>
