<template>
  <section class="view">
    <header class="page-header">
      <div>
        <p class="eyebrow">
          LOCAL PILOT · 沟通详情
        </p>
        <h1>沟通观察详情</h1>
        <p>中文摘要、证据定位和 AI 提案分区展示；所有提案仍需人工审核。</p>
      </div>
      <RouterLink class="button button--secondary" to="/gbos/communications">
        返回列表
      </RouterLink>
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
    <template v-else-if="communication">
      <p
        v-if="communication.classification === 'CEO Informal Observation'"
        class="informal-label"
        role="note"
      >
        基于沟通的非正式观察/非正式指标
      </p>
      <div class="communication-layout">
        <article class="evidence-card">
          <p class="eyebrow">
            {{ communication.channel }} · {{ communication.classification }}
          </p>
          <h2>中文摘要</h2>
          <p>{{ communication.summary_zh }}</p>
          <dl class="status-list">
            <div><dt>观察编号</dt><dd>{{ communication.observation_id }}</dd></div>
            <div><dt>发生时间</dt><dd>{{ communication.occurred_at }}</dd></div>
            <div><dt>原始语言</dt><dd>{{ communication.original_language }}</dd></div>
            <div><dt>审核状态</dt><dd>{{ communication.review_status }}</dd></div>
          </dl>
          <section>
            <h3>原始语言内容</h3>
            <blockquote
              v-if="communication.raw_access_allowed && communication.original_text"
              :lang="communication.original_language"
            >
              {{ communication.original_text }}
            </blockquote>
            <p v-else class="restricted-notice">
              Restricted 原文默认不可打开；如业务确需访问，请按权限流程申请。
            </p>
          </section>
          <section>
            <h3>证据定位</h3>
            <ul class="evidence-ref-list">
              <li v-for="item in communication.evidence" :key="`${item.ref}:${item.locator}`">
                <strong>{{ item.ref }}</strong>
                <span>{{ item.locator }}</span>
              </li>
            </ul>
          </section>
        </article>

        <div class="detail-stack">
          <article class="command-card">
            <h2>事实提案</h2>
            <p v-if="communication.fact_proposals.length === 0">
              暂无事实提案。
            </p>
            <ul v-else class="proposal-list">
              <li v-for="proposal in communication.fact_proposals" :key="`${proposal.type}:${proposal.value_display}`">
                <strong>{{ proposal.type }}</strong>
                <span>{{ proposal.value_display }}</span>
                <small>{{ proposal.status }} · 置信度 {{ formatConfidence(proposal.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="command-card">
            <h2>关联建议</h2>
            <p v-if="communication.association_suggestions.length === 0">
              暂无关联建议。
            </p>
            <ul v-else class="proposal-list">
              <li v-for="suggestion in communication.association_suggestions" :key="`${suggestion.type}:${suggestion.target_ref}`">
                <strong>{{ suggestion.type }}</strong>
                <span>{{ suggestion.target_ref }}</span>
                <small>置信度 {{ formatConfidence(suggestion.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="command-card">
            <h2>模型版本</h2>
            <p>{{ communication.model.name }} · {{ communication.model.version }}</p>
          </article>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getCommunication(props.id);
  return response.data;
});
const communication = computed(() => resource.data.value?.communication);
const { state, message, requestId, load } = resource;
const formatConfidence = (value: number) =>
  `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;

watch(
  () => props.id,
  () => void load(),
);
</script>
