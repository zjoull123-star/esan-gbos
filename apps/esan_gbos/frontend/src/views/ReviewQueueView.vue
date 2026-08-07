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
    <StatePanel
      v-else-if="cases.length === 0 && aiDrafts.length === 0"
      kind="empty"
      @retry="load"
    />
    <template v-else>
      <DemoBanner v-if="hasFixtureData" />
      <section v-if="aiDrafts.length" class="review-draft-section" aria-labelledby="ai-drafts-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">
              AI 草稿闸门
            </p>
            <h2 id="ai-drafts-title">
              AI Draft → Pending
            </h2>
          </div>
          <p>这里只能送交人工审核，不能自动形成正式业务命令。</p>
        </div>
        <p v-if="draftMessage" class="notice notice--success" role="status">
          {{ draftMessage }}
        </p>
        <p v-if="draftError" class="notice notice--error" role="alert">
          {{ draftError }}
        </p>
        <ul class="record-grid" aria-label="AI 草稿">
          <li v-for="draft in aiDrafts" :key="draft.draft_id">
            <article class="record-card">
              <p class="eyebrow">
                {{ draft.origin }} · {{ draft.status }}
              </p>
              <h3>{{ draft.subject }}</h3>
              <dl class="status-list">
                <div><dt>类型</dt><dd>{{ draft.kind }}</dd></div>
                <div><dt>版本</dt><dd>{{ draft.revision }}</dd></div>
                <div><dt>模型</dt><dd>{{ draft.model.name }} · {{ draft.model.version }}</dd></div>
                <div><dt>证据数量</dt><dd>{{ draft.evidence.length }}</dd></div>
              </dl>
              <p
                v-if="draft.kind === 'CEO Informal Observation'"
                class="informal-label"
              >
                基于沟通的非正式观察/非正式指标
              </p>
              <button
                v-if="draft.status === 'AI Draft'"
                class="button button--primary"
                type="button"
                :disabled="submittingDrafts.has(draft.draft_id)"
                @click="submitDraft(draft)"
              >
                {{ submittingDrafts.has(draft.draft_id) ? "送审中…" : "送交人工审核" }}
              </button>
              <p v-else class="form-message">
                已进入 Pending，等待人工审核。
              </p>
            </article>
          </li>
        </ul>
      </section>
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
import { computed, ref, type DeepReadonly } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { AiDraft } from "@/api/types";
import DemoBanner from "@/components/DemoBanner.vue";
import StatePanel from "@/components/StatePanel.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { sessionState } from "@/session";

const client = useBffClient();
const submittedDrafts = ref<AiDraft[]>([]);
const submittingDrafts = ref(new Set<string>());
const draftMessage = ref("");
const draftError = ref("");
const resource = useOnlineResource(async () => {
  const response = await client.listReviewCases({ pageSize: 20 });
  const canReadAiDrafts =
    sessionState.roles.includes("Reviewer") ||
    sessionState.roles.includes("GBOS Admin");
  const drafts = canReadAiDrafts
    ? (await client.listAiDrafts({ pageSize: 20 })).data.drafts
    : [];
  submittedDrafts.value = [];
  return { ...response.data, drafts };
});
const cases = computed(() =>
  (resource.data.value?.cases ?? []).filter(
    (reviewCase) => reviewCase.review_status === "Pending",
  ),
);
const hasFixtureData = computed(() =>
  cases.value.some((reviewCase) => reviewCase.origin === "Fixture"),
);
const cloneDraft = (draft: DeepReadonly<AiDraft>): AiDraft => ({
  ...draft,
  evidence: draft.evidence.map((evidence) => ({ ...evidence })),
  model: { ...draft.model },
});
const aiDrafts = computed<AiDraft[]>(() =>
  submittedDrafts.value.length
    ? submittedDrafts.value
    : (resource.data.value?.drafts ?? []).map(cloneDraft),
);
const { state, message, requestId, load } = resource;

const submitDraft = async (draft: DeepReadonly<AiDraft>) => {
  if (draft.status !== "AI Draft" || submittingDrafts.value.has(draft.draft_id)) {
    return;
  }
  if (!window.confirm(`确认将“${draft.subject}”送交人工审核？`)) {
    return;
  }
  draftMessage.value = "";
  draftError.value = "";
  submittingDrafts.value = new Set(submittingDrafts.value).add(draft.draft_id);
  try {
    const response = await client.submitAiDraftForReview({
      draft_id: draft.draft_id,
      expected_revision: draft.revision,
      idempotency_key: createIdempotencyKey(),
    });
    submittedDrafts.value = aiDrafts.value.map((item) =>
      item.draft_id === draft.draft_id ? response.data.draft : item,
    );
    draftMessage.value = "AI 草稿已进入 Pending，等待人工审核。";
  } catch (error) {
    draftError.value =
      error instanceof BffError
        ? error.displayMessage
        : "暂时无法送交审核，请稍后重试。";
    if (
      error instanceof BffError &&
      (error.code === "revision_conflict" || error.code === "idempotency_conflict")
    ) {
      await load();
    }
  } finally {
    const next = new Set(submittingDrafts.value);
    next.delete(draft.draft_id);
    submittingDrafts.value = next;
  }
};
</script>
