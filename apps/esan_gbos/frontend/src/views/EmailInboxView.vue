<template>
  <section class="email-inbox-view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="CRM · PHASE 1"
          title="统一邮件收件箱"
          description="只展示受控摘要和当前处理状态；本阶段不开放认领、会话变更、回复或外发操作。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="load">
              刷新
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #filters>
        <form class="email-inbox-filters" @submit.prevent="applyFilter">
          <label>
            处理状态
            <select v-model="draftState">
              <option value="">全部安全状态</option>
              <option value="identity_pending">身份待确认</option>
              <option value="unassigned">待分配</option>
            </select>
          </label>
          <GbosButton type="submit">
            应用筛选
          </GbosButton>
        </form>
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="boundaryMessage"
          :request-id="requestId"
          :empty="items.length === 0"
          @retry="load"
        >
          <div class="email-inbox-layout">
            <ul class="email-inbox-list" aria-label="邮件安全摘要">
              <li v-for="item in items" :key="item.inbox_item_ref" class="email-inbox-card">
                <div class="email-inbox-card__heading">
                  <div>
                    <p>{{ modeLabel(item.mailbox_role) }}</p>
                    <h2>{{ item.mailbox_label }}</h2>
                  </div>
                  <span>{{ stateLabel(item.state) }}</span>
                </div>
                <p class="email-inbox-card__summary">
                  {{ item.safe_summary }}
                </p>
                <dl>
                  <div><dt>团队</dt><dd>{{ item.team_label || "待确定" }}</dd></div>
                  <div><dt>接收时间</dt><dd>{{ item.received_at }}</dd></div>
                </dl>
                <GbosButton
                  data-inbox-detail
                  intent="secondary"
                  type="button"
                  @click="openDetail(item.inbox_item_ref)"
                >
                  查看安全详情
                </GbosButton>
              </li>
            </ul>

            <aside
              v-if="detail"
              class="email-inbox-detail"
              aria-labelledby="email-inbox-detail-title"
            >
              <p>{{ modeLabel(detail.mailbox_role) }}</p>
              <h2 id="email-inbox-detail-title">
                {{ detail.mailbox_label }}
              </h2>
              <p>{{ detail.safe_summary }}</p>
              <dl>
                <div><dt>状态</dt><dd>{{ stateLabel(detail.state) }}</dd></div>
                <div><dt>身份</dt><dd>{{ identityLabel(detail.identity_state) }}</dd></div>
                <div><dt>团队</dt><dd>{{ detail.team_label || "待确定" }}</dd></div>
                <div><dt>当前负责人</dt><dd>{{ detail.assignee_label || "未分配" }}</dd></div>
                <div><dt>接收时间</dt><dd>{{ detail.received_at }}</dd></div>
                <div><dt>版本</dt><dd>{{ detail.revision }}</dd></div>
              </dl>
            </aside>
          </div>
        </ResourceBoundary>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

import { useEmailGatewayClient } from "@/api/email-gateway";
import type {
  EmailBusinessMode,
  EmailIdentityState,
  EmailInboxDetail,
  EmailInboxState,
} from "@/api/email-gateway-types";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useEmailGatewayClient();
const selectedState = ref<EmailInboxState>();
const draftState = ref<EmailInboxState | "">("");
const detail = ref<EmailInboxDetail>();
const resource = useOnlineResource(async () => {
  detail.value = undefined;
  const response = await client.listInbox({ state: selectedState.value, pageSize: 25 });
  return response.data;
});
const items = computed(() => resource.data.value?.inbox_items ?? []);
const boundaryMessage = computed(() =>
  resource.state.value === "ready" && items.value.length === 0
    ? "当前没有可展示的安全邮件摘要。"
    : resource.message.value,
);
const { state, requestId, load } = resource;

const stateLabel = (value: EmailInboxState) =>
  ({ identity_pending: "身份待确认", unassigned: "待分配" })[value];
const modeLabel = (value: EmailBusinessMode) =>
  ({ primary: "主入口", selective_archive: "选择性归档", migration: "迁移邮箱" })[
    value
  ];
const identityLabel = (value: EmailIdentityState) =>
  ({ unknown: "未知", confirmed: "已确认", revoked: "已撤销" })[value];
const applyFilter = () => {
  selectedState.value = draftState.value || undefined;
  void load();
};
const openDetail = async (reference: string) => {
  const response = await client.getInboxItem(reference);
  detail.value = response.data.inbox_item;
};
</script>

<style scoped>
.email-inbox-view,
.email-inbox-layout,
.email-inbox-list,
.email-inbox-card,
.email-inbox-detail {
  min-width: 0;
}

.email-inbox-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
}

.email-inbox-filters label {
  display: grid;
  gap: 6px;
  color: var(--gbos-muted);
  font-size: 13px;
  font-weight: 700;
}

.email-inbox-filters select {
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-surface);
}

.email-inbox-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(280px, 0.65fr);
  gap: 16px;
}

.email-inbox-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.email-inbox-card,
.email-inbox-detail {
  overflow-wrap: anywhere;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.email-inbox-card__heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.email-inbox-card p,
.email-inbox-card h2,
.email-inbox-detail p,
.email-inbox-detail h2 {
  margin: 0;
}

.email-inbox-card__heading p,
.email-inbox-detail > p:first-child {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
}

.email-inbox-card h2,
.email-inbox-detail h2 {
  font-size: 18px;
}

.email-inbox-card__summary,
.email-inbox-detail > p {
  margin-top: 12px !important;
  line-height: 1.55;
}

.email-inbox-card dl,
.email-inbox-detail dl {
  display: grid;
  gap: 8px;
}

.email-inbox-card dl div,
.email-inbox-detail dl div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.email-inbox-card dt,
.email-inbox-detail dt {
  color: var(--gbos-muted);
}

.email-inbox-card dd,
.email-inbox-detail dd {
  margin: 0;
  text-align: right;
}

@media (max-width: 767px) {
  .email-inbox-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .email-inbox-filters label,
  .email-inbox-filters select {
    width: 100%;
  }
}
</style>
