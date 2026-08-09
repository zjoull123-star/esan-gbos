<template>
  <section class="review-queue-view">
    <OperationalListTemplate>
      <template #header>
        <PageHeader
          eyebrow="ESAN GBOS · GATE 4"
          title="人工审核队列"
          description="正式审核案件与 AI 草稿分别读取、分别报错；草稿只能送交人工审核，不能直接修改业务主体。"
        >
          <template #actions>
            <GbosButton intent="secondary" type="button" @click="refreshAll">
              刷新全部
            </GbosButton>
          </template>
        </PageHeader>
      </template>

      <template #list>
        <div class="review-resource-stack">
          <section
            class="review-resource-section"
            data-review-resource="drafts"
            aria-labelledby="ai-drafts-title"
          >
            <div class="review-section-heading">
              <div>
                <p>AI 草稿闸门</p>
                <h2 id="ai-drafts-title">
                  AI Draft → Pending
                </h2>
              </div>
              <span>仅送交审核，不形成正式业务命令</span>
            </div>
            <p v-if="draftMessage" class="review-notice review-notice--success" role="status">
              {{ draftMessage }}
            </p>
            <p v-if="draftError" class="review-notice review-notice--error" role="alert">
              {{ draftError }}
            </p>
            <ResourceBoundary
              :state="draftState"
              :message="draftBoundaryMessage"
              :request-id="draftRequestId"
              :empty="aiDrafts.length === 0"
              @retry="draftResource.load"
            >
              <ul class="review-card-list" aria-label="AI 草稿">
                <li v-for="draft in aiDrafts" :key="draft.draft_id" class="review-card">
                  <div class="review-card__heading">
                    <div>
                      <p>{{ draft.origin }} · {{ draft.status }}</p>
                      <h3>{{ draft.subject }}</h3>
                    </div>
                    <span>{{ draft.kind }}</span>
                  </div>
                  <dl class="review-label-rows">
                    <div><dt>草稿编号</dt><dd>{{ draft.draft_id }}</dd></div>
                    <div><dt>版本</dt><dd>{{ draft.revision }}</dd></div>
                    <div><dt>模型</dt><dd>{{ draft.model.name }} · {{ draft.model.version }}</dd></div>
                    <div><dt>证据数量</dt><dd>{{ draft.evidence.length }}</dd></div>
                  </dl>
                  <p
                    v-if="draft.kind === 'CEO Informal Observation'"
                    class="review-boundary-note"
                  >
                    基于沟通的非正式观察/非正式指标
                  </p>
                  <GbosButton
                    v-if="draft.status === 'AI Draft'"
                    :data-draft-submit="draft.draft_id"
                    intent="primary"
                    type="button"
                    :loading="submittingDrafts.has(draft.draft_id)"
                    :disabled="submittingDrafts.has(draft.draft_id)"
                    @click="requestDraftSubmit(draft)"
                  >
                    {{ submittingDrafts.has(draft.draft_id) ? "送审中…" : "送交人工审核" }}
                  </GbosButton>
                  <p v-else class="review-boundary-note" role="status">
                    已进入 Pending，等待人工审核。
                  </p>
                </li>
              </ul>
            </ResourceBoundary>
          </section>

          <section
            class="review-resource-section"
            data-review-resource="cases"
            aria-labelledby="review-cases-title"
          >
            <div class="review-section-heading">
              <div>
                <p>FRAPPE REVIEW CASES</p>
                <h2 id="review-cases-title">
                  待审核案件
                </h2>
              </div>
              <span>仅显示服务端分配给当前审核人的 Pending 案件</span>
            </div>
            <ResourceBoundary
              :state="caseState"
              :message="caseBoundaryMessage"
              :request-id="caseRequestId"
              :empty="cases.length === 0"
              @retry="caseResource.load"
            >
              <DemoBanner v-if="hasFixtureData" />
              <ul class="review-card-list" aria-label="待审核案件">
                <li v-for="reviewCase in cases" :key="reviewCase.name" class="review-card">
                  <div class="review-card__heading">
                    <div>
                      <p>待审核</p>
                      <h3>{{ reviewCase.title }}</h3>
                    </div>
                    <span>{{ reviewCase.subject.doctype }}</span>
                  </div>
                  <dl class="review-label-rows">
                    <div><dt>案件编号</dt><dd>{{ reviewCase.name }}</dd></div>
                    <div><dt>案件版本</dt><dd>{{ reviewCase.case_revision }}</dd></div>
                    <div><dt>主体版本</dt><dd>{{ reviewCase.subject.revision }}</dd></div>
                    <div><dt>证据数量</dt><dd>{{ reviewCase.evidence.length }}</dd></div>
                  </dl>
                  <RouterLink
                    class="review-detail-link"
                    :to="`/gbos/review/${encodeURIComponent(reviewCase.name)}`"
                  >
                    查看并审核
                  </RouterLink>
                </li>
              </ul>
            </ResourceBoundary>
          </section>
        </div>
      </template>

      <template v-if="caseCursor || nextCaseCursor" #pagination>
        <div class="review-pagination">
          <GbosButton
            v-if="caseCursor"
            data-pagination="home"
            intent="secondary"
            type="button"
            @click="returnHome"
          >
            回首页
          </GbosButton>
          <GbosButton
            v-if="nextCaseCursor"
            data-pagination="next"
            intent="secondary"
            type="button"
            @click="nextPage"
          >
            下一页
          </GbosButton>
        </div>
      </template>
    </OperationalListTemplate>

    <ConfirmDialog
      v-model="draftConfirmOpen"
      title="送交人工审核"
      :message="draftConfirmMessage"
      confirm-label="确认送审"
      @confirm="confirmDraftSubmit"
      @cancel="clearDraftConfirmation"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, ref, type DeepReadonly } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { AiDraft } from "@/api/types";
import DemoBanner from "@/components/DemoBanner.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { sessionState } from "@/session";

const client = useBffClient();
const caseCursor = ref<string>();
const submittedDrafts = ref<AiDraft[]>([]);
const submittingDrafts = ref(new Set<string>());
const draftMessage = ref("");
const draftError = ref("");
const draftConfirmOpen = ref(false);
const pendingDraft = ref<AiDraft>();

const caseResource = useOnlineResource(async () => {
  const response = await client.listReviewCases({
    cursor: caseCursor.value,
    pageSize: 20,
  });
  return response.data;
});

const draftResource = useOnlineResource(async () => {
  submittedDrafts.value = [];
  const canReadAiDrafts =
    sessionState.roles.includes("Reviewer") ||
    sessionState.roles.includes("GBOS Admin");
  if (!canReadAiDrafts) {
    return { drafts: [], next_cursor: null };
  }
  const response = await client.listAiDrafts({ pageSize: 20 });
  return response.data;
});

const cases = computed(() =>
  (caseResource.data.value?.cases ?? []).filter(
    (reviewCase) => reviewCase.review_status === "Pending",
  ),
);
const nextCaseCursor = computed(
  () => caseResource.data.value?.next_cursor ?? null,
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
    : (draftResource.data.value?.drafts ?? []).map(cloneDraft),
);

const caseState = caseResource.state;
const caseRequestId = caseResource.requestId;
const caseBoundaryMessage = computed(() =>
  caseState.value === "ready" && cases.value.length === 0
    ? "当前没有分配给你的待审核案件。"
    : caseResource.message.value,
);
const draftState = draftResource.state;
const draftRequestId = draftResource.requestId;
const draftBoundaryMessage = computed(() =>
  draftState.value === "ready" && aiDrafts.value.length === 0
    ? "当前没有可送审的 AI 草稿。"
    : draftResource.message.value,
);
const draftConfirmMessage = computed(() =>
  pendingDraft.value
    ? `确认将“${pendingDraft.value.subject}”从 AI Draft 送入 Pending？此操作不会直接修改业务主体。`
    : "确认将这份 AI Draft 送入 Pending？",
);

const refreshAll = () => {
  draftMessage.value = "";
  draftError.value = "";
  void Promise.all([caseResource.load(), draftResource.load()]);
};
const nextPage = () => {
  if (!nextCaseCursor.value) {
    return;
  }
  caseCursor.value = nextCaseCursor.value;
  void caseResource.load();
};
const returnHome = () => {
  caseCursor.value = undefined;
  void caseResource.load();
};

const requestDraftSubmit = (draft: DeepReadonly<AiDraft>) => {
  if (draft.status !== "AI Draft" || submittingDrafts.value.has(draft.draft_id)) {
    return;
  }
  pendingDraft.value = cloneDraft(draft);
  draftConfirmOpen.value = true;
};
const clearDraftConfirmation = () => {
  pendingDraft.value = undefined;
};
const confirmDraftSubmit = () => {
  const draft = pendingDraft.value;
  pendingDraft.value = undefined;
  if (draft) {
    void submitDraft(draft);
  }
};

const submitDraft = async (draft: AiDraft) => {
  if (draft.status !== "AI Draft" || submittingDrafts.value.has(draft.draft_id)) {
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
      (error.status === 409 ||
        error.code === "revision_conflict" ||
        error.code === "idempotency_conflict")
    ) {
      await draftResource.load();
    }
  } finally {
    const next = new Set(submittingDrafts.value);
    next.delete(draft.draft_id);
    submittingDrafts.value = next;
  }
};
</script>

<style scoped>
.review-queue-view,
.review-resource-stack,
.review-card-list {
  min-width: 0;
}

.review-resource-stack {
  display: grid;
  gap: 16px;
}

.review-resource-section {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.review-section-heading,
.review-card__heading,
.review-pagination {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.review-section-heading p,
.review-section-heading h2,
.review-section-heading span,
.review-card__heading p,
.review-card__heading h3,
.review-card__heading span,
.review-boundary-note,
.review-notice {
  margin: 0;
}

.review-section-heading p,
.review-card__heading p {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.review-section-heading h2,
.review-card__heading h3 {
  margin-top: 3px;
  color: var(--gbos-text);
  line-height: 1.35;
}

.review-section-heading h2 {
  font-size: 20px;
}

.review-card__heading h3 {
  font-size: 17px;
}

.review-section-heading span,
.review-card__heading span {
  color: var(--gbos-muted);
  font-size: 13px;
  line-height: 1.5;
}

.review-card-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.review-card {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-canvas);
}

.review-label-rows {
  display: grid;
  gap: 7px;
  margin: 0;
}

.review-label-rows > div {
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(88px, 0.7fr) minmax(0, 1.3fr);
  gap: 10px;
  padding-top: 7px;
  border-top: 1px solid var(--gbos-border);
}

.review-label-rows dt,
.review-label-rows dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.review-label-rows dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.review-label-rows dd {
  color: var(--gbos-text);
  font-size: 13px;
  line-height: 1.45;
}

.review-detail-link {
  display: inline-flex;
  width: fit-content;
  min-height: 36px;
  align-items: center;
  padding: 8px 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-primary);
  background: var(--gbos-surface);
  font-size: 14px;
  font-weight: 700;
  text-decoration: none;
}

.review-boundary-note,
.review-notice {
  padding: 10px 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-canvas);
  font-size: 13px;
  line-height: 1.55;
}

.review-notice--success {
  border-color: var(--gbos-accent);
}

.review-notice--error {
  border-color: var(--gbos-primary);
}

.review-pagination {
  flex-wrap: wrap;
}

@media (max-width: 767px) {
  .review-section-heading,
  .review-card__heading {
    flex-direction: column;
  }

  .review-label-rows > div {
    grid-template-columns: minmax(0, 1fr);
    gap: 3px;
  }

  .review-detail-link {
    min-height: 44px;
  }
}
</style>
