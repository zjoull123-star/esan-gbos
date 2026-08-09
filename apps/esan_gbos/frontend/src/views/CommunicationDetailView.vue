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
            <RouterLink class="communication-back-link" to="/gbos/communications">
              返回列表
            </RouterLink>
          </template>
        </PageHeader>
      </template>

      <template #facts>
        <div v-if="communication" class="communication-facts">
          <p
            v-if="communication.classification === 'CEO Informal Observation'"
            class="communication-informal-notice"
            role="note"
          >
            基于沟通的非正式观察/非正式指标
          </p>
          <dl class="communication-facts__list">
            <div><dt>观察编号</dt><dd>{{ communication.observation_id }}</dd></div>
            <div><dt>渠道</dt><dd>{{ communication.channel }}</dd></div>
            <div><dt>发生时间</dt><dd>{{ communication.occurred_at }}</dd></div>
            <div><dt>审核状态</dt><dd>{{ communication.review_status }}</dd></div>
            <div><dt>团队</dt><dd>{{ communication.team_ref || "未关联" }}</dd></div>
            <div><dt>业务方</dt><dd>{{ communication.party_ref || "未关联" }}</dd></div>
            <div>
              <dt>渠道账号所属用户</dt>
              <dd>{{ connectorOwnerLabel }}</dd>
            </div>
            <div>
              <dt>业务负责人</dt>
              <dd>本页不推断，请以业务对象或工作项为准</dd>
            </div>
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
              :original-text="originalRevealed ? communication.original_text : undefined"
              :original-language="communication.original_language"
            />
            <button
              v-if="communication.raw_access_allowed && communication.original_text"
              class="communication-original-toggle"
              data-action="reveal-original"
              type="button"
              :aria-expanded="originalRevealed"
              @click="originalRevealed = !originalRevealed"
            >
              {{ originalRevealed ? "隐藏受保护原文" : "显示受保护原文" }}
            </button>
            <p
              v-if="!communication.raw_access_allowed"
              class="communication-restricted-notice"
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
              <ul v-else class="communication-evidence-list">
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
          <article class="communication-proposal-card">
            <h2>事实提案（Proposal）</h2>
            <p v-if="communication.fact_proposals.length === 0">
              暂无事实提案。
            </p>
            <ul v-else class="communication-proposal-list">
              <li v-for="proposal in communication.fact_proposals" :key="`${proposal.type}:${proposal.value_display}`">
                <strong>{{ proposal.type }}</strong>
                <span>{{ proposal.value_display }}</span>
                <small>Proposal · {{ proposal.status }} · 置信度 {{ formatConfidence(proposal.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="communication-proposal-card">
            <h2>关联建议（Proposal）</h2>
            <p v-if="communication.association_suggestions.length === 0">
              暂无关联建议。
            </p>
            <ul v-else class="communication-proposal-list">
              <li v-for="suggestion in communication.association_suggestions" :key="suggestion.suggestion_key">
                <strong>{{ suggestion.type }}</strong>
                <small>Proposal · 置信度 {{ formatConfidence(suggestion.confidence) }}</small>
              </li>
            </ul>
          </article>
          <article class="communication-proposal-card" aria-labelledby="identity-title">
            <h2 id="identity-title">
              消息参与者身份
            </h2>
            <p class="identity-relation-note">
              消息参与者与渠道账号所属用户、业务负责人是三种独立关系，本页不会互相推断。
            </p>
            <p v-if="identityState === 'loading'" role="status">
              正在读取身份状态…
            </p>
            <p v-else-if="identityError" class="identity-error" role="alert">
              {{ identityError }}
            </p>
            <p v-else-if="identities.length === 0">
              暂无可展示的消息参与者身份。
            </p>
            <ul v-else class="identity-state-list" aria-label="消息参与者身份状态">
              <li v-for="(identity, index) in identities" :key="identity.identity_ref">
                <strong>消息参与者 {{ index + 1 }}</strong>
                <span>{{ providerLabel(identity.provider) }} · {{ identityStatusLabel(identity.status) }}</span>
                <span v-if="identity.display_label">{{ identity.display_label }}</span>
                <small v-if="identity.target_type">已审核对象类型：{{ identity.target_type }}</small>
              </li>
            </ul>
            <div
              v-if="canSubmitIdentity && unresolvedIdentityOptions.length > 0 && suggestions.length > 0"
              class="identity-source-selectors"
            >
              <label>
                待解析消息参与者
                <select v-model.number="selectedParticipantIndex" name="participant_identity">
                  <option :value="-1" disabled>
                    请选择消息参与者
                  </option>
                  <option
                    v-for="(option, index) in unresolvedIdentityOptions"
                    :key="index"
                    :value="index"
                  >
                    消息参与者 {{ option.ordinal }} · {{ providerLabel(option.identity.provider) }}
                  </option>
                </select>
              </label>
              <label>
                关联建议
                <select v-model.number="selectedSuggestionIndex" name="association_suggestion">
                  <option :value="-1" disabled>
                    请选择关联建议
                  </option>
                  <option
                    v-for="(suggestion, index) in suggestions"
                    :key="index"
                    :value="index"
                  >
                    建议 {{ index + 1 }} · {{ suggestion.type }} · {{ formatConfidence(suggestion.confidence) }}
                  </option>
                </select>
              </label>
              <p v-if="!activeIdentity || !activeSuggestion" class="identity-relation-note">
                多个参与者或建议存在时，必须明确选择后才能读取候选对象。
              </p>
            </div>
            <p v-if="submitMessage" class="identity-success" role="status">
              {{ submitMessage }}
            </p>
            <p v-if="submitError" class="identity-error" role="alert">
              {{ submitError }}
            </p>

            <form
              v-if="canSubmitIdentity && activeIdentity && activeSuggestion"
              class="identity-review-form"
              aria-label="身份关联送审"
              @submit.prevent="submitIdentity"
            >
              <p class="identity-relation-note">
                只能从服务端返回的同团队候选对象和合格审核人中选择；提交后进入人工审核，不会直接确认身份。
              </p>
              <label>
                候选类型
                <select v-model="candidateType" name="candidate_type" @change="resetCandidateSearch">
                  <option value="User">系统用户</option>
                  <option value="Party">客户主体</option>
                  <option value="Contact">联系人</option>
                </select>
              </label>
              <div class="identity-search-row">
                <label>
                  搜索同团队候选对象
                  <input v-model="candidateSearch" name="candidate_search" maxlength="100">
                </label>
                <button type="button" data-action="search-candidates" @click="applyCandidateSearch">
                  搜索
                </button>
              </div>
              <fieldset>
                <legend>选择候选对象</legend>
                <p v-if="candidateLoading" role="status">
                  正在读取候选对象…
                </p>
                <p v-else-if="candidateError" class="identity-error" role="alert">
                  {{ candidateError }}
                </p>
                <p v-else-if="candidates.length === 0">
                  当前筛选没有同团队候选对象。
                </p>
                <label
                  v-for="(candidate, index) in candidates"
                  :key="`${candidate.candidate_type}:${index}`"
                  class="identity-choice"
                >
                  <input v-model="selectedCandidateIndex" type="radio" name="candidate" :value="index">
                  <span>{{ candidate.display_label }} · {{ candidate.candidate_type }}</span>
                </label>
              </fieldset>
              <div v-if="candidatePage > 1 || candidateHasMore" class="identity-pagination">
                <button
                  v-if="candidatePage > 1"
                  type="button"
                  data-candidate-page="previous"
                  @click="previousCandidatePage"
                >
                  上一页
                </button>
                <button
                  v-if="candidateHasMore"
                  type="button"
                  data-candidate-page="next"
                  @click="nextCandidatePage"
                >
                  下一页
                </button>
              </div>
              <label>
                合格审核人
                <select v-model="selectedReviewerIndex" name="assigned_reviewer">
                  <option :value="-1" disabled>请选择审核人</option>
                  <option
                    v-for="(reviewer, index) in eligibleReviewers"
                    :key="index"
                    :value="index"
                  >
                    {{ reviewer.display_label }}
                  </option>
                </select>
              </label>
              <button
                class="identity-submit"
                data-action="submit-identity-review"
                type="submit"
                :disabled="!selectedCandidate || !selectedReviewer || submittingIdentity"
              >
                {{ submittingIdentity ? "提交中…" : "提交审核" }}
              </button>
            </form>
            <p
              v-else-if="unresolvedIdentityOptions.length > 0 && suggestions.length === 0"
              class="identity-relation-note"
            >
              当前没有可用于送审的关联建议。
            </p>
            <p
              v-else-if="unresolvedIdentityOptions.length > 0 && !canSubmitIdentity"
              class="identity-relation-note"
            >
              当前角色只能查看身份状态，不能发起身份关联审核。
            </p>
          </article>
          <article class="communication-proposal-card">
            <h2>模型版本</h2>
            <p>{{ communication.model.name }} · {{ communication.model.version }}</p>
          </article>
        </div>
      </template>
    </DetailCommandTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { IdentityCandidateType } from "@/api/types";
import EvidencePanel from "@/components/data/EvidencePanel.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";
import { sessionState } from "@/session";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const resource = useOnlineResource(async () => {
  const response = await client.getCommunication(props.id);
  return response.data;
});
const identityResource = useOnlineResource(async () => {
  const response = await client.listIdentityStates(props.id);
  return response.data;
});
const communication = computed(() => resource.data.value?.communication);
const { state, message, requestId, load } = resource;
const identityState = identityResource.state;
const identities = computed(() => identityResource.data.value?.identities ?? []);
const connectorOwnerLabel = computed(
  () => identityResource.data.value?.connector_account_owner?.display_label ?? "未配置",
);
const identityError = computed(() =>
  identityState.value === "permission"
    ? "当前角色无权读取身份状态。"
    : identityResource.message.value,
);
const originalRevealed = ref(false);
const candidateType = ref<IdentityCandidateType>("Party");
const candidateSearch = ref("");
const appliedCandidateSearch = ref("");
const candidatePage = ref(1);
const candidateLoading = ref(false);
const candidateError = ref("");
const candidatePayload = ref<Awaited<ReturnType<typeof client.listIdentityCandidates>>["data"]>();
const selectedCandidateIndex = ref(-1);
const selectedReviewerIndex = ref(-1);
const selectedParticipantIndex = ref(-1);
const selectedSuggestionIndex = ref(-1);
const submittingIdentity = ref(false);
const submitMessage = ref("");
const submitError = ref("");
const submissionKey = ref("");
let candidateGeneration = 0;

const canSubmitIdentity = computed(() =>
  sessionState.roles.some((role) =>
    ["Sales User", "Sales Manager", "Integration Admin", "GBOS Admin"].includes(role),
  ),
);
const unresolvedIdentityOptions = computed(() =>
  identities.value.flatMap((identity, index) =>
    identity.status === "unresolved" ? [{ identity, ordinal: index + 1 }] : [],
  ),
);
const suggestions = computed(() => communication.value?.association_suggestions ?? []);
const activeIdentity = computed(
  () => unresolvedIdentityOptions.value[selectedParticipantIndex.value]?.identity,
);
const activeSuggestion = computed(() => suggestions.value[selectedSuggestionIndex.value]);
const candidates = computed(() => candidatePayload.value?.candidates ?? []);
const eligibleReviewers = computed(() => candidatePayload.value?.eligible_reviewers ?? []);
const candidateHasMore = computed(() => candidatePayload.value?.has_more ?? false);
const selectedCandidate = computed(() => candidates.value[selectedCandidateIndex.value]);
const selectedReviewer = computed(
  () => eligibleReviewers.value[selectedReviewerIndex.value],
);
const formatConfidence = (value: number) =>
  `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
const identityStatusLabel = (status: string) =>
  ({
    unresolved: "未解析",
    proposed: "已建议",
    pending: "待审核",
    confirmed: "已确认",
    revoked: "已撤回",
  })[status] ?? "未知状态";
const providerLabel = (provider: string) =>
  ({ email: "Email", wecom: "企业微信", whatsapp: "WhatsApp", phone: "电话", manual_import: "人工导入" })[
    provider
  ] ?? "未知渠道";

const resetSubmissionKey = () => {
  submissionKey.value = "";
  submitMessage.value = "";
  submitError.value = "";
};

const loadCandidates = async () => {
  const identity = activeIdentity.value;
  const currentGeneration = ++candidateGeneration;
  candidatePayload.value = undefined;
  selectedCandidateIndex.value = -1;
  selectedReviewerIndex.value = -1;
  candidateError.value = "";
  if (!identity || !activeSuggestion.value || !canSubmitIdentity.value) {
    candidateLoading.value = false;
    return;
  }
  candidateLoading.value = true;
  try {
    const response = await client.listIdentityCandidates({
      observationId: props.id,
      identityRef: identity.identity_ref,
      candidateType: candidateType.value,
      search: appliedCandidateSearch.value || undefined,
      page: candidatePage.value,
      pageSize: 20,
    });
    if (currentGeneration === candidateGeneration) {
      candidatePayload.value = response.data;
    }
  } catch (error) {
    if (currentGeneration === candidateGeneration) {
      candidateError.value =
        error instanceof BffError ? error.displayMessage : "暂时无法读取候选对象。";
    }
  } finally {
    if (currentGeneration === candidateGeneration) {
      candidateLoading.value = false;
    }
  }
};

const resetCandidateSearch = () => {
  candidatePage.value = 1;
  appliedCandidateSearch.value = candidateSearch.value.trim();
  resetSubmissionKey();
  void loadCandidates();
};
const applyCandidateSearch = () => resetCandidateSearch();
const previousCandidatePage = () => {
  if (candidatePage.value > 1) {
    candidatePage.value -= 1;
    resetSubmissionKey();
    void loadCandidates();
  }
};
const nextCandidatePage = () => {
  if (candidateHasMore.value) {
    candidatePage.value += 1;
    resetSubmissionKey();
    void loadCandidates();
  }
};

const submitIdentity = async () => {
  const identity = activeIdentity.value;
  const suggestion = activeSuggestion.value;
  const candidate = selectedCandidate.value;
  const reviewer = selectedReviewer.value;
  if (!identity || !suggestion || !candidate || !reviewer || submittingIdentity.value) {
    return;
  }
  submittingIdentity.value = true;
  submitMessage.value = "";
  submitError.value = "";
  submissionKey.value ||= createIdempotencyKey();
  try {
    await client.submitIdentityForReview({
      observation_id: props.id,
      identity_ref: identity.identity_ref,
      suggestion_key: suggestion.suggestion_key,
      selected_candidate_type: candidate.candidate_type,
      selected_candidate_ref: candidate.candidate_ref,
      assigned_reviewer: reviewer.reviewer_ref,
      expected_state: "unresolved",
      expected_revision: 0,
      idempotency_key: submissionKey.value,
    });
    submitMessage.value = "已提交人工审核，不会直接确认身份。";
    await identityResource.load();
  } catch (error) {
    submitError.value =
      error instanceof BffError ? error.displayMessage : "暂时无法提交身份审核。";
    if (
      error instanceof BffError &&
      (error.status === 409 ||
        error.code === "revision_conflict" ||
        error.code === "idempotency_conflict")
    ) {
      await identityResource.load();
      await loadCandidates();
    }
  } finally {
    submittingIdentity.value = false;
  }
};

watch(
  () => unresolvedIdentityOptions.value.map((option) => option.identity.identity_ref).join("|"),
  () => {
    selectedParticipantIndex.value =
      unresolvedIdentityOptions.value.length === 1 ? 0 : -1;
  },
);
watch(
  () => suggestions.value.map((suggestion) => suggestion.suggestion_key).join("|"),
  () => {
    selectedSuggestionIndex.value = suggestions.value.length === 1 ? 0 : -1;
  },
);
watch(
  () => [activeIdentity.value?.identity_ref, activeSuggestion.value?.suggestion_key] as const,
  () => {
    candidatePage.value = 1;
    if (!submittingIdentity.value) {
      submissionKey.value = "";
    }
    void loadCandidates();
  },
);
watch([selectedCandidateIndex, selectedReviewerIndex], () => {
  if (!submittingIdentity.value) {
    resetSubmissionKey();
  }
});

watch(
  () => props.id,
  () => {
    originalRevealed.value = false;
    candidateGeneration += 1;
    candidatePayload.value = undefined;
    candidateError.value = "";
    candidateLoading.value = false;
    candidatePage.value = 1;
    candidateSearch.value = "";
    appliedCandidateSearch.value = "";
    selectedCandidateIndex.value = -1;
    selectedReviewerIndex.value = -1;
    selectedParticipantIndex.value = -1;
    selectedSuggestionIndex.value = -1;
    resetSubmissionKey();
    void load();
    void identityResource.load();
  },
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

.communication-back-link {
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  padding: 8px 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font-family: var(--gbos-font-sans);
  font-size: 14px;
  font-weight: 700;
  line-height: 1.2;
  text-decoration: none;
}

.communication-facts {
  padding: 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
}

.communication-facts__list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(136px, 1fr));
  gap: 10px;
  margin: 0;
}

.communication-facts__list > div {
  min-width: 0;
  padding: 10px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.communication-facts__list dt,
.communication-facts__list dd {
  margin: 0;
}

.communication-facts__list dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.communication-facts__list dd {
  margin-top: 4px;
  color: var(--gbos-text);
  font-size: 14px;
  font-weight: 700;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.communication-informal-notice,
.communication-restricted-notice {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--gbos-accent);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-accent-text);
  background: var(--gbos-canvas);
  font-size: 13px;
  font-weight: 700;
  line-height: 1.55;
}

.communication-informal-notice {
  margin-bottom: 12px;
}

.communication-original-toggle,
.identity-review-form button,
.identity-review-form select,
.identity-review-form input,
.identity-source-selectors select {
  min-height: 44px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font: inherit;
}

.communication-original-toggle,
.identity-review-form button {
  width: fit-content;
  padding: 9px 14px;
  font-weight: 700;
  cursor: pointer;
}

.communication-original-toggle:focus-visible,
.identity-review-form button:focus-visible,
.identity-review-form select:focus-visible,
.identity-review-form input:focus-visible,
.identity-source-selectors select:focus-visible,
.identity-choice input:focus-visible {
  outline: 3px solid var(--gbos-accent);
  outline-offset: 2px;
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
.evidence-reference-panel .communication-evidence-list {
  margin-top: 10px;
}

.communication-evidence-list,
.communication-proposal-list {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.communication-evidence-list li {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 6px 16px;
  padding: 10px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.communication-evidence-list strong,
.communication-evidence-list span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.communication-evidence-list span {
  color: var(--gbos-muted);
}

.communication-proposal-card {
  display: grid;
  min-width: 0;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.communication-proposal-card h2,
.communication-proposal-card p {
  margin: 0;
}

.communication-proposal-card h2 {
  font-size: 18px;
  line-height: 1.4;
}

.communication-proposal-card p {
  color: var(--gbos-muted);
  font-size: 14px;
  line-height: 1.55;
}

.communication-proposal-list li {
  display: grid;
  min-width: 0;
  gap: 4px;
  padding: 10px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.communication-proposal-list li > * {
  min-width: 0;
  overflow-wrap: anywhere;
}

.communication-proposal-list small {
  color: var(--gbos-muted);
}

.identity-state-list,
.identity-review-form,
.identity-source-selectors {
  display: grid;
  min-width: 0;
  gap: 12px;
  margin: 0;
  padding: 0;
}

.identity-state-list {
  list-style: none;
}

.identity-state-list li {
  display: grid;
  min-width: 0;
  gap: 4px;
  padding: 10px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.identity-state-list li > *,
.identity-review-form label,
.identity-relation-note,
.identity-error,
.identity-success {
  min-width: 0;
  overflow-wrap: anywhere;
}

.identity-state-list small,
.identity-relation-note {
  color: var(--gbos-muted);
}

.identity-review-form {
  padding-top: 12px;
  border-top: 1px solid var(--gbos-border);
}

.identity-source-selectors {
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
  padding-top: 12px;
  border-top: 1px solid var(--gbos-border);
}

.identity-source-selectors > label {
  display: grid;
  min-width: 0;
  gap: 6px;
  color: var(--gbos-text);
  font-size: 13px;
  font-weight: 700;
}

.identity-source-selectors select {
  width: 100%;
  min-width: 0;
  padding: 8px 10px;
}

.identity-source-selectors .identity-relation-note {
  grid-column: 1 / -1;
}

.identity-review-form > label,
.identity-search-row label {
  display: grid;
  gap: 6px;
  color: var(--gbos-text);
  font-size: 13px;
  font-weight: 700;
}

.identity-review-form select,
.identity-review-form input {
  width: 100%;
  min-width: 0;
  padding: 8px 10px;
}

.identity-search-row,
.identity-pagination {
  display: flex;
  min-width: 0;
  align-items: end;
  flex-wrap: wrap;
  gap: 10px;
}

.identity-search-row label {
  flex: 1 1 220px;
}

.identity-review-form fieldset {
  display: grid;
  min-width: 0;
  gap: 8px;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
}

.identity-review-form legend {
  padding: 0 4px;
  font-weight: 700;
}

.identity-choice {
  display: flex;
  min-height: 44px;
  min-width: 0;
  align-items: center;
  gap: 10px;
  padding: 7px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.identity-choice input {
  width: 20px;
  min-height: 20px;
  flex: 0 0 20px;
}

.identity-choice span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.identity-submit {
  color: var(--gbos-primary) !important;
}

.identity-review-form button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.identity-error,
.identity-success {
  margin: 0;
  padding: 10px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.identity-error {
  border-color: var(--gbos-primary);
}

.identity-success {
  border-color: var(--gbos-accent);
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

@media (max-width: 767px) {
  .communication-back-link {
    width: 100%;
    min-height: 44px;
  }

  .communication-original-toggle,
  .identity-review-form button {
    width: 100%;
  }

  .communication-facts__list {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
