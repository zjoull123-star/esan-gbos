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
            <GbosButton intent="secondary" type="button" @click="reload">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #filters>
        <InboxQueueTabs :model-value="selectedQueue" @update:model-value="selectQueue" />
        <p v-if="selectedQueue === 'send_failure_uncertain'" class="queue-seam" role="status">
          当前接口只提供发送结果不确定状态；发送失败队列字段尚未提供。
        </p>
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="boundaryMessage"
          :request-id="requestId"
          :empty="items.length === 0"
          @retry="reload"
        >
          <InboxItemList :items="items" />
        </ResourceBoundary>
        <p v-if="paginationMessage" class="pagination-error" role="alert">
          {{ paginationMessage }}
        </p>
        <GbosButton
          v-if="nextCursor"
          data-inbox-next-page
          intent="secondary"
          type="button"
          :disabled="loadingMore"
          @click="loadMore"
        >
          {{ loadingMore ? "正在加载…" : "加载更多" }}
        </GbosButton>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { BffError } from "@/api/bff";
import { useEmailGatewayClient } from "@/api/email-gateway";
import type { EmailInboxItem, EmailInboxListQuery, EmailInboxQueue, EmailInboxState } from "@/api/email-gateway-types";
import InboxItemList from "@/components/email/InboxItemList.vue";
import InboxQueueTabs from "@/components/email/InboxQueueTabs.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useEmailGatewayClient();
const selectedQueue = ref<EmailInboxQueue>("all");
const appendedItems = ref<EmailInboxItem[]>([]);
const nextCursorOverride = ref<string | null>();
const loadingMore = ref(false);
const paginationMessage = ref("");
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
const items = computed(() => [...(resource.data.value?.inbox_items ?? []), ...appendedItems.value]);
const nextCursor = computed(() => nextCursorOverride.value === undefined
  ? resource.data.value?.next_cursor ?? null
  : nextCursorOverride.value);
const boundaryMessage = computed(() => resource.state.value === "ready" && items.value.length === 0
  ? "当前队列没有可展示的安全邮件摘要。" : resource.message.value);
const { state, requestId } = resource;
const resetPagination = () => {
  appendedItems.value = [];
  nextCursorOverride.value = undefined;
  paginationMessage.value = "";
};
const reload = () => {
  resetPagination();
  void resource.load();
};
const selectQueue = (queue: EmailInboxQueue) => {
  selectedQueue.value = queue;
  reload();
};
const loadMore = async () => {
  const cursor = nextCursor.value;
  if (!cursor || loadingMore.value) return;
  const queue = selectedQueue.value;
  loadingMore.value = true;
  paginationMessage.value = "";
  try {
    const response = await client.listInbox({ ...query.value, cursor });
    if (queue !== selectedQueue.value) return;
    const known = new Set(items.value.map((item) => item.inbox_item_ref));
    if (response.data.inbox_items.some((item) => known.has(item.inbox_item_ref))) {
      paginationMessage.value = "分页结果与当前列表重复，请刷新后重试。";
      nextCursorOverride.value = null;
      return;
    }
    appendedItems.value = [...appendedItems.value, ...response.data.inbox_items];
    nextCursorOverride.value = response.data.next_cursor;
  } catch (error) {
    paginationMessage.value = error instanceof BffError
      ? error.displayMessage
      : "下一页暂时不可用，请稍后重试。";
  } finally {
    loadingMore.value = false;
  }
};
</script>

<style scoped>
.email-inbox-view { min-width: 0; }
.queue-seam { margin: 8px 0 0; color: var(--gbos-muted); }
.pagination-error { padding: 10px 12px; color: var(--gbos-danger-text); background: var(--gbos-danger-soft); }
@media (max-width: 767px) { .email-inbox-view { width: 100%; } }
</style>
