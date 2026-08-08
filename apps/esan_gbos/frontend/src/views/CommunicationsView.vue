<template>
  <section class="view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="LOCAL PILOT · 服务端范围"
          title="沟通观察"
          description="销售角色只接收服务端按本人或团队裁剪后的结果；页面不会扩大数据范围。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="resetFilters">
              清除筛选
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #filters>
        <form class="communication-filters" aria-label="沟通筛选" @submit.prevent="applyFilters">
          <label>
            渠道
            <select v-model="draftFilters.channel" name="channel">
              <option value="">全部渠道</option>
              <option value="WhatsApp">WhatsApp</option>
              <option value="WeCom">企业微信</option>
              <option value="Email">邮件</option>
            </select>
          </label>
          <label>
            分类
            <input v-model.trim="draftFilters.classification" name="classification">
          </label>
          <label>
            审核状态
            <select v-model="draftFilters.reviewStatus" name="review_status">
              <option value="">全部状态</option>
              <option value="Unreviewed">未审核</option>
              <option value="Pending">待审核</option>
              <option value="Reviewed">已审核</option>
            </select>
          </label>
          <GbosButton intent="primary" type="submit">
            应用筛选
          </GbosButton>
        </form>
      </template>

      <template #list>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="communications.length === 0"
          @retry="retryLoad"
        >
          <div class="communication-table">
            <table aria-label="沟通观察列表">
              <thead>
                <tr>
                  <th scope="col">
                    渠道
                  </th>
                  <th scope="col">
                    时间
                  </th>
                  <th scope="col">
                    状态
                  </th>
                  <th scope="col">
                    团队
                  </th>
                  <th scope="col">
                    摘要
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in communications" :key="item.observation_id">
                  <td>
                    <strong>{{ item.channel }}</strong>
                    <small>{{ item.classification }}</small>
                  </td>
                  <td>{{ item.occurred_at }}</td>
                  <td>{{ item.review_status }}</td>
                  <td>{{ item.team_ref || "未关联" }}</td>
                  <td>
                    <RouterLink
                      class="communication-summary-link"
                      :to="`/gbos/communications/${encodeURIComponent(item.observation_id)}`"
                    >
                      {{ item.summary_zh }}
                    </RouterLink>
                    <small>{{ item.evidence_count }} 条证据 · {{ item.original_language }}</small>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <ul
            class="communication-mobile-list"
            data-mobile-list
            aria-label="沟通观察移动列表"
          >
            <li v-for="item in communications" :key="item.observation_id">
              <dl>
                <div data-label="渠道">
                  <dt>渠道</dt>
                  <dd>{{ item.channel }}</dd>
                </div>
                <div data-label="分类">
                  <dt>分类</dt>
                  <dd>{{ item.classification }}</dd>
                </div>
                <div data-label="时间">
                  <dt>时间</dt>
                  <dd>{{ item.occurred_at }}</dd>
                </div>
                <div data-label="状态">
                  <dt>状态</dt>
                  <dd>{{ item.review_status }}</dd>
                </div>
                <div data-label="团队">
                  <dt>团队</dt>
                  <dd>{{ item.team_ref || "未关联" }}</dd>
                </div>
                <div data-label="摘要">
                  <dt>摘要</dt>
                  <dd>{{ item.summary_zh }}</dd>
                </div>
                <div data-label="证据数">
                  <dt>证据数</dt>
                  <dd>{{ item.evidence_count }}</dd>
                </div>
                <div data-label="原始语言">
                  <dt>原始语言</dt>
                  <dd>{{ item.original_language }}</dd>
                </div>
                <div data-label="详情">
                  <dt>详情</dt>
                  <dd>
                    <RouterLink
                      class="communication-detail-link"
                      :to="`/gbos/communications/${encodeURIComponent(item.observation_id)}`"
                    >
                      查看详情
                    </RouterLink>
                  </dd>
                </div>
              </dl>
            </li>
          </ul>
        </ResourceBoundary>
      </template>

      <template v-if="nextCursor" #pagination>
        <GbosButton
          intent="secondary"
          type="button"
          data-pagination="next"
          @click="nextPage"
        >
          下一页
        </GbosButton>
      </template>
    </OperationalListTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";

import { useBffClient } from "@/api/injection";
import type { CommunicationListQuery } from "@/api/types";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useBffClient();
const cursor = ref<string>();
const filters = reactive({
  channel: "",
  classification: "",
  reviewStatus: "",
});
const draftFilters = reactive({ ...filters });
const resource = useOnlineResource(async () => {
  const query: CommunicationListQuery = {
    channel: filters.channel || undefined,
    classification: filters.classification || undefined,
    reviewStatus: filters.reviewStatus || undefined,
    cursor: cursor.value,
    pageSize: 20,
  };
  const response = await client.listCommunications(query);
  return response.data;
});
const communications = computed(
  () => resource.data.value?.communications ?? [],
);
const nextCursor = computed(() => resource.data.value?.next_cursor);
const { state, message, requestId, load } = resource;

const applyFilters = () => {
  Object.assign(filters, draftFilters);
  cursor.value = undefined;
  void load();
};
const resetFilters = () => {
  Object.assign(filters, { channel: "", classification: "", reviewStatus: "" });
  Object.assign(draftFilters, filters);
  cursor.value = undefined;
  void load();
};
const retryLoad = () => {
  if (state.value === "ready" && communications.value.length === 0) {
    resetFilters();
    return;
  }
  void load();
};
const nextPage = () => {
  if (nextCursor.value) {
    cursor.value = nextCursor.value;
    void load();
  }
};
</script>

<style scoped>
.communication-filters {
  display: grid;
  min-width: 0;
  grid-template-columns: repeat(3, minmax(128px, 1fr)) auto;
  gap: 12px;
  align-items: end;
  margin: 0;
}

.communication-filters label {
  display: grid;
  min-width: 0;
  gap: 6px;
  color: var(--gbos-muted);
  font-family: var(--gbos-font-sans);
  font-size: 13px;
  font-weight: 700;
}

.communication-filters input,
.communication-filters select {
  width: 100%;
  min-width: 0;
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font-family: var(--gbos-font-sans);
  font-size: 14px;
}

.communication-table {
  min-width: 0;
  overflow-x: auto;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

table {
  width: 100%;
  min-width: 720px;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 13px;
}

th,
td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--gbos-border);
  text-align: start;
  overflow-wrap: anywhere;
  vertical-align: top;
}

th {
  color: var(--gbos-muted);
  background: var(--gbos-canvas);
  font-size: 12px;
  font-weight: 700;
}

th:nth-child(1) {
  width: 18%;
}

th:nth-child(2) {
  width: 21%;
}

th:nth-child(3),
th:nth-child(4) {
  width: 14%;
}

tbody tr:last-child td {
  border-bottom: 0;
}

td strong,
td small {
  display: block;
}

td small {
  margin-top: 3px;
  color: var(--gbos-muted);
  font-size: 11px;
}

.communication-summary-link {
  color: var(--gbos-accent-text);
  font-weight: 700;
  line-height: 1.45;
}

.communication-mobile-list {
  display: none;
  min-width: 0;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.communication-mobile-list > li {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.communication-mobile-list dl,
.communication-mobile-list dt,
.communication-mobile-list dd {
  margin: 0;
}

.communication-mobile-list dl {
  display: grid;
  min-width: 0;
  gap: 10px;
}

.communication-mobile-list [data-label] {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(76px, 0.7fr) minmax(0, 1.3fr);
  gap: 10px;
  align-items: start;
}

.communication-mobile-list dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.communication-mobile-list dd {
  min-width: 0;
  color: var(--gbos-text);
  font-size: 14px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.communication-detail-link {
  color: var(--gbos-accent-text);
  font-weight: 700;
}

@media (max-width: 767px) {
  .communication-filters {
    grid-template-columns: minmax(0, 1fr);
  }

  .communication-filters input,
  .communication-filters select {
    min-height: 44px;
  }

  .communication-table {
    display: none;
  }

  .communication-mobile-list {
    display: grid;
  }
}
</style>
