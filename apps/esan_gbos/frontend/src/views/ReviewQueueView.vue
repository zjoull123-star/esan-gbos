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
          <form class="review-kind-filter" aria-label="审核类型筛选" @submit.prevent>
            <label for="review-kind">审核类型</label>
            <select id="review-kind" v-model="reviewKind" name="review_kind">
              <option value="general">
                通用审核与 AI 草稿
              </option>
              <option value="identity">
                Identity Resolution
              </option>
            </select>
            <p>Identity Resolution 使用服务端专用队列与分页，不在本地筛选通用案件。</p>
          </form>
          <section
            v-if="reviewKind === 'general'"
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
            v-if="reviewKind === 'general'"
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

          <section
            v-else
            class="review-resource-section"
            data-review-resource="identity"
            aria-labelledby="identity-reviews-title"
          >
            <div class="review-section-heading">
              <div>
                <p>SERVER-SCOPED REVIEW CASES</p>
                <h2 id="identity-reviews-title">
                  Identity Resolution
                </h2>
              </div>
              <span>仅显示服务端分配给当前审核人的身份解析案件</span>
            </div>
            <p v-if="!canReadIdentityReviews" class="review-boundary-note" role="note">
              当前角色没有身份解析审核队列权限；页面不会尝试扩大服务端范围。
            </p>
            <ResourceBoundary
              v-else
              :state="identityState"
              :message="identityBoundaryMessage"
              :request-id="identityRequestId"
              :empty="identityReviews.length === 0"
              @retry="identityResource.load"
            >
              <ul class="review-card-list" aria-label="身份解析待审核案件">
                <li
                  v-for="identityReview in identityReviews"
                  :key="identityReview.review_case_ref"
                  class="review-card"
                >
                  <div class="review-card__heading">
                    <div>
                      <p>待审核</p>
                      <h3>{{ identityReview.target.display_label }}</h3>
                    </div>
                    <span>{{ identityReview.target.candidate_type }}</span>
                  </div>
                  <IdentityReviewFacts :review="identityReview" />
                  <button
                    class="review-detail-link"
                    type="button"
                    :data-identity-detail="identityReview.review_case_ref"
                    :disabled="identityDetailLoading"
                    @click="loadIdentityDetail(identityReview.review_case_ref)"
                  >
                    {{ identityDetailRef === identityReview.review_case_ref && identityDetailLoading ? "读取中…" : "查看固定详情" }}
                  </button>
                  <p
                    v-if="identityDetailError && identityDetailRef === identityReview.review_case_ref"
                    class="review-notice review-notice--error"
                    role="alert"
                  >
                    {{ identityDetailError }}
                  </p>
                  <article
                    v-if="identityDetail?.review_case_ref === identityReview.review_case_ref"
                    class="identity-review-detail"
                    aria-label="身份解析固定详情"
                  >
                    <h4>身份解析固定详情</h4>
                    <IdentityReviewFacts :review="identityDetail" />
                  </article>
                </li>
              </ul>
              <div v-if="identityPage > 1 || identityHasMore" class="review-pagination">
                <GbosButton
                  v-if="identityPage > 1"
                  data-identity-pagination="previous"
                  intent="secondary"
                  type="button"
                  @click="previousIdentityPage"
                >
                  上一页
                </GbosButton>
                <GbosButton
                  v-if="identityHasMore"
                  data-identity-pagination="next"
                  intent="secondary"
                  type="button"
                  @click="nextIdentityPage"
                >
                  下一页
                </GbosButton>
              </div>
            </ResourceBoundary>
          </section>
        </div>
      </template>

      <template v-if="reviewKind === 'general' && (caseCursor || nextCaseCursor)" #pagination>
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
import { computed, defineComponent, h, ref, watch, type DeepReadonly, type PropType } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { AiDraft, IdentityPendingReview } from "@/api/types";
import DemoBanner from "@/components/DemoBanner.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import OperationalListTemplate from "@/components/layout/OperationalListTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import ConfirmDialog from "@/components/ui/ConfirmDialog.vue";
import GbosButton from "@/components/ui/GbosButton.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { sessionState } from "@/session";

const client = useBffClient();
const reviewKind = ref<"general" | "identity">("general");
const caseCursor = ref<string>();
const submittedDrafts = ref<AiDraft[]>([]);
const submittingDrafts = ref(new Set<string>());
const draftMessage = ref("");
const draftError = ref("");
const draftConfirmOpen = ref(false);
const pendingDraft = ref<AiDraft>();
const identityPage = ref(1);
const identityDetail = ref<IdentityPendingReview>();
const identityDetailRef = ref("");
const identityDetailLoading = ref(false);
const identityDetailError = ref("");
let identityDetailGeneration = 0;

const canReadIdentityReviews = computed(
  () =>
    sessionState.roles.includes("Reviewer") ||
    sessionState.roles.includes("GBOS Admin"),
);

const IdentityReviewFacts = defineComponent({
  name: "IdentityReviewFacts",
  props: {
    review: {
      type: Object as PropType<DeepReadonly<IdentityPendingReview>>,
      required: true,
    },
  },
  setup(props) {
    const row = (label: string, value: string | number) =>
      h("div", [h("dt", label), h("dd", String(value))]);
    return () =>
      h("dl", { class: "review-label-rows" }, [
        row("目标类型", props.review.target.candidate_type),
        row("安全目标", props.review.target.display_label),
        row("审核版本", props.review.review_case_revision),
        row("映射版本", props.review.mapping_revision),
        row("分配审核人引用", props.review.assigned_reviewer),
        row("团队", props.review.team_ref),
        row("策略版本", props.review.policy_version),
        row(
          "固定证据引用",
          props.review.evidence_refs.length
            ? props.review.evidence_refs.join("、")
            : "暂无",
        ),
      ]);
  },
});

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

const identityResource = useOnlineResource(async () => {
  identityDetailGeneration += 1;
  identityDetail.value = undefined;
  identityDetailRef.value = "";
  identityDetailError.value = "";
  if (reviewKind.value !== "identity" || !canReadIdentityReviews.value) {
    return { reviews: [], has_more: false };
  }
  const response = await client.listPendingIdentityReviews({
    page: identityPage.value,
    pageSize: 20,
  });
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
const identityState = identityResource.state;
const identityRequestId = identityResource.requestId;
const identityReviews = computed(() => identityResource.data.value?.reviews ?? []);
const identityHasMore = computed(() => identityResource.data.value?.has_more ?? false);
const identityBoundaryMessage = computed(() =>
  identityState.value === "ready" && identityReviews.value.length === 0
    ? "当前没有分配给你的身份解析待审核案件。"
    : identityResource.message.value,
);
const draftConfirmMessage = computed(() =>
  pendingDraft.value
    ? `确认将“${pendingDraft.value.subject}”从 AI Draft 送入 Pending？此操作不会直接修改业务主体。`
    : "确认将这份 AI Draft 送入 Pending？",
);

const refreshAll = () => {
  draftMessage.value = "";
  draftError.value = "";
  if (reviewKind.value === "identity") {
    void identityResource.load();
  } else {
    void Promise.all([caseResource.load(), draftResource.load()]);
  }
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
const previousIdentityPage = () => {
  if (identityPage.value > 1) {
    identityPage.value -= 1;
    void identityResource.load();
  }
};
const nextIdentityPage = () => {
  if (identityHasMore.value) {
    identityPage.value += 1;
    void identityResource.load();
  }
};

const loadIdentityDetail = async (reviewCaseRef: string) => {
  if (identityDetailLoading.value) {
    return;
  }
  const generation = ++identityDetailGeneration;
  identityDetailRef.value = reviewCaseRef;
  identityDetail.value = undefined;
  identityDetailError.value = "";
  identityDetailLoading.value = true;
  try {
    const response = await client.getPendingIdentityReview(reviewCaseRef);
    if (generation === identityDetailGeneration) {
      identityDetail.value = response.data.review;
    }
  } catch (error) {
    if (generation === identityDetailGeneration) {
      identityDetailError.value =
        error instanceof BffError ? error.displayMessage : "暂时无法读取身份审核详情。";
    }
  } finally {
    if (generation === identityDetailGeneration) {
      identityDetailLoading.value = false;
    }
  }
};

watch(reviewKind, (kind) => {
  identityPage.value = 1;
  if (kind === "identity") {
    void identityResource.load();
  }
});

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

.review-kind-filter {
  display: grid;
  min-width: 0;
  gap: 7px;
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.review-kind-filter label {
  color: var(--gbos-text);
  font-size: 13px;
  font-weight: 700;
}

.review-kind-filter select {
  width: 100%;
  min-width: 0;
  min-height: 44px;
  padding: 8px 10px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font: inherit;
}

.review-kind-filter p {
  margin: 0;
  color: var(--gbos-muted);
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
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
  min-height: 44px;
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

button.review-detail-link {
  cursor: pointer;
  font-family: inherit;
}

button.review-detail-link:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.identity-review-detail {
  display: grid;
  min-width: 0;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-surface);
}

.identity-review-detail h4 {
  margin: 0;
  color: var(--gbos-text);
  font-size: 15px;
  overflow-wrap: anywhere;
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
