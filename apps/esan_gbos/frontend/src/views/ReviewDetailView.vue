<template>
  <section class="review-detail-view">
    <DetailCommandTemplate>
      <template #header>
        <PageHeader
          eyebrow="ESAN GBOS · GATE 4"
          title="审核案件"
          description="核对冻结主体快照、版本、哈希、策略与证据引用后，只记录当前案件的人工决定。"
        >
          <template #actions>
            <RouterLink class="review-back-link" to="/gbos/review">
              返回队列
            </RouterLink>
          </template>
        </PageHeader>
      </template>

      <template v-if="reviewCase" #facts>
        <div class="review-facts">
          <div class="review-facts__heading">
            <p>{{ statusLabel }}</p>
            <h2>{{ reviewCase.title }}</h2>
          </div>
          <dl class="review-fact-rows">
            <div><dt>案件编号</dt><dd>{{ reviewCase.name }}</dd></div>
            <div>
              <dt>主体</dt>
              <dd>
                {{ isIdentityReview ? "身份解析受保护主体" : `${reviewCase.subject.doctype} · ${reviewCase.subject.name}` }}
              </dd>
            </div>
            <div><dt>案件版本</dt><dd>{{ reviewCase.case_revision }}</dd></div>
            <div><dt>主体版本</dt><dd>{{ reviewCase.subject.revision }}</dd></div>
            <div><dt>案件哈希</dt><dd><code>{{ reviewCase.case_payload_hash }}</code></dd></div>
            <div><dt>主体哈希</dt><dd><code>{{ reviewCase.subject.payload_hash }}</code></dd></div>
            <div><dt>策略</dt><dd>{{ reviewCase.policy_reference }}</dd></div>
          </dl>
        </div>
      </template>

      <template #main>
        <ResourceBoundary
          :state="state"
          :message="boundaryMessage"
          :request-id="requestId"
          :empty="!reviewCase"
          @retry="load"
        >
          <div v-if="reviewCase" class="review-detail-stack">
            <DemoBanner v-if="reviewCase.origin === 'Fixture'" />
            <p v-if="decisionMessage" class="review-notice review-notice--success" role="status">
              {{ decisionMessage }}
            </p>
            <p v-if="decisionError" class="review-notice review-notice--error" role="alert">
              {{ decisionError }}
            </p>

            <article
              v-if="isIdentityReview"
              class="review-detail-card identity-safe-panel"
              aria-labelledby="identity-safe-title"
            >
              <div class="review-detail-card__heading">
                <p>PROTECTED IDENTITY</p>
                <h2 id="identity-safe-title">
                  身份解析案件
                </h2>
              </div>
              <p>
                受保护的身份主体快照不会在此页显示。请返回审核队列并选择 Identity Resolution 身份解析筛选，读取服务端提供的安全目标、固定证据与版本信息。
              </p>
            </article>

            <article v-else class="review-detail-card" aria-labelledby="snapshot-title">
              <div class="review-detail-card__heading">
                <p>READ-ONLY</p>
                <h2 id="snapshot-title">
                  完整冻结主体 Snapshot
                </h2>
              </div>
              <p>以下内容是案件引用的冻结快照，本页不会改写该主体。</p>
              <pre>{{ snapshotJson }}</pre>
            </article>

            <article class="review-detail-card" aria-labelledby="evidence-title">
              <div class="review-detail-card__heading">
                <p>REFERENCES ONLY</p>
                <h2 id="evidence-title">
                  证据引用
                </h2>
              </div>
              <p v-if="reviewCase.evidence.length === 0">
                暂无证据引用。
              </p>
              <ul v-else class="review-evidence-list">
                <li v-for="evidence in reviewCase.evidence" :key="evidence.reference">
                  <dl class="review-fact-rows">
                    <div><dt>引用</dt><dd><strong>{{ evidence.reference }}</strong></dd></div>
                    <div><dt>类型</dt><dd>{{ evidence.evidence_type }}</dd></div>
                    <div v-if="evidence.revision !== undefined">
                      <dt>版本</dt><dd>{{ evidence.revision }}</dd>
                    </div>
                    <div v-if="evidence.payload_hash">
                      <dt>哈希</dt><dd><code>{{ evidence.payload_hash }}</code></dd>
                    </div>
                  </dl>
                </li>
              </ul>
            </article>
          </div>
        </ResourceBoundary>
      </template>

      <template v-if="reviewCase" #command>
        <ReviewDecisionForm
          v-if="reviewCase.review_status === 'Pending'"
          :submitting="submitting"
          :reset-key="formResetKey"
          @decide="decide"
        />
        <article v-else class="review-ended-card">
          <p>CASE CLOSED</p>
          <h2>案件已结束</h2>
          <span>{{ reviewCase.decision_note || "该案件已有人工决定。" }}</span>
        </article>
      </template>
    </DetailCommandTemplate>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { BffError, createIdempotencyKey } from "@/api/bff";
import { useBffClient } from "@/api/injection";
import type { ReviewCaseDetailPayload } from "@/api/types";
import DemoBanner from "@/components/DemoBanner.vue";
import ReviewDecisionForm from "@/components/ReviewDecisionForm.vue";
import ResourceBoundary from "@/components/feedback/ResourceBoundary.vue";
import DetailCommandTemplate from "@/components/layout/DetailCommandTemplate.vue";
import PageHeader from "@/components/layout/PageHeader.vue";
import { useOnlineResource } from "@/composables/useOnlineResource";

const props = defineProps<{ id: string }>();
const client = useBffClient();
const submitting = ref(false);
const formResetKey = ref(0);
const decisionError = ref("");
const decisionMessage = ref("");
const decidedPayload = ref<ReviewCaseDetailPayload>();

const resource = useOnlineResource(async () => {
  decidedPayload.value = undefined;
  const response = await client.getReviewCase(props.id);
  return response.data;
});
const reviewCase = computed(
  () => decidedPayload.value?.case ?? resource.data.value?.case,
);
const isIdentityReview = computed(
  () => reviewCase.value?.subject.doctype === "GBOS External Identity",
);
const statusLabel = computed(() => {
  const labels = {
    Pending: "待审核",
    Approved: "已批准",
    Rejected: "已拒绝",
    Superseded: "已取代",
  } as const;
  return reviewCase.value ? labels[reviewCase.value.review_status] : "";
});
const snapshotJson = computed(() =>
  reviewCase.value
    ? JSON.stringify(reviewCase.value.subject.snapshot, null, 2)
    : "",
);
const boundaryMessage = computed(() =>
  resource.state.value === "ready" && !reviewCase.value
    ? "未找到可审核案件。"
    : resource.message.value,
);
const { state, requestId, load } = resource;

watch(
  () => props.id,
  () => {
    decisionError.value = "";
    decisionMessage.value = "";
    formResetKey.value += 1;
    void load();
  },
);

const decide = async (decision: "Approved" | "Rejected", note: string) => {
  const current = reviewCase.value;
  if (!current || submitting.value) {
    return;
  }
  decisionError.value = "";
  decisionMessage.value = "";
  submitting.value = true;
  try {
    const response = await client.decideReviewCase({
      name: current.name,
      decision,
      decision_note: note,
      expected_revision: current.case_revision,
      expected_subject_revision: current.subject.revision,
      idempotency_key: createIdempotencyKey(),
      subject_payload_sha256: current.subject.payload_hash,
      evidence_refs: current.evidence.map((evidence) => evidence.reference),
      policy_version: current.policy_reference,
      expected_case_payload_hash: current.case_payload_hash,
    });
    decidedPayload.value = response.data;
    decisionMessage.value = "审核决定已记录。";
    formResetKey.value += 1;
  } catch (error) {
    if (error instanceof BffError) {
      decisionError.value = error.displayMessage;
      if (error.status === 409 || error.code === "revision_conflict") {
        formResetKey.value += 1;
        await load();
      }
    } else {
      decisionError.value = "暂时无法提交审核决定，请稍后重试。";
    }
  } finally {
    submitting.value = false;
  }
};
</script>

<style scoped>
.review-detail-view,
.review-detail-stack {
  min-width: 0;
}

.review-back-link {
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  padding: 8px 14px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-surface);
  font-size: 14px;
  font-weight: 700;
  text-decoration: none;
}

.review-facts,
.review-detail-card,
.review-ended-card {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-card);
  background: var(--gbos-surface);
  box-shadow: var(--gbos-shadow-card);
}

.review-facts__heading p,
.review-facts__heading h2,
.review-detail-card__heading p,
.review-detail-card__heading h2,
.review-detail-card > p,
.review-ended-card p,
.review-ended-card h2,
.review-ended-card span,
.review-notice {
  margin: 0;
}

.review-facts__heading p,
.review-detail-card__heading p,
.review-ended-card p {
  color: var(--gbos-accent-text);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.review-facts__heading h2,
.review-detail-card__heading h2,
.review-ended-card h2 {
  margin-top: 3px;
  color: var(--gbos-text);
  font-size: 19px;
  line-height: 1.35;
}

.review-fact-rows {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 8px;
  margin: 12px 0 0;
}

.review-fact-rows > div {
  min-width: 0;
  padding: 9px 10px;
  border-radius: var(--gbos-radius-control);
  background: var(--gbos-canvas);
}

.review-fact-rows dt,
.review-fact-rows dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.review-fact-rows dt {
  color: var(--gbos-muted);
  font-size: 12px;
  font-weight: 700;
}

.review-fact-rows dd {
  margin-top: 4px;
  color: var(--gbos-text);
  font-size: 13px;
  line-height: 1.5;
}

.review-detail-stack {
  display: grid;
  gap: 12px;
}

.review-detail-card > p,
.review-ended-card span {
  display: block;
  margin-top: 8px;
  color: var(--gbos-muted);
  font-size: 13px;
  line-height: 1.55;
}

.review-detail-card pre {
  max-width: 100%;
  margin: 12px 0 0;
  padding: 12px;
  overflow: auto;
  border: 1px solid var(--gbos-border);
  border-radius: var(--gbos-radius-control);
  color: var(--gbos-text);
  background: var(--gbos-canvas);
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.review-evidence-list {
  display: grid;
  gap: 10px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.review-evidence-list li {
  min-width: 0;
  padding-top: 2px;
  border-top: 1px solid var(--gbos-border);
}

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

@media (max-width: 767px) {
  .review-back-link {
    min-height: 44px;
  }

  .review-fact-rows {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
