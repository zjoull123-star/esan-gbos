<template>
  <section class="view">
    <DetailCommandTemplate>
      <template #header>
        <PageHeader
          eyebrow="LOCAL PILOT · 沟通详情"
          title="沟通观察详情"
          description="先核对中文摘要与证据，再查看受权限保护的原文；事实与关联仅为 Proposal。"
        >
          <template #actions>
            <RouterLink class="button button--secondary" to="/gbos/communications">
              返回列表
            </RouterLink>
          </template>
        </PageHeader>
      </template>

      <template #facts>
        <div v-if="communication" class="communication-facts">
          <p
            v-if="communication.classification === 'CEO Informal Observation'"
            class="informal-label"
            role="note"
          >
            基于沟通的非正式观察/非正式指标
          </p>
          <dl class="status-list">
            <div><dt>观察编号</dt><dd>{{ communication.observation_id }}</dd></div>
            <div><dt>渠道</dt><dd>{{ communication.channel }}</dd></div>
            <div><dt>发生时间</dt><dd>{{ communication.occurred_at }}</dd></div>
            <div><dt>审核状态</dt><dd>{{ communication.review_status }}</dd></div>
            <div><dt>团队</dt><dd>{{ communication.team_ref || "未关联" }}</dd></div>
            <div><dt>业务方</dt><dd>{{ communication.party_ref || "未关联" }}</dd></div>
          </dl>
        </div>
      </template>

      <template #main>
        <ResourceBoundary
          :state="state"
          :message="message"
          :request-id="requestId"
          :empty="!communication"
          @retry="load"
        >
          <div v-if="communication" class="evidence-stack">
            <EvidencePanel
              :title="`${communication.channel} · ${communication.classification}`"
              :summary-zh="communication.summary_zh"
              :original-text="communication.raw_access_allowed ? communication.original_text : undefined"
              :original-language="communication.original_language"
            />
            <p
              v-if="!communication.raw_access_allowed"
              class="restricted-notice"
              role="note"
            >
              Restricted：当前角色无权查看原文。请按既有权限流程申请，页面不会绕过服务端范围。
            </p>
            <article class="evidence-reference-panel" aria-labelledby="evidence-location-title">
              <h2 id="evidence-location-title">
                证据定位
              </h2>
              <p v-if="communication.evidence.length === 0">
                暂无可展示的证据定位。
              </p>
              <ul v-else class="evidence-ref-list">
                <li v-for="item in communication.evidence" :key="`${item.ref}:${item.locator}`">
                  <strong>{{ item.ref }}</strong>
                  <span>{{ item.locator }}</span>
                </li>
              </ul>
            </article>
          </div>
        </ResourceBoundary>
      </template>

      <template v-if="communication" #command>
        <div class="detail-stack">
          <p class="proposal-boundary" role="note">
            所有事实与关联均为 Proposal，不构成批准、外发或正式业务修改。
          </p>
          <article class="command-card">
            <h2>事实提案（Proposal）</h2>
            <p v-if="communication.fact_proposals.length === 0">
              暂无事实提案。
            </p>
            <ul v-else class="proposal-list">
              <li v-for="proposal in communication.fact_proposals" :key="`${proposal.type}:${proposal.value_display}`">
                <strong>{{ proposal.type }}</strong>
                <span>{{ proposal.value_display }}</span>
                <small>Proposal · {{ proposal.status }} · 置信度 {{ formatConfidence(proposal.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="command-card">
            <h2>关联建议（Proposal）</h2>
            <p v-if="communication.association_suggestions.length === 0">
              暂无关联建议。
            </p>
            <ul v-else class="proposal-list">
              <li v-for="suggestion in communication.association_suggestions" :key="`${suggestion.type}:${suggestion.target_ref}`">
                <strong>{{ suggestion.type }}</strong>
                <span>{{ suggestion.target_ref }}</span>
                <small>Proposal · 置信度 {{ formatConfidence(suggestion.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="command-card">
            <h2>模型版本</h2>
            <p>{{ communication.model.name }} · {{ communication.model.version }}</p>
          </article>
        </div>
      </template>
    </DetailCommandTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, watch } from "vue";

import { useBffClient } from "@/api/injection";
import EvidencePanel from "@/components/data/EvidencePanel.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
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

<style scoped>
.communication-facts,
.evidence-stack,
.detail-stack {
  display: grid;
  min-width: 0;
  gap: 12px;
}

.communication-facts .status-list {
  margin: 0;
}

.informal-label,
.restricted-notice {
  margin: 0;
}

.evidence-reference-panel {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.evidence-reference-panel h2,
.evidence-reference-panel p {
  margin: 0;
}

.evidence-reference-panel h2 {
  font-size: 18px;
}

.evidence-reference-panel p,
.evidence-reference-panel .evidence-ref-list {
  margin-top: 10px;
}

.proposal-boundary {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-muted);
  background: var(--gbos-canvas);
  font-size: 13px;
  line-height: 1.55;
}
</style>
