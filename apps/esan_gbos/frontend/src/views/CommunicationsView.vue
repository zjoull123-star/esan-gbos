<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          LOCAL PILOT · 服务端范围
        </p>
        <h1>沟通观察</h1>
        <p>销售角色只接收服务端按本人或团队裁剪后的结果；页面不会扩大数据范围。</p>
      </div>
      <button class="button button--secondary" type="button" @click="resetFilters">
        清除筛选
      </button>
    </header>

    <form class="filter-bar" aria-label="沟通筛选" @submit.prevent="applyFilters">
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
      <button class="button button--primary" type="submit">
        应用筛选
      </button>
    </form>

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
    <StatePanel v-else-if="communications.length === 0" kind="empty" @retry="resetFilters" />
    <template v-else>
      <ul class="record-grid" aria-label="沟通观察列表">
        <li v-for="item in communications" :key="item.observation_id">
          <article class="record-card">
            <p class="eyebrow">
              {{ item.channel }} · {{ item.classification }}
            </p>
            <h2>{{ item.summary_zh }}</h2>
            <dl class="status-list">
              <div><dt>发生时间</dt><dd>{{ item.occurred_at }}</dd></div>
              <div><dt>原始语言</dt><dd>{{ item.original_language }}</dd></div>
              <div><dt>审核状态</dt><dd>{{ item.review_status }}</dd></div>
              <div><dt>证据数量</dt><dd>{{ item.evidence_count }}</dd></div>
              <div><dt>团队</dt><dd>{{ item.team_ref || "未关联" }}</dd></div>
              <div><dt>业务方</dt><dd>{{ item.party_ref || "未关联" }}</dd></div>
            </dl>
            <RouterLink
              class="text-link"
              :to="`/gbos/communications/${encodeURIComponent(item.observation_id)}`"
            >
              查看安全详情
            </RouterLink>
          </article>
        </li>
      </ul>
      <div class="pagination-actions">
        <button
          v-if="nextCursor"
          class="button button--secondary"
          type="button"
          @click="nextPage"
        >
          下一页
        </button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from "vue";

import { useBffClient } from "@/api/injection";
import type { CommunicationListQuery } from "@/api/types";
import StatePanel from "@/components/StatePanel.vue";
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
const nextPage = () => {
  if (nextCursor.value) {
    cursor.value = nextCursor.value;
    void load();
  }
};
</script>
