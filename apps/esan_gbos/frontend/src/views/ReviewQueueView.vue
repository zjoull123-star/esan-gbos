<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          ESAN GBOS · Gate 4
        </p>
        <h1>人工审核队列</h1>
        <p>仅显示分配给当前审核人的待审案件；业务主体保持只读。</p>
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
    <StatePanel v-else-if="cases.length === 0" kind="empty" @retry="load" />
    <template v-else>
      <DemoBanner v-if="hasFixtureData" />
      <ul class="record-grid" aria-label="待审核案件">
        <li v-for="reviewCase in cases" :key="reviewCase.name">
          <article class="record-card">
            <p class="eyebrow">
              待审核
            </p>
            <h2>{{ reviewCase.title }}</h2>
            <dl class="status-list">
              <div>
                <dt>主体类型</dt>
                <dd>{{ reviewCase.subject.doctype }}</dd>
              </div>
              <div>
                <dt>案件版本</dt>
                <dd>{{ reviewCase.case_revision }}</dd>
              </div>
              <div>
                <dt>证据数量</dt>
                <dd>{{ reviewCase.evidence.length }}</dd>
              </div>
            </dl>
            <RouterLink class="text-link" :to="`/gbos/review/${encodeURIComponent(reviewCase.name)}`">
              查看并审核
            </RouterLink>
          </article>
        </li>
      </ul>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";

import { useBffClient } from "@/api/injection";
import DemoBanner from "@/components/DemoBanner.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.listReviewCases({ pageSize: 20 });
  return response.data;
});
const cases = computed(() =>
  (resource.data.value?.cases ?? []).filter(
    (reviewCase) => reviewCase.review_status === "Pending",
  ),
);
const hasFixtureData = computed(() =>
  cases.value.some((reviewCase) => reviewCase.origin === "Fixture"),
);
const { state, message, requestId, load } = resource;
</script>
